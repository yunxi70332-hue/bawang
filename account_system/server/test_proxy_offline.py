"""网络出口管理（services/net_proxy + routers/settings）离线测试（零网络、零生产文件写入）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && ..\\..\\.venv_verify\\Scripts\\python.exe test_proxy_offline.py
    （或 pytest test_proxy_offline.py -q；单独跑，不与其它套件合跑——engine 互踩是既定约定）

覆盖（编号即用例名前缀，main() 按字典序执行）：
  01 parse_proxy_payload：海量IP 实测 JSON 形态 / 通用 JSON 字段名变体 / TXT 多分隔符与
     ip:port:user:pass / 垃圾行忽略 / 声明 json 实回 txt 的兜底解析
  02 大陆判定与 echo 解析：_is_mainland_region（中国省市区/港澳台/海外/空）+ _parse_echo
     （ipip 文本 / ip.sb / ipinfo JSON / 无 IP 文本）
  03 validate_config：规范化合并 / 非法 mode/format/protocol / api 缺 url / manual 缺
     host:port / route_domains 空 —— 全部 ValueError 中文因
  04 配置保存与脱敏：save_config 落盘 + 密码 *** 掩码回显 + *** 哨兵合并保留旧密码
  05 urlopen 补丁路由：命中 route_domains 且代理健康→经代理不走直连；代理异常→本次直连
     重试（stats.fallback_retries）且非命中域名原样直连；代理回 HTTPError→如实上抛不回退
  06 失败降级阈值：连续 2 次代理失败 → route 切 direct + oplog proxy.switch WARN
  07 关键请求出口留痕：proxy.egress 同(操作,路由,IP)限频一条 / 不同操作各一条
  08 _check_once 状态机：候选可用→proxy_active（realIp 元数据入口）→ 候选全败→
     fallback_direct（WARN switch）→ enabled=false→disabled；直连非大陆告警文案
  09 /api/ops/settings/proxy* 端点：viewer 403 / GET 掩码 / PUT 非法 422 / PUT 合法
     manual 配置 + 状态刷新 / POST test 干跑步骤 / GET logs 只见 proxy.* 前缀
  10 SSE topic 权限注册：proxy → settings:manage（routers/events 白名单含本 topic）

要点：
  - CHAGEE_PROXY_CONFIG / CHAGEE_OPLOG_DB 指向 data/test_*：不碰生产 proxy_config.json 与 oplog.db
  - CHAGEE_PROXY_CHECK_INTERVAL_SECONDS=0：监控线程不启动（直调 _check_once）
  - 补丁用例自己 install/uninstall，收尾还原 urllib.request.urlopen
  - 全部外呼（fetch/echo/候选实测/CONNECT）以桩替换，零真实网络
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

os.environ["CHAGEE_PROXY_CHECK_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_LOG_MONITOR_INTERVAL_SECONDS"] = "0"

import database  # noqa: E402

TEST_DIR = database.DATA_DIR
os.environ["CHAGEE_PROXY_CONFIG"] = os.path.join(TEST_DIR, "test_proxy_config.json")
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(TEST_DIR, "test_proxy_oplog.db")
for _p in (os.environ["CHAGEE_PROXY_CONFIG"], os.environ["CHAGEE_OPLOG_DB"]):
    if os.path.exists(_p):
        os.remove(_p)

database.DB_PATH = os.path.join(TEST_DIR, "test_proxy_app.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    _p = database.DB_PATH + _suffix
    if os.path.exists(_p):
        os.remove(_p)
from sqlalchemy import create_engine  # noqa: E402

database.engine = create_engine(
    f"sqlite:///{database.DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
database.SessionLocal = database.sessionmaker(
    bind=database.engine, autoflush=False, autocommit=False)

import oplog  # noqa: E402
oplog.init_oplog(process="proxy-offline-test")

import seed  # noqa: E402
seed.init_db()

from services import net_proxy  # noqa: E402
import urllib.request  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.settings as settings_router  # noqa: E402
import routers.events as events_router  # noqa: E402
from models import Role, SystemUser  # noqa: E402
from security import create_token, hash_password  # noqa: E402
from oplog import query_logs  # noqa: E402


def _reset_net_proxy_state():
    """用例间复位：直连状态、失败计数、opener 缓存、egress 限频表；保留已保存配置文件。"""
    net_proxy.load_config()
    net_proxy._openers.clear()
    net_proxy._egress_log_last.clear()
    net_proxy._state.update({
        "route": "direct", "proxy_addr": "", "proxy_protocol": "http",
        "proxy_obtained_at": 0.0, "proxy_egress_ip": "", "proxy_egress_region": "",
        "proxy_egress_source": "", "proxy_egress_mainland": None,
        "direct_ip": "", "direct_region": "", "direct_mainland": None,
        "direct_checked_at": 0.0, "last_check_at": 0.0, "last_error": "",
        "consecutive_failures": 0,
        "stats": {"requests_proxied": 0, "requests_direct": 0,
                  "fallback_retries": 0, "rotations": 0},
    })


class _StubResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubOpener:
    """可编程桩 opener：mode=ok 返回应答；mode=err 抛网络异常；mode=http 抛 HTTPError。"""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = []
        self.proxy_auth_header = ""

    def open(self, url, *a, **k):
        self.calls.append(str(getattr(url, "full_url", url)))
        if self.mode == "err":
            raise OSError("stub proxy conn refused")
        if self.mode == "http":
            raise urllib.error.HTTPError(
                str(getattr(url, "full_url", url)), 502, "stub bad gateway", {}, None)
        return _StubResponse()


# ---------------- 01 提取响应解析 ----------------

def test_01_parse_proxy_payload():
    hailiang = ('{"serialNo":"x","code":0,"data":[{"realIp":"120.238.216.123","pid":21,'
                '"cid":247,"area":"广东-佛山","ip":"36.156.10.42","port":16671}]}')
    items = net_proxy.parse_proxy_payload(hailiang, "json")
    assert len(items) == 1, items
    assert items[0]["host"] == "36.156.10.42" and items[0]["port"] == 16671
    assert items[0]["real_ip"] == "120.238.216.123" and items[0]["area"] == "广东-佛山"

    generic = '{"code":0,"data":[{"proxyIp":"1.2.3.4","proxy_port":"8080"},{"proxy":"5.6.7.8:1080"}]}'
    items = net_proxy.parse_proxy_payload(generic, "json")
    assert {(i["host"], i["port"]) for i in items} == {("1.2.3.4", 8080), ("5.6.7.8", 1080)}

    txt = "9.9.9.9:3128, 10.10.10.10:8888:u1:p1\r\njunk-line\r\n11.11.11.11:80"
    items = net_proxy.parse_proxy_payload(txt, "txt")
    assert [(i["host"], i["port"], i["user"]) for i in items] == [
        ("9.9.9.9", 3128, ""), ("10.10.10.10", 8888, "u1"), ("11.11.11.11", 80, "")]

    # 声明 json 但服务商圈 txt：兜底走 txt 解析而非抛错
    assert len(net_proxy.parse_proxy_payload("1.2.3.4:8080", "json")) == 1
    # 纯垃圾：无解析结果不抛
    assert net_proxy.parse_proxy_payload("对不起，您使用的IP地址不在白名单内", "txt") == []
    print("01 parse_proxy_payload OK")


# ---------------- 02 大陆判定 / echo 解析 ----------------

def test_02_region_and_echo():
    assert net_proxy._is_mainland_region("中国 广东 深圳 电信") is True
    assert net_proxy._is_mainland_region("CN China Guangdong") is True
    assert net_proxy._is_mainland_region("中国 香港") is False
    assert net_proxy._is_mainland_region("Hong Kong") is False
    assert net_proxy._is_mainland_region("United States") is False
    assert net_proxy._is_mainland_region("") is None

    r = net_proxy._parse_echo("https://myip.ipip.net",
                              "当前 IP：180.153.160.45  来自于：中国 上海 上海  电信")
    assert r["ip"] == "180.153.160.45" and r["mainland"] is True and "上海" in r["region"]

    r = net_proxy._parse_echo("https://api.ip.sb/geoip",
                              '{"ip":"8.8.8.8","country":"United States","country_code":"US","region":"CA"}')
    assert r["ip"] == "8.8.8.8" and r["mainland"] is False

    r = net_proxy._parse_echo("https://ipinfo.io/json",
                              '{"ip":"1.2.3.4","country":"CN","region":"Hong Kong"}')
    assert r["mainland"] is False

    assert net_proxy._parse_echo("https://x/", "no ip here") is None
    print("02 region/echo OK")


# ---------------- 03 配置校验 ----------------

def test_03_validate_config():
    ok = net_proxy.validate_config({"enabled": True, "mode": "api",
                                    "api_url": "https://api.hailiangip.com:8522/x?d=0",
                                    "ttl_seconds": 60, "check_interval": 15})
    assert ok["protocol"] == "http" and ok["enabled"] is True

    for bad in (
        {"mode": "vip"},
        {"api_format": "yaml"},
        {"protocol": "socks4"},
        {"mode": "api", "api_url": ""},
        {"mode": "manual", "manual_host": "", "manual_port": 0},
        {"route_domains": []},
        {"ttl_seconds": "abc"},
    ):
        try:
            net_proxy.validate_config(bad)
            raise AssertionError(f"应当 ValueError: {bad}")
        except ValueError:
            pass
    print("03 validate_config OK")


# ---------------- 04 配置保存 / 掩码 ----------------

def test_04_save_and_mask():
    _reset_net_proxy_state()
    saved = net_proxy.save_config({"enabled": False, "mode": "manual", "manual_host": "10.0.0.8",
                                   "manual_port": 3128, "username": "u", "password": "Secret@1"},
                                  actor="tester")
    assert saved["manual_port"] == 3128
    with open(os.environ["CHAGEE_PROXY_CONFIG"], encoding="utf-8") as f:
        assert json.load(f)["password"] == "Secret@1"      # 落盘留明文（本机敏感文件）
    masked = net_proxy.masked_config()
    assert masked["password"] == "***" and masked["username"] == "u"

    # *** 哨兵合并：改其它字段不动密码
    merged = settings_router._merge_saved_password(
        {"password": "***", "manual_port": 1080})
    net_proxy.save_config(merged, actor="tester")
    with open(os.environ["CHAGEE_PROXY_CONFIG"], encoding="utf-8") as f:
        assert json.load(f)["password"] == "Secret@1"
    print("04 save/mask OK")


# ---------------- 05 urlopen 补丁路由 ----------------

def test_05_patch_routing():
    _reset_net_proxy_state()
    calls = {"direct": []}

    def fake_direct(url, *a, **k):
        calls["direct"].append(str(getattr(url, "full_url", url)))
        return _StubResponse()

    net_proxy.install()
    real_orig, net_proxy._orig_urlopen = net_proxy._orig_urlopen, fake_direct
    try:
        net_proxy.save_config({"enabled": True, "mode": "manual", "manual_host": "10.0.0.8",
                               "manual_port": 3128, "username": "", "password": ""})
        net_proxy._state.update({"route": "proxy", "proxy_addr": "10.0.0.8:3128",
                                 "proxy_protocol": "http"})
        key = "http|10.0.0.8:3128||"
        stub = _StubOpener("ok")
        net_proxy._openers[key] = stub

        req = urllib.request.Request("https://gw.chagee.com/user-client/customer/userInfo/query")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        assert len(stub.calls) == 1 and not calls["direct"], "应走代理且不走直连"
        assert net_proxy._state["stats"]["requests_proxied"] == 1

        # 非命中域名：原样直连，代理零调用
        n0 = len(stub.calls)
        urllib.request.urlopen("https://api.hailiangip.com:8522/get", timeout=5)
        assert len(stub.calls) == n0 and len(calls["direct"]) == 1

        # 代理网络级异常 → 本次直连重试成功（调用方无感）+ 计数
        net_proxy._openers[key] = _StubOpener("err")
        req2 = urllib.request.Request("https://gj-api.bwcj.com/encrypt-server/x")
        with urllib.request.urlopen(req2, timeout=5) as r:
            assert r.status == 200
        assert net_proxy._state["stats"]["fallback_retries"] == 1
        assert calls["direct"][-1].endswith("/encrypt-server/x")
        assert net_proxy._state["consecutive_failures"] == 1 and net_proxy._state["route"] == "proxy"

        # 代理回 HTTPError（业务层应答）：如实上抛，不直连重试
        net_proxy._openers[key] = _StubOpener("http")
        n_direct = len(calls["direct"])
        try:
            urllib.request.urlopen(
                urllib.request.Request("https://gw.chagee.com/x/order"), timeout=5)
            raise AssertionError("HTTPError 应上抛")
        except urllib.error.HTTPError as e:
            assert e.code == 502
        assert len(calls["direct"]) == n_direct
    finally:
        net_proxy._orig_urlopen = real_orig
        net_proxy.uninstall()
        net_proxy._state["route"] = "direct"
    print("05 patch routing OK")


# ---------------- 06 失败降级阈值 ----------------

def test_06_failure_threshold():
    _reset_net_proxy_state()
    net_proxy._state.update({"route": "proxy", "proxy_addr": "10.0.0.8:3128"})
    net_proxy._on_proxy_failure("10.0.0.8:3128", OSError("timeout-1"))
    assert net_proxy._state["route"] == "proxy", "单次失败不降级"
    net_proxy._on_proxy_failure("10.0.0.8:3128", OSError("timeout-2"))
    assert net_proxy._state["route"] == "direct", "连续两次应降级直连"
    rows = query_logs(action="proxy.switch")["items"]
    assert rows and rows[0]["level"] == "WARN" and "回退" in rows[0]["params"]
    print("06 failure threshold OK")


# ---------------- 07 关键请求出口留痕限频 ----------------

def test_07_egress_log_ratelimit():
    _reset_net_proxy_state()
    net_proxy._state.update({"direct_ip": "180.153.160.45", "direct_mainland": True})
    before = query_logs(action="proxy.egress")["total"]
    url = urllib.request.Request("https://gw.chagee.com/chagee-biz-trade-web/trade-web/order/create")
    net_proxy._log_egress(url, "direct", "", 1.0)
    net_proxy._log_egress(url, "direct", "", 2.0)   # 同组合限频内 → 不落
    other = urllib.request.Request(
        "https://gw.chagee.com/user-client/message/send")
    net_proxy._log_egress(other, "direct", "", 3.0)  # 不同操作 → 落
    items = query_logs(action="proxy.egress")["items"]
    assert len(items) == before + 2, items
    labels = {i["target"] for i in items[:2]}
    assert "下单/取餐" in labels and "发送短信验证码" in labels
    print("07 egress log ratelimit OK")


# ---------------- 08 _check_once 状态机 ----------------

def test_08_check_once_cycle():
    _reset_net_proxy_state()
    net_proxy.save_config({"enabled": True, "mode": "api",
                           "api_url": "https://stub.example/extract"}, actor="t")
    fake_cand = {"host": "36.156.10.42", "port": 16671, "user": "", "pwd": "",
                 "real_ip": "120.238.216.123", "area": "广东-佛山"}
    net_proxy.fetch_api_proxies = lambda *a, **k: [dict(fake_cand)]
    net_proxy._fetch_via = lambda open_obj, url, timeout: (200, "{}")   # 候选健康检查全通
    net_proxy.probe_egress = lambda opener=None, timeout=6: {
        "ip": "180.153.160.45", "region": "中国 上海", "mainland": True, "endpoint": "stub"}

    net_proxy._check_once(force=True)
    snap = net_proxy.status_snapshot()
    assert snap["status"] == "proxy_active" and snap["route"] == "proxy"
    assert snap["proxy"]["addr"] == "36.156.10.42:16671"
    assert snap["proxy"]["egress_ip"] == "120.238.216.123"        # 服务商 realIp 元数据
    assert snap["proxy"]["egress_source"] == "provider"
    assert snap["proxy"]["egress_mainland"] is True
    assert query_logs(action="proxy.fetch")["total"] >= 1

    # 候选全败（健康检查网络级异常）→ 回退直连 + WARN switch
    def _dead(*a, **k):
        raise OSError("proxy dead")

    net_proxy._fetch_via = _dead
    net_proxy._check_once(force=True)
    snap = net_proxy.status_snapshot()
    assert snap["status"] == "fallback_direct" and snap["route"] == "direct"
    assert "36.156.10.42" in snap["last_error"]
    rows = query_logs(action="proxy.switch")["items"]
    assert any(r["level"] == "WARN" and "回退" in r["params"] for r in rows)

    # 直连非大陆（香港部署） → 快照带警示文案
    net_proxy._state.update({"direct_ip": "8.8.8.8", "direct_region": "Hong Kong",
                             "direct_mainland": False, "direct_checked_at": time.time()})
    snap = net_proxy.status_snapshot()
    assert "非大陆" in snap["warning"]

    # 停用 → disabled
    net_proxy.save_config({"enabled": False}, actor="t")
    net_proxy._check_once(force=True)
    assert net_proxy.status_snapshot()["status"] == "disabled"
    print("08 check_once cycle OK")


# ---------------- 09 settings 路由端点 ----------------

MINI = FastAPI()
MINI.include_router(settings_router.router)
CLIENT = TestClient(MINI)


def _auth(username: str) -> dict:
    with database.SessionLocal() as db:
        user = db.query(SystemUser).filter(SystemUser.username == username).one()
        return {"Authorization": f"Bearer {create_token(user)}"}


def test_09_settings_endpoints():
    _reset_net_proxy_state()
    with database.SessionLocal() as db:
        if not db.query(SystemUser).filter(SystemUser.username == "viewer_px").first():
            viewer_role = db.query(Role).filter(Role.name == "viewer").one()
            db.add(SystemUser(username="viewer_px", display_name="无设置权限",
                              password_hash=hash_password("V@123"), role_id=viewer_role.id))
            db.commit()

    r = CLIENT.get("/api/ops/settings/proxy", headers=_auth("viewer_px"))
    assert r.status_code == 403, r.text
    r = CLIENT.get("/api/ops/settings/proxy")
    assert r.status_code == 401

    r = CLIENT.get("/api/ops/settings/proxy", headers=_auth("admin"))
    assert r.status_code == 200 and "status" in r.json() and "config" in r.json()

    r = CLIENT.put("/api/ops/settings/proxy", headers=_auth("admin"),
                   json={"enabled": True, "mode": "manual", "manual_host": "", "manual_port": 0})
    assert r.status_code == 422

    # 合法 manual 保存：refresh_now 以桩替换避免真实外呼
    net_proxy.refresh_now = lambda: net_proxy.status_snapshot()
    r = CLIENT.put("/api/ops/settings/proxy", headers=_auth("admin"),
                   json={"enabled": True, "mode": "manual", "manual_host": "10.0.0.9",
                         "manual_port": 1080, "protocol": "socks5", "password": "Pw@9"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["manual_host"] == "10.0.0.9" and body["config"]["password"] == "***"
    assert body["status"]["config_summary"]["protocol"] == "socks5"

    # 干跑测试：全链路桩化（提取/CONNECT/候选实测/直连基线）
    net_proxy.fetch_api_proxies = lambda *a, **k: [
        {"host": "36.156.10.9", "port": 1234, "user": "", "pwd": "",
         "real_ip": "1.2.3.4", "area": "广东-广州"}]
    net_proxy._connect_probe = lambda h, p, t, timeout=6: "HTTP/1.1 200 Connection established"
    net_proxy._try_candidate = lambda c, cfg: (True, {"ip": "1.2.3.4", "region": "中国 广东 广州",
                                                      "mainland": True, "source": "provider"}, "")
    r = CLIENT.post("/api/ops/settings/proxy/test", headers=_auth("admin"),
                    json={"mode": "api", "api_url": "https://stub.example/e"})
    assert r.status_code == 200, r.text
    steps = {s["step"]: s["ok"] for s in r.json()["steps"]}
    assert steps.get("extract") and steps.get("tunnel") and steps.get("request") \
        and steps.get("mainland") and steps.get("direct")
    assert r.json()["ok"] is True

    r = CLIENT.get("/api/ops/settings/proxy/logs", headers=_auth("admin"))
    assert r.status_code == 200
    assert r.json()["total"] >= 1
    assert all(i["action"].startswith("proxy.") for i in r.json()["items"])
    print("09 settings endpoints OK")


# ---------------- 10 SSE topic 注册 ----------------

def test_10_events_topic():
    assert events_router._TOPIC_PERMS.get("proxy") == "settings:manage"
    print("10 events topic OK")


def main():
    test_01_parse_proxy_payload()
    test_02_region_and_echo()
    test_03_validate_config()
    test_04_save_and_mask()
    test_05_patch_routing()
    test_06_failure_threshold()
    test_07_egress_log_ratelimit()
    test_08_check_once_cycle()
    test_09_settings_endpoints()
    test_10_events_topic()
    print("\nall proxy offline tests passed")


if __name__ == "__main__":
    main()
