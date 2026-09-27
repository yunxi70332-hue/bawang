#!/usr/bin/env python3
"""env_bootstrap.py — 霸王茶姬逆向工作台环境一键配置/启动/体检/回滚。

用法（项目根目录）：
    python scripts/env_bootstrap.py doctor              # 全链路体检 + 修复建议
    python scripts/env_bootstrap.py setup               # 幂等一键配置（设备侧+宿主侧+探针）
    python scripts/env_bootstrap.py probe               # 只验证（L1 隧道 / L3 游客 / L4 会话）
    python scripts/env_bootstrap.py status              # 在位状态 + 回滚命令
    python scripts/env_bootstrap.py teardown            # 全还原

可选参数：
    --serial 125.109.27.7:56915   指定设备（缺省自动发现/用最近 serial）
    --known 39.174.221.6:56915 ... 追加可尝试连接的已知地址（可多次）
    --no-mitm                      只配设备侧，不启动宿主 mitmdump
    --start-frida                  setup 时顺带拉起设备 frida-server
    --domains gw.chagee.com ...    覆盖默认 hosts 重定向域名集

设计约束：
    - 幂等：所有 setup 步骤先 check 后 do，重复执行安全
    - 纯标准库；adb 用 subprocess 列表参数调用（免疫 Git Bash MSYS 路径转换，E-A1/E-A2）
    - 跨平台：Windows(GitBash/cmd/PowerShell) 与 POSIX 均可运行（进程分离按 OS 分支）
    - 可扩展：Steps 注册表追加 (name, check, apply) 三元组即可
错误对照：skills/chagee-reverse-env/references/error_kb.md（E-A*/E-B*/E-C*/E-D*）
"""

import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAPTURE_DIR = os.path.join(ROOT, "capture")
STATE_FILE = os.path.join(HERE, ".env_bootstrap_state.json")

# ---------------- 配置（集中在此，便于维护） ----------------

CFG = {
    "proxy_port": 8080,             # 原生 SDK 流量（设备全局代理 → reverse → 宿主 mitmdump 常规模式）
    "reverse_port": 8443,           # Dart 流量（hosts→127.0.0.1:443 → reverse → 宿主 mitmdump reverse+SNI）
    "reverse_default_upstream": "https://gj-api.bwcj.com/",
    "proxy_flows": "capture/env_proxy.flows",
    "dart_flows": "capture/env_dart.flows",
    "sni_addon": "scripts/sni_route_addon.py",
    "domains": [
        "gw.chagee.com", "gj-api.bwcj.com", "api-cn.chagee.com", "h5-sea.chagee.com",
    ],
    "hosts_backup": "/data/local/tmp/hosts.bak",
    "ca_pem": "~/.mitmproxy/mitmproxy-ca-cert.pem",
    "ca_dir_device": "/system/etc/security/cacerts",
    "frida_server": "/data/local/tmp/fs1666-arm64",
    "frida_port": 27042,
    "l1_probe_url": "https://gw.chagee.com/",   # L1 只看有无 HTTP 响应（任意状态码）
    "guest_probe": {                             # L3：游客业务探针（cityList）
        "url": "https://gw.chagee.com/chagee-navigation-web/api/navigation/store/cityList",
        "method": "POST", "body": None,
    },
    "last_serials": [                            # 设备 IP 轮换历史，自动重试
        "39.174.221.6:56915", "125.109.27.7:56915",
    ],
}

OK, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


# ---------------- 基础工具 ----------------

def sh(cmd, timeout=30, **kw):
    """subprocess 列表参数执行（不经 shell，天然免疫 MSYS 路径转换）。"""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", **kw)


def adb(args, serial=None, timeout=30):
    cmd = ["adb"] + ([ "-s", serial] if serial else []) + args
    return sh(cmd, timeout=timeout)


def adb_shell(cmdstr, serial, timeout=30):
    return adb(["shell", cmdstr], serial=serial, timeout=timeout)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(d):
    d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(d, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------- 发现：adb / 设备 / mitmdump / openssl ----------------

def find_devices():
    out = adb(["devices"]).stdout or ""
    return [l.split()[0] for l in out.splitlines()
            if l.strip() and not l.startswith("List") and l.split()[-1] == "device"]


def discover_serial(args) -> str:
    """优先 --serial > 状态记忆 > 自动发现 > 已知地址重试连接。"""
    if args.serial:
        r = adb(["connect", args.serial])
        return args.serial if "connected" in (r.stdout or "") else ""
    devs = find_devices()
    if len(devs) == 1:
        return devs[0]
    if len(devs) > 1:
        print(f"[!] 多设备在线 {devs}，请用 --serial 指定"); return ""
    state = load_state()
    for cand in [state.get("serial")] + CFG["last_serials"] + args.known:
        if not cand:
            continue
        r = adb(["connect", cand])
        if "connected" in (r.stdout or "") and find_devices():
            print(f"[*] 重连成功（IP 轮换恢复）: {cand}")
            return cand
    return ""


def find_mitmdump():
    p = shutil.which("mitmdump")
    if p:
        return p
    for pat in (
        os.path.expanduser("~/AppData/Roaming/Python/Python*/Scripts/mitmdump.exe"),
        os.path.expanduser("~/.local/bin/mitmdump"),
        "/usr/local/bin/mitmdump", "/opt/homebrew/bin/mitmdump",
    ):
        for hit in glob.glob(pat):
            return hit
    return ""


def ca_hash_local():
    """openssl subject_hash_old 计算 CA 文件名；无 openssl 返回空。"""
    pem = os.path.expanduser(CFG["ca_pem"])
    if not os.path.exists(pem):
        return "", pem
    o = shutil.which("openssl")
    if not o:
        return "", pem
    r = sh([o, "x509", "-inform", "PEM", "-subject_hash_old", "-in", pem])
    h = (r.stdout or "").strip().splitlines()
    return (h[0] if h else ""), pem


def port_pid_listen(port):
    """宿主端口监听与 PID（匹配任意本地地址；mitmdump 默认绑 0.0.0.0/[::] 而非 127.0.0.1）。"""
    try:
        r = sh(["netstat", "-ano"], timeout=15)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTEN" in parts[-2].upper():
                local = parts[1]
                if local.rsplit(":", 1)[-1] == str(port):  # 0.0.0.0:PORT / [::]:PORT / 127.0.0.1:PORT
                    return int(parts[-1])
    except FileNotFoundError:
        pass
    if shutil.which("ss"):
        try:
            r = sh(["ss", "-tlnp"], timeout=15)
            if r.returncode == 0:
                for line in (r.stdout or "").splitlines():
                    if line.rsplit(":", 1)[-1].split()[0:1] and f":{port} " in line + " ":
                        return -1
        except FileNotFoundError:
            pass
    return None


# ---------------- 体检项（doctor = check 全集） ----------------

class Report:
    def __init__(self):
        self.rows = []
    def add(self, name, st, detail="", hint=""):
        self.rows.append((name, st, detail, hint))
    def show(self, title):
        print(f"\n===== {title} =====")
        w = max(len(r[0]) for r in self.rows) if self.rows else 10
        fails = 0
        for name, st, detail, hint in self.rows:
            mark = {OK: "✔", FAIL: "✘", WARN: "▲", SKIP: "-"}[st]
            print(f"  [{mark}] {name:<{w}}  {detail}")
            if hint:
                print(f"        ↳ {hint}")
            fails += st == FAIL
        print(f"  小计: {len(self.rows)-fails} ok / {fails} fail")
        return fails


def check_serial(serial):
    if not serial:
        return FAIL, "无在线设备", "检查云手机 IP（会轮换）后 --serial <ip:56915>；见 E-A6"
    return OK, serial


def check_root_remount(serial):
    r = adb_shell("id", serial)
    if "uid=0" not in (r.stdout or ""):
        return FAIL, "adb 非 root（id 输出异常）", "需 eng 构建 root 设备"
    return OK, "uid=0"


def check_deps():
    probs = []
    try:
        import Crypto  # noqa
    except ImportError:
        probs.append("pycryptodome 缺失: pip install pycryptodome")
    try:
        import frida  # noqa
        frida_ver = frida.__version__
    except ImportError:
        frida_ver, probs = "?", probs + ["frida 缺失: pip install frida"]
    mitm = find_mitmdump()
    if not mitm:
        probs.append("mitmdump 未找到: pip install --user mitmproxy（入口在 %APPDATA%\\Python\\Python3XX\\Scripts，E-A4）")
    return (OK if not probs else WARN), f"frida={frida_ver} mitmdump={'√' if mitm else '×'}", "; ".join(probs)


def check_frida_match(serial):
    try:
        import frida
        client = frida.__version__
    except ImportError:
        return WARN, "frida client 未装", ""
    r = adb_shell(f"{CFG['frida_server']} --version", serial, timeout=10)
    server = (r.stdout or "").strip()
    if not server:
        return WARN, f"设备 frida-server 未运行（client={client}）", f"启动: adb shell 'nohup {CFG['frida_server']} -l 127.0.0.1:{CFG['frida_port']} &'"
    if server != client:
        return FAIL, f"版本失配 client={client} server={server}", "对齐其一：pip install frida==<server 版本> 或换对应 server（E-A7）"
    return OK, f"client=server={client}"


def check_ca(serial):
    h, pem = ca_hash_local()
    if not h:
        return WARN, "无法计算 CA 哈希（openssl 缺失或 CA 未生成）", "先跑一次 mitmdump 生成 ~/.mitmproxy；装 openssl（E-A4 类）"
    r = adb_shell(f"ls {CFG['ca_dir_device']}/{h}.0", serial)
    return (OK if (r.returncode == 0) else FAIL), f"{h}.0 {'在位' if r.returncode == 0 else '缺失'}", \
        "" if r.returncode == 0 else f"setup 将安装（源 {pem}）"


def check_hosts(serial):
    r = adb_shell("cat /system/etc/hosts", serial)
    present = [d for d in CFG["domains"] if d in (r.stdout or "")]
    missing = [d for d in CFG["domains"] if d not in present]
    st = OK if not missing else (WARN if present else FAIL)
    return st, f"{len(present)}/{len(CFG['domains'])} 域名重定向" + (f" 缺:{missing}" if missing else ""), \
        "" if not missing else "setup 将补齐（先备份 → " + CFG["hosts_backup"] + "）"


def check_proxy(serial):
    r = adb_shell("settings get global http_proxy", serial)
    cur = (r.stdout or "").strip()
    want = f"127.0.0.1:{CFG['proxy_port']}"
    return (OK if cur == want else FAIL), f"当前={cur or 'null'} 期望={want}", "" if cur == want else "setup 将设置"


def check_reverse(serial):
    r = adb(["reverse", "--list"], serial=serial)
    out = (r.stdout or "")
    has = lambda p: any(str(p) in l for l in out.splitlines())
    p1, p2 = CFG["proxy_port"], 443
    st = OK if (has(p1) and has(p2)) else FAIL
    return st, f"reverse: {out.strip().replace(chr(10), ' ') or '无'}", "" if st == OK else "setup 将重建 reverse（断线后遗症，E-A6）"


def check_mitmdumps():
    r1, r2 = port_pid_listen(CFG["proxy_port"]), port_pid_listen(CFG["reverse_port"])
    def desc(pid): return f"pid={pid}" if pid else "未监听"
    st = OK if (r1 and r2) else FAIL
    return st, f":{CFG['proxy_port']} {desc(r1)} | :{CFG['reverse_port']} {desc(r2)}", \
        "" if st == OK else "setup 将启动双 mitmdump（或 --no-mitm 跳过）"


def check_l1(serial):
    r = adb_shell(f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' {CFG['l1_probe_url']}", serial, timeout=20)
    code = (r.stdout or "").strip()
    if code and code != "000":
        return OK, f"HTTP {code}（TLS+HTTP 通）"
    return FAIL, f"code={code or '无输出'}", "隧道断：hosts/reverse/mitmdump 逐层查（E-B1/B2）"


# ---------------- 配置动作（setup = 幂等 apply 全集） ----------------

def apply_hosts(serial):
    adb_shell(f"cp /system/etc/hosts {CFG['hosts_backup']}", serial)
    cur = adb_shell("cat /system/etc/hosts", serial).stdout or ""
    add = [d for d in CFG["domains"] if d not in cur]
    if add:
        echo = "; ".join(f"echo '127.0.0.1 {d}' >> /system/etc/hosts" for d in add)
        adb_shell(echo, serial)
    return f"hosts 备份+新增 {len(add)} 条"


def apply_ca(serial):
    h, pem = ca_hash_local()
    if not h:
        return "跳过（无法计算哈希）"
    if adb_shell(f"ls {CFG['ca_dir_device']}/{h}.0", serial).returncode == 0:
        return "CA 已在位"
    tmp = f"/data/local/tmp/{h}.0"
    adb(["push", pem, tmp], serial=serial)
    adb_shell(f"cp {tmp} {CFG['ca_dir_device']}/{h}.0 && chmod 644 {CFG['ca_dir_device']}/{h}.0 && chown root:root {CFG['ca_dir_device']}/{h}.0", serial)
    return f"CA {h}.0 已安装"


def apply_proxy_reverse(serial):
    """幂等且不破坏既有映射：reverse 已存在（无论指向）只报告，缺失才补建。"""
    out = (adb(["reverse", "--list"], serial=serial).stdout or "")
    notes = []
    for local, remote in ((f"tcp:{CFG['proxy_port']}", f"tcp:{CFG['proxy_port']}"),
                          ("tcp:443", f"tcp:{CFG['reverse_port']}")):
        if any(l in line for line in out.splitlines() for l in [local]):
            notes.append(f"{local} 已存在（保留既有映射）")
        else:
            adb(["reverse", local, remote], serial=serial)
            notes.append(f"{local}→{remote} 已建")
    cur = (adb_shell("settings get global http_proxy", serial).stdout or "").strip()
    if cur != f"127.0.0.1:{CFG['proxy_port']}":
        adb_shell(f"settings put global http_proxy 127.0.0.1:{CFG['proxy_port']}", serial)
        notes.append("全局代理已设")
    else:
        notes.append("全局代理已就绪")
    return "; ".join(notes)


def apply_mitmdumps():
    mitm = find_mitmdump()
    if not mitm:
        return "跳过（mitmdump 缺失）"
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    procs = []
    creation = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS / 0
    kw = dict(creationflags=creation) if os.name == "nt" else dict(start_new_session=True)
    p1 = os.path.join(CAPTURE_DIR, "env_proxy.flows")
    p2 = os.path.join(CAPTURE_DIR, "env_dart.flows")
    if not port_pid_listen(CFG["proxy_port"]):
        procs.append(subprocess.Popen(
            [mitm, "-w", p1, "-p", str(CFG["proxy_port"]), "--set", "block_global=false"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw))
    if not port_pid_listen(CFG["reverse_port"]):
        addon = os.path.join(ROOT, CFG["sni_addon"])
        procs.append(subprocess.Popen(
            [mitm, "--mode", f"reverse:{CFG['reverse_default_upstream']}", "-p", str(CFG["reverse_port"]),
             "--set", "block_global=false", "--set", "keep_host_header",
             "--set", "connection_strategy=lazy", "-w", p2, "-s", addon],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw))
    if procs:
        time.sleep(2.5)
    return f"mitmdump 启动 {len(procs)} 个（pid: {[p.pid for p in procs]}）"


def apply_frida(serial):
    r = adb_shell(f"pidof {os.path.basename(CFG['frida_server'])}", serial)
    if (r.stdout or "").strip():
        return "frida-server 已运行"
    adb_shell(f"nohup {CFG['frida_server']} -l 127.0.0.1:{CFG['frida_port']} >/data/local/tmp/fs1666.log 2>&1 &", serial)
    return "frida-server 已拉起（仅 loopback）"


# ---------------- 探针 ----------------

def probe_l3():
    """游客 cityList（直连生产，验证宿主出网+业务可用）。"""
    import urllib.request
    h = {"user-agent": "Dart/3.6 (dart:io)", "ua": "Dart/2.12 (dart:io)", "avc": "638",
         "tcode": "CHAGEE", "channel": "APP", "os": "android", "aid": "100001",
         "language": "zh_CN", "region": "CN"}
    req = urllib.request.Request(CFG["guest_probe"]["url"], data=b"", headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    n = sum(len(g.get("cityList", [])) for g in resp.get("data") or [])
    return f"errcode={resp.get('errcode')} cities={n}"


def probe_l4():
    sys.path.insert(0, HERE)
    from chagee_client import ChageeClient
    c = ChageeClient(env="release")
    info = c.whoami()
    d = info.get("data") or {}
    return f"errcode={info.get('errcode')} nick={d.get('nickName')}"


# ---------------- 模式 ----------------

def mode_doctor(args):
    rep = Report()
    serial = discover_serial(args)
    rep.add("adb 设备", *check_serial(serial))
    if serial:
        save_state(dict(load_state(), serial=serial))
        rep.add("root", *check_root_remount(serial))
        rep.add("CA 系统库", *check_ca(serial))
        rep.add("hosts 重定向", *check_hosts(serial))
        rep.add("全局代理", *check_proxy(serial))
        rep.add("adb reverse", *check_reverse(serial))
        rep.add("frida 版本匹配", *check_frida_match(serial))
        rep.add("L1 隧道探针", *check_l1(serial))
    rep.add("宿主依赖", *check_deps())
    rep.add("mitmdump 监听", *check_mitmdumps())
    fails = rep.show("doctor 全链路体检")
    if fails:
        print("\n建议: python scripts/env_bootstrap.py setup   （幂等修复全部 FAIL 项）")
    return 0 if not fails else 1


def mode_setup(args):
    serial = discover_serial(args)
    if not serial:
        print("[✘] 无法连接设备"); return 1
    save_state(dict(load_state(), serial=serial))
    print(f"[*] 设备: {serial}")
    r = adb_shell("id", serial)
    if "uid=0" not in (r.stdout or ""):
        print("[✘] 设备非 root，中止"); return 1
    adb(["remount"], serial=serial)
    print("  -", apply_ca(serial))
    print("  -", apply_hosts(serial))
    print("  -", apply_proxy_reverse(serial))
    if not args.no_mitm:
        print("  -", apply_mitmdumps())
    if args.start_frida:
        print("  -", apply_frida(serial))
    print("[*] L1 探针:", check_l1(serial)[1])
    st, det = check_l1(serial)
    print("[✔] 环境就绪" if st == OK else "[✘] 隧道未通，运行 doctor 定位")
    return 0 if st == OK else 1


def mode_probe(args):
    serial = discover_serial(args)
    ok = True
    if serial:
        st, det = check_l1(serial); print(f"[L1 隧道]   {st} {det}"); ok &= st == OK
    else:
        print("[L1 隧道]   SKIP 无设备"); ok = False
    try:
        print(f"[L3 游客]   {OK} {probe_l3()}")
    except Exception as e:
        print(f"[L3 游客]   FAIL {type(e).__name__}: {e}"); ok = False
    try:
        print(f"[L4 会话]   {OK} {probe_l4()}")
    except Exception as e:
        print(f"[L4 会话]   FAIL {type(e).__name__}: {str(e)[:80]}（token 失效需重登，E-D5）"); ok = False
    return 0 if ok else 1


def mode_status(args):
    serial = discover_serial(args)
    print(f"serial={serial or '离线'}  state={load_state().get('serial')}  updated={load_state().get('updated_at')}")
    if serial:
        print("proxy  :", adb_shell("settings get global http_proxy", serial).stdout.strip())
        print("hosts  :", adb_shell("cat /system/etc/hosts", serial).stdout.strip().replace("\n", " | "))
        print("reverse:", (adb(["reverse", "--list"], serial=serial).stdout or "").strip().replace("\n", " "))
        st, det = check_l1(serial); print(f"L1     : {st} {det}")
    print("回滚: python scripts/env_bootstrap.py teardown")


def mode_teardown(args):
    serial = discover_serial(args)
    if not serial:
        print("[!] 设备离线，仅清理宿主侧")
    else:
        adb_shell("settings delete global http_proxy", serial)
        adb_shell(f"cp {CFG['hosts_backup']} /system/etc/hosts", serial)
        h, _ = ca_hash_local()
        if h:
            adb_shell(f"rm {CFG['ca_dir_device']}/{h}.0", serial)
        adb(["reverse", "--remove-all"], serial=serial)
        print("[✔] 设备侧已还原（hosts/代理/CA/reverse）")
    for port in (CFG["proxy_port"], CFG["reverse_port"]):
        pid = port_pid_listen(port)
        if pid and pid > 0:
            sh(["taskkill", "/PID", str(pid), "/F"] if os.name == "nt" else ["kill", str(pid)])
    print("[✔] 宿主 mitmdump 已停止（若在运行）")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["doctor", "setup", "probe", "status", "teardown"])
    ap.add_argument("--serial")
    ap.add_argument("--known", action="append", default=[])
    ap.add_argument("--no-mitm", action="store_true")
    ap.add_argument("--start-frida", action="store_true")
    ap.add_argument("--domains", nargs="+")
    args = ap.parse_args()
    if args.domains:
        CFG["domains"] = args.domains
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    return {"doctor": mode_doctor, "setup": mode_setup, "probe": mode_probe,
            "status": mode_status, "teardown": mode_teardown}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
