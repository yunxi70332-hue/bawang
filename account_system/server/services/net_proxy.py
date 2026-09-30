"""网络出口管理（net_proxy）：账号相关请求的大陆 IP 代理接入与自动回退（2026-09-29）。

背景：生产部署在香港，茶姬账号域操作（登录/查券/下单/取餐码）走大陆 IP 更稳；
本模块给所有出站茶姬域请求（gw.chagee.com / gj-api.bwcj.com 等）统一加出口路由：

  配置（data/proxy_config.json，env CHAGEE_PROXY_CONFIG 可重定向——测试隔离约定）
    mode=api     提取式代理池：调服务商 API（TXT/JSON）取 ip:port，TTL 到期或失败自动轮换
                 （实测海量IP getIpEncrypt：JSON data[].ip:port + realIp/area 自带出口信息；
                  其隧道对 echo 域名回 614 domain is black、未授权业务域名静默丢弃——故
                  健康判定以「真实茶姬网关可达」为准，出口 IP 优先采信服务商 realIp/area）
    mode=manual  固定代理：host:port + 协议(HTTP/HTTPS代理 或 SOCKS5) + 认证(选填)
  协议：HTTP(S) 代理（ProxyHandler CONNECT 隧道，代理认证预置 Proxy-authorization 头）
        SOCKS5（PySocks socksocket 自定义 Connection/handler，缺库时该协议明确报错）

  路由（monkeypatch urllib.request.urlopen，scripts/chagee_*.py 全部覆盖）
    - 目标 host 命中 route_domains（默认 chagee.com / bwcj.com 后缀）才接管；其余原样直连
    - 代理健康 → 走代理；请求期代理异常(非 HTTPError) → 本次请求立即直连重试（调用方无感），
      连续 2 次失败切直连并唤醒监控线程轮换
    - 未配置/未启用/代理不可用 → 一律服务器直连（fail-open，可用性优先）
  监控线程（CHAGEE_PROXY_CHECK_INTERVAL_SECONDS，默认 30s，0=停用——conftest 离线测试约定）
    周期：提取/轮换候选(≤3 个逐一实测) → 激活最优 → 状态经 events_bus topic=proxy SSE 推送；
    直接出口每 300s 探一次基线。切代理/回退/恢复全部落 oplog（proxy.*），关键账号请求
    （短信/登录/登出/账号信息/券/订单）按「操作+路由+出口IP」限频记录 proxy.egress。
"""

import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from oplog import heartbeat, log_op
from services import events_bus

logger = logging.getLogger("net_proxy")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(_BASE, "..", "data", "proxy_config.json")
CONFIG_PATH = os.path.join(os.environ.get("CHAGEE_PROXY_CONFIG") or DEFAULT_CONFIG_PATH)

# 代理健康判定目标：真实业务网关（任何 HTTP 应答——含 4xx/5xx/errcode JSON——即视为隧道可用；
# 而服务商 echo 域名普遍被拉黑，不能作为判定依据）
HEALTHCHECK_URL = os.environ.get("CHAGEE_PROXY_HEALTHCHECK_URL") or "https://gw.chagee.com/"

# 出口 IP 探测端点（直连基线 & 部分代理可用时；全部失败则回退服务商 realIp/area 元数据）
_ECHO_ENDPOINTS = (
    "https://myip.ipip.net",           # 文本：当前 IP：x.x.x.x  来自于：中国 广东 深圳 电信
    "https://api.ip.sb/geoip",         # JSON：{ip, country, country_code, region}
    "https://ipinfo.io/json",          # JSON：{ip, country, region}
)

# 关键账号请求打标（URL 路径子串 → 操作名），用于 proxy.egress 留痕
_KEY_PATH_RULES = (
    ("message/send", "发送短信验证码"),
    ("auth/login", "账号登录"),
    ("auth/logout", "账号登出"),
    ("userInfo", "账号信息查询"),
    ("coupon", "优惠券查询"),
    ("order", "下单/取餐"),
)

DEFAULT_CONFIG = {
    "enabled": False,
    "mode": "api",                      # api | manual
    # 海量IP 提取 API 地址**不再硬编码**（凭证不入代码/仓库/发布包）：
    # 配置写入 data/proxy_config.json（已 .gitignore），经系统设置页或手工落盘
    "api_url": "",
    "api_format": "json",               # txt | json（txt 每行/逗号分隔 ip:port[:user:pass]）
    "protocol": "http",                 # http(HTTP/HTTPS代理) | socks5
    "username": "",
    "password": "",
    "ttl_seconds": 120,                 # api 模式提取代理有效期估计，到期前轮换
    "check_interval": 30,               # 健康检测周期秒（监控线程 sleep 粒度）
    "manual_host": "",
    "manual_port": 0,
    "route_domains": ["chagee.com", "bwcj.com"],   # 命中后缀才经代理路由
    "require_mainland": True,           # 代理出口非大陆视为无效（防买到港/海外代理）
}

MONITOR_INTERVAL = float(os.environ.get("CHAGEE_PROXY_CHECK_INTERVAL_SECONDS", "30") or 0)
DIRECT_PROBE_INTERVAL = 300.0          # 直连出口基线探测周期
_CANDIDATE_LIMIT = 3                   # 每轮实测候选上限
_PROXY_AUTH_FAIL_THRESHOLD = 2         # 连续失败 N 次降级直连
_EGRESS_LOG_MIN_INTERVAL = 90.0        # 同(操作,路由,出口IP)组合落库限频

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_PROXY_LINE_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})(?::(\S+):(\S+))?$")

_lock = threading.RLock()          # 守 _cfg/_state
_refresh_lock = threading.Lock()   # 串行化检测周期（监控线程 / refresh_now / 保存后即检）
_wake = threading.Event()
_installed = False
_orig_urlopen = None
_openers: dict = {}                # opener key -> OpenerDirector（配置变更时清空重建）
_egress_log_last: dict = {}
_last_fetch_attempt = 0.0
_api_fetch_min_interval = 5.0      # 服务商提取频率保护（幂等重入也削峰）

_cfg: dict = dict(DEFAULT_CONFIG)

_state: dict = {
    "route": "direct",             # direct | proxy —— 当前实际生效出口
    "proxy_addr": "",
    "proxy_protocol": "http",
    "proxy_obtained_at": 0.0,
    "proxy_egress_ip": "",
    "proxy_egress_region": "",
    "proxy_egress_source": "",     # provider | echo | ""
    "proxy_egress_mainland": None,  # True/False/None(未知)
    "direct_ip": "",
    "direct_region": "",
    "direct_mainland": None,
    "direct_checked_at": 0.0,
    "last_check_at": 0.0,
    "last_error": "",
    "consecutive_failures": 0,
    "stats": {"requests_proxied": 0, "requests_direct": 0, "fallback_retries": 0, "rotations": 0},
}


# ---------------- 配置 ----------------

def load_config() -> dict:
    global _cfg
    with _lock:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            stored = {}
        except Exception as e:
            logger.warning("proxy_config.json 解析失败，沿用默认: %s", e)
            stored = {}
        _cfg = dict(DEFAULT_CONFIG)
        _cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
        return dict(_cfg)


def _save_config_file(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def validate_config(cfg: dict) -> dict:
    """校验并规范化；返回规范化后的完整配置，非法抛 ValueError（中文明因）。"""
    out = dict(DEFAULT_CONFIG)
    out.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT_CONFIG})
    out["enabled"] = bool(out["enabled"])
    out["mode"] = str(out["mode"] or "api").lower()
    out["api_format"] = str(out["api_format"] or "json").lower()
    out["protocol"] = str(out["protocol"] or "http").lower()
    if out["mode"] not in ("api", "manual"):
        raise ValueError("mode 仅支持 api（提取API）/ manual（固定代理）")
    if out["api_format"] not in ("txt", "json"):
        raise ValueError("api_format 仅支持 txt / json")
    if out["protocol"] not in ("http", "socks5"):
        raise ValueError("protocol 仅支持 http（HTTP/HTTPS代理）/ socks5")
    try:
        out["ttl_seconds"] = max(30, min(86400, int(out["ttl_seconds"] or 120)))
        out["check_interval"] = max(10, min(3600, int(out["check_interval"] or 30)))
        out["manual_port"] = max(0, min(65535, int(out["manual_port"] or 0)))
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds / check_interval / manual_port 须为整数")
    if out["mode"] == "api":
        if not str(out["api_url"]).startswith(("http://", "https://")):
            raise ValueError("api 模式需要合法的提取 API 地址（http/https）")
    else:
        if not out["manual_host"] or not out["manual_port"]:
            raise ValueError("manual 模式需要代理服务器地址与端口")
    if out["protocol"] == "socks5":
        try:
            import socks  # noqa: F401
        except ImportError:
            raise ValueError("SOCKS5 需要 PySocks 库（pip install pysocks）")
    domains = out.get("route_domains") or []
    if not isinstance(domains, list) or not domains:
        raise ValueError("route_domains 不能为空（至少保留 chagee.com）")
    out["route_domains"] = [str(d).strip().lstrip(".").lower() for d in domains if str(d).strip()]
    return out


def save_config(partial: dict, actor: str = "") -> dict:
    """合并保存 + 即刻生效（重建 opener、清失败计数、唤醒检测）。返回规范化后的配置。"""
    with _lock:
        merged = dict(_cfg)
        merged.update(partial or {})
        normalized = validate_config(merged)
        _save_config_file(normalized)
        _cfg.clear()
        _cfg.update(normalized)
        _openers.clear()
        _state["consecutive_failures"] = 0
    log_op("proxy.config_save", actor=actor or "-",
           params={"enabled": normalized["enabled"], "mode": normalized["mode"],
                   "protocol": normalized["protocol"], "api_url": normalized["api_url"],
                   "manual_host": normalized["manual_host"], "manual_port": normalized["manual_port"]})
    _wake.set()
    return dict(normalized)


def masked_config() -> dict:
    with _lock:
        cfg = dict(_cfg)
    if cfg.get("password"):
        cfg["password"] = "***"
    return cfg


def maybe_reload_config() -> None:
    """监控线程每周期静默重读配置文件（主 API 保存后双进程共享同一 JSON，另一进程下轮生效）。"""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if mtime != getattr(maybe_reload_config, "_mtime", None):
        maybe_reload_config._mtime = mtime
        load_config()
        with _lock:
            _openers.clear()


# ---------------- 出口 IP / 大陆判定 ----------------

def _is_mainland_region(region: str):
    """region 为空返回 None（未知）；含中国但非港澳台为 True；其余 False。"""
    r = (region or "").strip()
    if not r:
        return None
    low = r.lower()
    if "中国" in r or "china" in low or "cn" == low:
        return not any(b in low for b in ("香港", "澳门", "台湾", "hong kong", "hongkong",
                                          "macao", "macau", "taiwan"))
    return False


def _parse_echo(url: str, body: str):
    """解析出口探测响应 → {ip, region, mainland}；不认识返回 None。"""
    ip_m = _IP_RE.search(body)
    if not ip_m:
        return None
    ip = ip_m.group(1)
    region = ""
    low_url = url.lower()
    if "ipip.net" in low_url and ("来自" in body):
        region = body.split("来自于", 1)[-1].split("来自", 1)[-1].strip().lstrip("：: ").strip()
    else:
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                region = " ".join(str(x) for x in (
                    j.get("country_code"), j.get("country"), j.get("region")) if x)
        except Exception:
            region = ""
    return {"ip": ip, "region": region, "mainland": _is_mainland_region(region)}


def _fetch_via(open_obj, url, timeout: float):
    """open_obj 为 None 时直连（_orig_urlopen）；否则经 opener.open。返回 (status, text)。"""
    try:
        if open_obj is None:
            r = _orig_urlopen(url, timeout=timeout)
        else:
            r = open_obj.open(url, timeout=timeout)
        with r:
            return r.status, r.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # HTTP 应答=通道可用（echo 站点 4xx 也带 body 可解析）
        try:
            return e.code, e.read(2048).decode("utf-8", errors="replace")
        except Exception:
            return e.code, ""
    except Exception:
        raise


def probe_egress(opener=None, timeout: float = 6.0):
    """探测通道出口 IP；逐个端点尝试，成功即返回，全败返回 None。"""
    for url in _ECHO_ENDPOINTS:
        try:
            status, text = _fetch_via(opener, url, timeout)
            parsed = _parse_echo(url, text)
            if parsed and parsed.get("ip"):
                parsed["endpoint"] = url
                return parsed
        except Exception:
            continue
    return None


# ---------------- opener 构造（HTTP代理 / SOCKS5，认证预置） ----------------

def _socks_available() -> bool:
    try:
        import socks  # noqa: F401
        return True
    except ImportError:
        return False


def _build_opener(key: str):
    """按缓存 key（protocol|host:port|user|pwd，内含全部代理参数）构造 opener。"""
    if key in _openers:
        return _openers[key]
    scheme, addr, user, pwd = key.split("|")
    host, port = addr.rsplit(":", 1)
    if scheme == "socks5":
        import socks as _socks

        peer = (host, int(port), user or None, pwd or None)

        class SocksHTTPConnection(http.client.HTTPConnection):
            def connect(self):
                s = _socks.socksocket()
                s.set_proxy(_socks.SOCKS5, peer[0], peer[1], rdns=True,
                            username=peer[2], password=peer[3])
                s.settimeout(self.timeout or 10)
                s.connect((self.host, self.port))
                self.sock = s

        class SocksHTTPSConnection(http.client.HTTPSConnection):
            def connect(self):
                s = _socks.socksocket()
                s.set_proxy(_socks.SOCKS5, peer[0], peer[1], rdns=True,
                            username=peer[2], password=peer[3])
                s.settimeout(self.timeout or 10)
                s.connect((self.host, self.port))
                self.sock = self._context.wrap_socket(s, server_hostname=self.host)

        class SocksHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(SocksHTTPConnection, req)

        class SocksHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(SocksHTTPSConnection, req)

        opener = urllib.request.build_opener(SocksHTTPHandler, SocksHTTPSHandler)
    else:
        cred = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pwd, safe='')}@" if user else ""
        purl = f"http://{cred}{host}:{port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": purl, "https": purl}))
    opener.proxy_auth_header = _basic_proxy_auth(user, pwd)
    _openers[key] = opener
    return opener


def _basic_proxy_auth(user: str, pwd: str) -> str:
    if not user:
        return ""
    import base64
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


def _proxy_opener(addr: str, protocol: str, user: str, pwd: str):
    key = f"{protocol}|{addr}|{user}|{pwd}"
    return _build_opener(key)


def _connect_probe(host: str, port: int, target: str, timeout: float = 6.0):
    """原始 CONNECT 探测：返回代理对 CONNECT 的首行应答（诊断用；空串=无应答）。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        data = b""
        t0 = time.time()
        while b"\r\n" not in data and time.time() - t0 < timeout:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        return data.split(b"\r\n", 1)[0].decode("latin1", "replace") if data else ""
    except Exception as e:
        return f"EXC {type(e).__name__}: {e}"
    finally:
        s.close()


# ---------------- 提取 API 解析（TXT / JSON） ----------------

def parse_proxy_payload(text: str, api_format: str) -> list[dict]:
    """解析服务商提取响应 → [{host, port, user, pass, real_ip, area}]。

    JSON 容错策略（各家字段名不一）：递归收集 dict 中的 ip/proxyIp + port 键值对，
    以及形如 ip:port 的字符串值；realIp/real_ip/area 顺带提取（海量IP 实测形态）。
    TXT：按空白/逗号/分号切 token，匹配 ip:port[:user:pass]。"""
    items: list[dict] = []
    seen: set = set()

    def push(host, port, user="", pwd="", real_ip="", area=""):
        try:
            if not (1 <= int(port) <= 65535):
                return
            ipaddress.ip_address(host)
        except ValueError:
            return
        k = f"{host}:{port}"
        if k in seen:
            return
        seen.add(k)
        items.append({"host": host, "port": int(port), "user": user, "pwd": pwd,
                      "real_ip": real_ip, "area": area})

    if api_format == "json":
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if payload is None:  # 声明 json 但回了 txt：按 txt 兜底
            return parse_proxy_payload(text, "txt")

        def walk(node):
            if isinstance(node, dict):
                host = next((str(node[k]) for k in ("ip", "proxyIp", "proxy_ip", "host")
                             if node.get(k) and _IP_RE.fullmatch(str(node.get(k)))), None)
                port = next((node[k] for k in ("port", "proxyPort", "proxy_port")
                             if node.get(k)), None)
                real = next((str(node[k]) for k in ("realIp", "real_ip", "exitIp", "exit_ip")
                             if node.get(k)), "")
                area = next((str(node[k]) for k in ("area", "city_area", "region") if node.get(k)), "")
                if host and port:
                    push(host, port, real_ip=real, area=area)
                elif isinstance(node.get("proxy"), str):
                    m = _PROXY_LINE_RE.match(node["proxy"].strip())
                    if m:
                        push(m.group(1), m.group(2), m.group(3) or "", m.group(4) or "")
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                m = _PROXY_LINE_RE.match(node.strip())
                if m:
                    push(m.group(1), m.group(2), m.group(3) or "", m.group(4) or "")

        walk(payload)
    else:
        for token in re.split(r"[\s,;，；]+", text):
            m = _PROXY_LINE_RE.match(token.strip())
            if m:
                push(m.group(1), m.group(2), m.group(3) or "", m.group(4) or "")
    return items


def fetch_api_proxies(api_url: str, api_format: str, timeout: float = 12.0) -> list[dict]:
    """直连提取（绝不经代理自身）；含服务商频率保护与白名单类错误透传。"""
    global _last_fetch_attempt
    now = time.monotonic()
    if now - _last_fetch_attempt < _api_fetch_min_interval:
        time.sleep(_api_fetch_min_interval - (now - _last_fetch_attempt))
    _last_fetch_attempt = time.monotonic()
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Accept": "*/*"})
    with _orig_urlopen(req, timeout=timeout) as r:   # urllib 默认跟随 302（海量IP api→ecs）
        text = r.read(65536).decode("utf-8", errors="replace")
    items = parse_proxy_payload(text, api_format)
    if not items:
        # 服务商错误文案（白名单/频率/余额）原样透出，直接指向可操作原因
        raise RuntimeError(f"提取 API 未解析到代理，响应: {text[:200] or '(空)'}")
    return items


# ---------------- 路由判定与 urlopen 补丁 ----------------

def _host_of(url) -> str:
    if isinstance(url, urllib.request.Request):
        url = url.full_url
    parsed = urllib.parse.urlsplit(str(url))
    return (parsed.hostname or "").lower()


def _routable(host: str) -> bool:
    if not host:
        return False
    with _lock:
        domains = list(_cfg.get("route_domains") or [])
    return any(host == d or host.endswith("." + d) for d in domains)


def _effective_route():
    """返回 (opener|None, route, proxy_addr)。disabled/无活跃代理 → 直连。"""
    with _lock:
        if not _cfg.get("enabled") or _state["route"] != "proxy" or not _state["proxy_addr"]:
            return None, "direct", ""
        opener = _proxy_opener(_state["proxy_addr"], _state["proxy_protocol"],
                               _cfg.get("username") or "", _cfg.get("password") or "")
        return opener, "proxy", _state["proxy_addr"]


def _patched_urlopen(url, *args, **kwargs):
    try:
        host = _host_of(url)
    except Exception:
        host = ""
    if not _routable(host):
        return _orig_urlopen(url, *args, **kwargs)

    started = time.time()
    opener, route, addr = _effective_route()
    if opener is not None:
        auth = getattr(opener, "proxy_auth_header", "")
        if auth and hasattr(url, "add_header"):
            # 预置代理认证：https 由 do_open 转入 CONNECT tunnel_headers，http 随头透传
            url.add_header("Proxy-authorization", auth)
        try:
            resp = opener.open(url, *args, **kwargs)
            with _lock:
                _state["consecutive_failures"] = 0
                _state["stats"]["requests_proxied"] += 1
            _log_egress(url, "proxy", addr, started)
            return resp
        except urllib.error.HTTPError:
            # 代理通道正常、业务层 HTTP 应答（含茶姬 4xx/5xx）——不属于代理故障，不回退
            with _lock:
                _state["consecutive_failures"] = 0
                _state["stats"]["requests_proxied"] += 1
            raise
        except Exception as e:
            _on_proxy_failure(addr, e)
            with _lock:
                _state["stats"]["fallback_retries"] += 1
            # 落到直连重试（调用方无感；直连也失败则异常如实上抛，与未启用代理时一致）
    resp = _orig_urlopen(url, *args, **kwargs)
    with _lock:
        _state["stats"]["requests_direct"] += 1
    _log_egress(url, "direct", addr if route == "proxy" else "", started)
    return resp


def _on_proxy_failure(addr: str, exc: Exception) -> None:
    degrade = False
    with _lock:
        _state["consecutive_failures"] += 1
        n = _state["consecutive_failures"]
        degrade = n >= _PROXY_AUTH_FAIL_THRESHOLD and _state["route"] == "proxy"
        err = f"{type(exc).__name__}: {exc}"[:200]
        _state["last_error"] = err
    if degrade:
        _set_route("direct", reason=f"代理连续失败 {n} 次（{err}），自动回退服务器直连",
                   level="WARN")
    _wake.set()   # 立刻唤醒监控线程轮换/复检


def install() -> bool:
    """幂等安装 urlopen 补丁（主 API 与收银台进程各自调用）。"""
    global _installed, _orig_urlopen
    with _lock:
        if _installed:
            return False
        load_config()
        _orig_urlopen = urllib.request.urlopen
        urllib.request.urlopen = _patched_urlopen
        _installed = True
        logger.info("net_proxy urlopen 补丁已安装（route_domains=%s）", _cfg.get("route_domains"))
        return True


def uninstall() -> None:
    """测试用：还原 urlopen。"""
    global _installed
    with _lock:
        if _installed and _orig_urlopen is not None:
            urllib.request.urlopen = _orig_urlopen
        _installed = False


# ---------------- 关键请求出口留痕（oplog proxy.egress） ----------------

def _label_of(url) -> str:
    try:
        path = urllib.parse.urlsplit(str(getattr(url, "full_url", url))).path
    except Exception:
        return ""
    for token, label in _KEY_PATH_RULES:
        if token.lower() in path.lower():
            return label
    return ""


def _log_egress(url, route: str, proxy_addr: str, started: float) -> None:
    label = _label_of(url)
    if not label:
        return
    with _lock:
        if route == "proxy":
            ip, region, mainland = (_state["proxy_egress_ip"], _state["proxy_egress_region"],
                                    _state["proxy_egress_mainland"])
        else:
            ip, region, mainland = (_state["direct_ip"], _state["direct_region"],
                                    _state["direct_mainland"])
        key = (label, route, ip)
        last = _egress_log_last.get(key, 0.0)
        if time.time() - last < _EGRESS_LOG_MIN_INTERVAL:
            return
        _egress_log_last[key] = time.time()
    log_op("proxy.egress", level="INFO", actor="system", target=label,
           params={"route": route, "proxy": proxy_addr or "-", "egress_ip": ip or "-",
                   "egress_region": region or "-", "egress_mainland": mainland,
                   "duration_ms": int((time.time() - started) * 1000)})


# ---------------- 状态机 / 检测周期 ----------------

def _set_route(route: str, *, reason: str = "", level: str = "INFO",
               proxy_meta: dict | None = None, egress: dict | None = None) -> None:
    """切换实际出口并留痕（proxy↔direct、代理轮换同址不复读）。"""
    egress = egress or {}
    with _lock:
        old = _state["route"]
        old_addr = _state["proxy_addr"]
        if proxy_meta:
            _state["proxy_addr"] = proxy_meta.get("addr", "")
            _state["proxy_protocol"] = proxy_meta.get("protocol", "http")
            _state["proxy_obtained_at"] = time.time()
            eg = egress or {}
            _state["proxy_egress_ip"] = eg.get("ip", "")
            _state["proxy_egress_region"] = eg.get("region", "")
            _state["proxy_egress_source"] = eg.get("source", "")
            _state["proxy_egress_mainland"] = eg.get("mainland")
        _state["route"] = route
        new_addr = _state["proxy_addr"]
        if old == route and old_addr == new_addr:
            return
    log_op("proxy.switch", level=level, actor="system",
           params={"from": f"{old}:{old_addr}" if old == "proxy" else old,
                   "to": f"{route}:{new_addr}" if route == "proxy" else route,
                   "reason": reason[:200], "egress_ip": egress.get("ip", "")})
    _publish_status()


def _probe_direct(force: bool = False) -> None:
    now = time.time()
    with _lock:
        fresh = now - _state["direct_checked_at"] < (10 if force else DIRECT_PROBE_INTERVAL)
        if fresh and _state["direct_ip"]:
            return
    info = probe_egress(None)
    with _lock:
        if info:
            _state["direct_ip"] = info["ip"]
            _state["direct_region"] = info["region"]
            _state["direct_mainland"] = info["mainland"]
        _state["direct_checked_at"] = now


def _check_once(force: bool = False) -> None:
    """一个完整检测周期（监控线程 / refresh_now / 保存后即检共用；_refresh_lock 串行）。"""
    with _refresh_lock:
        maybe_reload_config()
        with _lock:
            cfg = dict(_cfg)
        _probe_direct(force=force)
        if not cfg["enabled"]:
            _set_route("direct", reason="代理未启用" if _state["route"] == "proxy" else "")
            with _lock:
                _state["last_check_at"] = time.time()
            _publish_status()
            return

        # 组装候选：manual 固定单条；api 按需提取（无代理/临期/带故障 → 强制取新）
        candidates: list[dict] = []
        with _lock:
            age = time.time() - _state["proxy_obtained_at"]
            need_new = (cfg["mode"] == "api" and
                        (force or not _state["proxy_addr"] or _state["route"] != "proxy"
                         or _state["consecutive_failures"] > 0
                         or age > max(30, cfg["ttl_seconds"] - cfg["check_interval"] - 5)))
        if cfg["mode"] == "manual":
            candidates = [{"host": cfg["manual_host"], "port": cfg["manual_port"],
                           "user": cfg.get("username") or "", "pwd": cfg.get("password") or "",
                           "real_ip": "", "area": ""}]
        elif need_new:
            try:
                fetched = fetch_api_proxies(cfg["api_url"], cfg["api_format"])
                candidates = fetched
                meta = fetched[0]
                log_op("proxy.fetch", actor="system",
                       params={"count": len(fetched),
                               "first": f"{meta['host']}:{meta['port']}",
                               "real_ip": meta.get("real_ip", ""), "area": meta.get("area", "")})
            except Exception as e:
                with _lock:
                    _state["last_error"] = f"提取失败 {type(e).__name__}: {e}"[:200]
        else:
            with _lock:
                if _state["proxy_addr"]:
                    candidates = [{"host": _state["proxy_addr"].rsplit(":", 1)[0],
                                   "port": int(_state["proxy_addr"].rsplit(":", 1)[1]),
                                   "user": cfg.get("username") or "",
                                   "pwd": cfg.get("password") or "",
                                   "real_ip": _state["proxy_egress_ip"],
                                   "area": _state["proxy_egress_region"]}]

        best = None
        last_err = ""
        for cand in candidates[:_CANDIDATE_LIMIT]:
            ok, egress, err = _try_candidate(cand, cfg)
            if ok:
                best = (cand, egress)
                break
            last_err = err
        if best:
            cand, egress = best
            with _lock:
                rotated = _state["proxy_addr"] != f"{cand['host']}:{cand['port']}"
                if rotated:
                    _state["stats"]["rotations"] += 1
            _set_route("proxy",
                       reason="代理轮换" if rotated else "代理恢复可用",
                       proxy_meta={"addr": f"{cand['host']}:{cand['port']}",
                                   "protocol": cfg["protocol"]},
                       egress=egress)
            with _lock:
                _state["consecutive_failures"] = 0
                _state["last_error"] = ""
        else:
            if _state["route"] == "proxy":
                _set_route("direct", level="WARN",
                           reason=f"全部候选不可用，回退服务器直连（{last_err[:120]}）")
            with _lock:
                _state["last_error"] = _state["last_error"] or last_err[:200]
        with _lock:
            _state["last_check_at"] = time.time()
        _publish_status()


def _try_candidate(cand: dict, cfg: dict):
    """实测单个候选：经代理访问茶姬网关（HTTP 应答即活）+ 出口信息（服务商元数据优先）。
    返回 (ok, egress, err)。"""
    addr = f"{cand['host']}:{cand['port']}"
    opener = _proxy_opener(addr, cfg["protocol"],
                           cand.get("user") or cfg.get("username") or "",
                           cand.get("pwd") or cfg.get("password") or "")
    t0 = time.time()
    try:
        req = urllib.request.Request(HEALTHCHECK_URL,
                                     headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        status, _ = _fetch_via(opener, req, timeout=8.0)
        alive = bool(status)
    except Exception as e:
        return False, None, f"{addr} {type(e).__name__}: {e}"[:200]

    egress: dict = {}
    if cand.get("real_ip"):
        tokens = [t for t in re.split(r"[-\s]+", str(cand.get("area") or "").strip()) if t]
        region = " ".join(tokens) if tokens and tokens[0] == "中国" else " ".join(["中国"] + tokens)
        egress = {"ip": cand["real_ip"], "region": region,
                  "mainland": _is_mainland_region(region), "source": "provider"}
    else:
        info = probe_egress(opener, timeout=5.0)
        if info:
            egress = {"ip": info["ip"], "region": info["region"],
                      "mainland": info["mainland"], "source": "echo"}
        else:
            egress = {"ip": "", "region": "", "mainland": None, "source": ""}
    if cfg.get("require_mainland") and egress.get("mainland") is False:
        return False, egress, (f"{addr} 出口非大陆：{egress.get('ip')} "
                               f"({egress.get('region')})")[:200]
    egress["checked_in"] = round(time.time() - t0, 2)
    return True, egress, ""


# ---------------- 状态快照 / SSE ----------------

def _status_label() -> str:
    with _lock:
        enabled = _cfg.get("enabled")
        route = _state["route"]
        has_src = ((_cfg.get("mode") == "api" and _cfg.get("api_url"))
                   or (_cfg.get("mode") == "manual" and _cfg.get("manual_host")))
    if not enabled:
        return "disabled"
    if route == "proxy":
        return "proxy_active"
    return "fallback_direct" if has_src else "not_configured"


_STATUS_TEXT = {
    "disabled": "未启用（服务器直连）",
    "not_configured": "已启用但缺少代理来源（直连）",
    "proxy_active": "代理生效中",
    "fallback_direct": "代理不可用，已回退服务器直连",
}


def status_snapshot() -> dict:
    with _lock:
        cfg = dict(_cfg)
        st = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _state.items()}
    label = _status_label()
    warning = ""
    if label in ("disabled", "fallback_direct", "not_configured"):
        if st["direct_mainland"] is False:
            warning = (f"当前出口为服务器直连且非大陆 IP（{st['direct_ip'] or '未知'} "
                       f"{st['direct_region'] or ''}），账号相关请求正从香港/海外发出")
    elif st["proxy_egress_mainland"] is False:
        warning = f"代理出口非大陆：{st['proxy_egress_ip']} {st['proxy_egress_region']}"
    elif st["proxy_egress_mainland"] is None and st["proxy_egress_ip"]:
        warning = "代理出口归属地未知（服务商未提供且探测端点不可达）"
    return {
        "status": label,
        "status_text": _STATUS_TEXT.get(label, label),
        "warning": warning,
        "route": st["route"],
        "proxy": {
            "addr": st["proxy_addr"], "protocol": st["proxy_protocol"],
            "obtained_at": st["proxy_obtained_at"],
            "age_seconds": round(time.time() - st["proxy_obtained_at"], 1) if st["proxy_obtained_at"] else None,
            "egress_ip": st["proxy_egress_ip"], "egress_region": st["proxy_egress_region"],
            "egress_source": st["proxy_egress_source"], "egress_mainland": st["proxy_egress_mainland"],
        },
        "direct": {
            "ip": st["direct_ip"], "region": st["direct_region"],
            "mainland": st["direct_mainland"], "checked_at": st["direct_checked_at"],
        },
        "last_check_at": st["last_check_at"],
        "last_error": st["last_error"],
        "config_summary": {"enabled": cfg["enabled"], "mode": cfg["mode"],
                           "protocol": cfg["protocol"],
                           "ttl_seconds": cfg["ttl_seconds"],
                           "check_interval": cfg["check_interval"],
                           "route_domains": cfg["route_domains"],
                           "require_mainland": cfg["require_mainland"]},
        "stats": st["stats"],
        "server_time": int(time.time() * 1000),
    }


_latest_lock = threading.Lock()
_latest_status: dict | None = None


def latest_status() -> dict | None:
    with _latest_lock:
        return _latest_status


def _publish_status() -> None:
    global _latest_status
    snap = status_snapshot()
    with _latest_lock:
        _latest_status = snap
    try:
        events_bus.publish("proxy", "status", snap)
    except Exception:
        logger.debug("proxy 状态 SSE 推送失败（无人订阅属正常）", exc_info=True)


def refresh_now() -> dict:
    """立即执行一个检测周期（API「立即检测」按钮 / 保存后即检）。"""
    _check_once(force=True)
    return status_snapshot()


# ---------------- 干跑诊断（不落状态） ----------------

def run_dry_test(draft: dict | None = None) -> dict:
    """对草稿配置（缺省用当前配置）做全链路干跑：提取→隧道→出口→茶姬可达→直连基线。"""
    try:
        cfg = validate_config({**_cfg, **(draft or {})})
    except ValueError as e:
        return {"ok": False, "steps": [{"step": "validate", "ok": False, "detail": str(e)}]}
    steps: list[dict] = []

    def add(step, ok, detail):
        steps.append({"step": step, "ok": bool(ok), "detail": str(detail)[:400]})

    # 1) 提取/组装候选
    if cfg["mode"] == "api":
        try:
            cands = fetch_api_proxies(cfg["api_url"], cfg["api_format"])
            add("extract", True, f"提取到 {len(cands)} 个代理："
                + ", ".join(f"{c['host']}:{c['port']}" for c in cands[:3])
                + ("（realIp: %s %s）" % (cands[0].get("real_ip"), cands[0].get("area"))
                   if cands[0].get("real_ip") else ""))
        except Exception as e:
            add("extract", False, f"{type(e).__name__}: {e}")
            cands = []
    else:
        cands = [{"host": cfg["manual_host"], "port": cfg["manual_port"],
                  "user": cfg.get("username") or "", "pwd": cfg.get("password") or ""}]
        add("extract", True, f"固定代理 {cands[0]['host']}:{cands[0]['port']}")

    # 2) 隧道 CONNECT 探测（把服务商 614 domain is black / 静默丢弃原样透出）
    target = urllib.parse.urlsplit(HEALTHCHECK_URL)
    probe_target = f"{target.hostname}:{target.port or 443}"
    for c in cands[:_CANDIDATE_LIMIT]:
        line = _connect_probe(c["host"], c["port"], probe_target, timeout=6.0)
        ok = line.startswith("HTTP/1.1 200") or line.startswith("HTTP/1.0 200")
        add("tunnel", ok, f"CONNECT {probe_target} via {c['host']}:{c['port']} → "
                          f"{line or '无应答（代理静默丢弃，疑似服务商未授权该域名）'}")
        if ok:
            break

    # 3) 全链路实测 + 出口信息
    egress_info = None
    for c in cands[:_CANDIDATE_LIMIT]:
        ok, egress, err = _try_candidate(c, cfg)
        if ok:
            egress_info = egress
            add("request", True, f"经代理访问 {HEALTHCHECK_URL} 成功；出口 "
                f"{egress.get('ip') or '未知'} {egress.get('region') or ''} "
                f"（来源: {egress.get('source') or '-'}，用时 {egress.get('checked_in')}s）")
            break
        add("request", False, err)
    if cfg.get("require_mainland") and egress_info:
        mainland = egress_info.get("mainland")
        add("mainland", mainland is not False,
            "大陆出口 ✓" if mainland else ("非大陆出口 ✗" if mainland is False else "归属未知（按放行处理）"))

    # 4) 直连基线（对照）
    info = probe_egress(None)
    if info:
        add("direct", True, f"服务器直连出口 {info['ip']} {info['region']}"
                            f"（大陆: {info['mainland']}）")
    else:
        add("direct", False, "直连出口探测失败（echo 端点不可达）")

    ok = any(s["step"] == "request" and s["ok"] for s in steps) if cands else False
    return {"ok": ok, "steps": steps, "tested_at": int(time.time() * 1000)}


# ---------------- 监控线程 ----------------

def start_proxy_monitor() -> None:
    if MONITOR_INTERVAL <= 0:
        logger.info("net_proxy 监控线程未启用（CHAGEE_PROXY_CHECK_INTERVAL_SECONDS=0）")
        return

    def _loop():
        # 启动即做一轮：状态页连上就有直连基线，不必等满首个 interval
        try:
            _check_once(force=False)
        except Exception:
            logger.warning("net_proxy 启动首轮检测异常（下轮重试）", exc_info=True)
        while True:
            interval = 30.0
            try:
                with _lock:
                    interval = max(10.0, float(_cfg.get("check_interval") or 30))
            except Exception:
                pass
            woke = _wake.wait(interval)
            _wake.clear()
            try:
                _check_once(force=woke)
            except Exception:
                logger.warning("net_proxy 检测周期异常（下轮重试）", exc_info=True)
            heartbeat("proxy-monitor", extra=_hb_extra())

    threading.Thread(target=_loop, daemon=True, name="proxy-monitor").start()
    logger.info("net_proxy 监控线程已启动（%ss）", MONITOR_INTERVAL)


def _hb_extra() -> dict:
    snap = status_snapshot()
    return {"status": snap["status"], "route": snap["route"],
            "proxy": snap["proxy"]["addr"] or "-", "egress": snap["proxy"]["egress_ip"] or "-"}


def start_net_proxy() -> None:
    """app startup 统一入口：装补丁 + 起监控（主 API 与收银台进程各自调用）。"""
    install()
    _publish_status()
    start_proxy_monitor()


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_config()
    print(json.dumps(run_dry_test(), ensure_ascii=False, indent=2))
