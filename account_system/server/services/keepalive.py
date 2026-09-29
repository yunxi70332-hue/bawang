"""账号保活跃自动化任务（2026-09-29）。

目标：每天定时（默认 10:30）用在线账号的 token 间歇性访问广东省内所有门店的菜单
接口，维持账号活跃。菜单浏览链（chagee-navigation-web）本身是游客接口不校验
token——因此每账号每轮先做一次真正的 token 鉴权校验（whoami，失效即标记账号
expired 并告警），菜单请求则携带账号 token 头 + 账号自己的设备 uuid/userId 发起
（「以该账号身份浏览菜单」的正确传递语义）。

省份门店枚举：游客 cityList → 按 cityCode 前缀过滤（44* = 广东，行政区划码）→
逐城市 store/list 全页遍历。默认策略全部门店轮转分配给各在线账号（round-robin，
打散+随机化），支持 max_stores_per_run 截断保护。

访问策略（间歇性）：请求之间 random.uniform(min,max) 秒停顿（默认 5~12s），避免
集中请求；瞬时失败（网络/超时/errcode）按指数退避重试（默认最多 2 次），token 失
效不重试直接告警。

监控与告警：逐请求落 keepalive_records（状态/耗时/尝试序号），运行头落
keepalive_runs（聚合计数）；oplog 打 run/account 两级摘要；token 失效 →
log_monitor.create_alert(ERROR)；失败率超阈值 → WARN 告警（冷却去重由监控模块负责）。

配置 data/keepalive_config.json（全 str 值，与 decision_config 同规约）：
  enabled / run_at / province_city_prefix / min_interval_seconds / max_interval_seconds /
  max_stores_per_run / min_stores_per_account / request_timeout / max_retries /
  whoami_check / alert_failure_rate / alert_min_failures

线程：start_keepalive_thread() 幂等启动（name="keepalive"，CHAGEE_KEEPALIVE_THREAD
<=0 不启动——离线测试必须禁用，与 menu-refresh/order-reconcile 同约定）。手动触发
（POST /api/ops/keepalive/trigger）不受 enabled 限制。
"""

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database import SessionLocal
from models import ChageeAccount, KeepaliveRecord, KeepaliveRun
from oplog import log_op

logger = logging.getLogger(__name__)

# data/ 定位：services/x.py 上三层 = account_system/（与 database.DATA_DIR 同级）
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
_CONFIG_PATH = os.path.join(_DATA_DIR, "keepalive_config.json")

DEFAULT_CONFIG = {
    "enabled": "true",               # 每日定时触发开关（手动触发不受限）
    "run_at": "10:30",               # 每天运行时刻 HH:MM
    "province_city_prefix": "44",    # 省份 = cityCode 前缀（44 广东，行政区划码）
    "min_interval_seconds": "5",     # 请求间最小间歇（含端点随机）
    "max_interval_seconds": "12",
    "max_stores_per_run": "0",       # 0 = 省内全部门店；>0 截断保护
    "min_stores_per_account": "2",   # 账号最少访问门店数（门店充足时由轮转自然保证）
    "request_timeout": "20",         # 菜单请求超时（秒）
    "max_retries": "2",              # 瞬时失败重试次数（指数退避 3s/6s/…；token 失效不重试）
    "whoami_check": "true",          # 每账号每轮先 whoami 鉴权校验
    "alert_failure_rate": "0.5",     # 失败率超此值且失败数达下限 → WARN 告警
    "alert_min_failures": "3",
}

_THREAD_ENV = os.environ.get("CHAGEE_KEEPALIVE_THREAD", "1")
_thread_started = False
_trigger_event = threading.Event()   # 手动触发信号（router set，调度线程消费）
_lock = threading.Lock()
_state: dict = {"running": False, "current_run_id": 0, "next_run_at": ""}

# 测试钩子：离线测试置 0 间歇并替换等待实现，避免真实 sleep / 网络等待
_sleep = time.sleep


# ---------------- 配置 ----------------

def load_config() -> dict:
    """读 data/keepalive_config.json：文件不存在/缺键/解析失败按 DEFAULT_CONFIG
    兜底（fail-soft 不落盘，save_config 才写，与 decision_config 同规约）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
            for key, value in DEFAULT_CONFIG.items():
                if cfg.get(key) is None:
                    cfg[key] = value
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("keepalive_config.json 解析失败，使用默认配置", exc_info=True)
    return cfg


def save_config(cfg: dict) -> dict:
    """写配置（DEFAULT_CONFIG 补全缺键 + 已知键转 str；间歇 min>max 自动对调）后
    临时文件原子落盘，返回落盘后的完整配置。"""
    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        merged.update(cfg)
    for key in DEFAULT_CONFIG:
        merged[key] = str(merged.get(key, DEFAULT_CONFIG[key]))
    try:
        lo = float(merged["min_interval_seconds"])
        hi = float(merged["max_interval_seconds"])
        if lo > hi:   # 间歇区间倒置自动对调（交换原字符串值，避免 float 痕迹）
            merged["min_interval_seconds"], merged["max_interval_seconds"] = (
                merged["max_interval_seconds"], merged["min_interval_seconds"])
    except ValueError:
        pass
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CONFIG_PATH)
    return merged


def cfg_bool(cfg: dict, key: str) -> bool:
    return str(cfg.get(key, "")).strip().lower() not in ("0", "false", "no", "off", "")


def cfg_int(cfg: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(str(cfg.get(key, default)).strip()))
    except (TypeError, ValueError):
        return default


def cfg_float(cfg: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(str(cfg.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def next_run_time(cfg: dict, now: datetime | None = None) -> datetime | None:
    """下一次定时运行时刻：今天 run_at 未过取今天，否则明天；时刻非法返回 None
    （调度线程按 60s 后重试解析，不中断线程）。"""
    now = now or datetime.now()
    try:
        hh, mm = str(cfg.get("run_at", "")).strip().split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        return None
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ---------------- 省份门店枚举（游客链路，无 token） ----------------

def enumerate_province_stores(cfg: dict) -> tuple[list[dict], list[str]]:
    """cityList → cityCode 前缀过滤省份 → 逐城市 iter_stores 全页遍历。
    返回 ([{store_no, store_name, city_name, city_code}], 城市级错误列表)；
    单城市失败跳过不中断（记入错误列表，运行 note 汇总）。"""
    from services import chagee_bridge as bridge

    prefix = str(cfg.get("province_city_prefix", "44")).strip()
    guest = bridge.ChageeMenuApi()
    stores: list[dict] = []
    errors: list[str] = []
    cities = []
    for group in guest.city_list():
        for city in group.get("cityList", []) or []:
            if str(city.get("cityCode", "")).startswith(prefix):
                cities.append(city)
    for city in cities:
        try:
            for s in guest.iter_stores(city["cityCode"]):
                stores.append({
                    "store_no": str(s.get("storeNo", "")),
                    "store_name": str(s.get("storeName", ""))[:128],
                    "city_name": str(s.get("cityName", "")),
                    "city_code": str(s.get("cityCode", "")),
                })
        except Exception as e:
            errors.append(f"{city.get('cityName', city.get('cityCode'))}: {type(e).__name__}: {e}"[:120])
    stores = [s for s in stores if s["store_no"]]
    random.shuffle(stores)   # 每轮顺序随机化：访问模式打散，避免每天同一顺序集中命中
    return stores, errors


# ---------------- 执行一轮 ----------------

def _record(db: Session, run_id: int, account: ChageeAccount, store: dict | None,
            action: str, attempt: int, ok: bool, status: str, ms: int, error: str = "") -> None:
    db.add(KeepaliveRecord(
        run_id=run_id, account_id=account.id, account_label=f"{account.label}#{account.id}",
        store_no=(store or {}).get("store_no", ""), store_name=(store or {}).get("store_name", ""),
        action=action, attempt=attempt, ok=ok, status=status[:64], ms=int(ms),
        error=str(error)[:255]))
    db.commit()   # 逐条落库：长任务中途可查进度（间歇节奏下写放大可忽略）


def _pace(cfg: dict) -> None:
    """请求间歇：[min,max] 区间随机停顿（间歇性访问策略）。"""
    lo = max(0.0, cfg_float(cfg, "min_interval_seconds", 5.0))
    hi = max(lo, cfg_float(cfg, "max_interval_seconds", 12.0))
    if hi <= 0:
        return
    _sleep(random.uniform(lo, hi))


def _visit_menu(menu_api, store: dict, cfg: dict) -> dict:
    """单门店菜单访问（含瞬时失败重试）：返回 {ok, ms, attempt, status, error}。
    任何异常（errcode/网络/超时）都视为可重试；最终失败如实记录。"""
    retries = max(0, cfg_int(cfg, "max_retries", 2))
    last = {"ok": False, "ms": 0, "attempt": 0, "status": "", "error": ""}
    for attempt in range(1, retries + 2):
        t0 = time.perf_counter()
        try:
            menu_api.store_goods_menu(store["store_no"])
            return {"ok": True, "ms": int((time.perf_counter() - t0) * 1000),
                    "attempt": attempt, "status": "ok", "error": ""}
        except Exception as e:
            last = {"ok": False, "ms": int((time.perf_counter() - t0) * 1000),
                    "attempt": attempt, "status": type(e).__name__,
                    "error": str(e)[:200]}
            if attempt <= retries:
                _sleep(3 * attempt)   # 指数退避：3s / 6s / …
    return last


def run_keepalive(db: Session, trigger: str = "manual", cfg: dict | None = None) -> dict:
    """执行一轮保活跃（同步；调度线程与手动触发共用）。流程：枚举省份门店 → 在线
    账号轮转分配 → 每账号 whoami 鉴权 + 逐店间歇菜单访问 → 聚合落库 + 告警。
    返回运行摘要 dict（run_id/status/计数）。线程内由调用方保证 _state.running 互斥。"""
    from services import chagee_bridge as bridge

    cfg = cfg or load_config()
    prefix = str(cfg.get("province_city_prefix", "44")).strip()
    run = KeepaliveRun(trigger=trigger, status="running",
                       province=f"cityCode {prefix}* 省份")
    db.add(run)
    db.commit()
    db.refresh(run)
    with _lock:
        _state["current_run_id"] = run.id

    accounts = (db.query(ChageeAccount)
                  .filter(ChageeAccount.status == "online", ChageeAccount.token != "")
                  .order_by(ChageeAccount.id).all())
    if not accounts:
        run.status, run.finished_at = "failed", datetime.now()
        run.note = "无在线账号（status=online 且 token 非空），未发起任何请求"
        db.commit()
        log_op(level="WARN", action="keepalive.run", actor="keepalive",
               target=f"run#{run.id}", result="failed", params={"note": run.note})
        return {"run_id": run.id, "status": "failed", "requests_total": 0}

    stores, enum_errors = enumerate_province_stores(cfg)
    store_total = len(stores)
    city_total = len({s["city_code"] for s in stores})
    cap = cfg_int(cfg, "max_stores_per_run", 0)
    if cap > 0:
        stores = stores[:cap]
    run.city_total = city_total
    run.store_total, run.stores_planned, run.accounts_total = store_total, len(stores), len(accounts)

    if not stores:
        run.status, run.finished_at = "failed", datetime.now()
        run.note = f"省份门店枚举为空（前缀 {prefix}*）：{'；'.join(enum_errors[:3]) or '城市列表为空'}"
        db.commit()
        log_op(level="ERROR", action="keepalive.run", actor="keepalive",
               target=f"run#{run.id}", result="failed", params={"note": run.note[:200]})
        from log_monitor import create_alert
        create_alert("keepalive_no_stores", "ERROR", f"保活跃任务 run#{run.id}：省份门店枚举为空",
                     {"trigger": trigger, "errors": enum_errors[:10]})
        return {"run_id": run.id, "status": "failed", "requests_total": 0}

    # 轮转分配：门店序列依次派给账号（i % n），每账号门店数差 ≤1 —— min_stores_per_account
    # 在门店充足时天然满足；cap 截断时若某账号分不到也如实记录（账号数多于门店数场景）
    assignments: dict[int, list[dict]] = {a.id: [] for a in accounts}
    for i, store in enumerate(stores):
        assignments[accounts[i % len(accounts)].id].append(store)
    min_stores = max(0, cfg_int(cfg, "min_stores_per_account", 2))

    ok_count, fail_count, ms_total, ms_hits, expired_accounts = 0, 0, 0, 0, 0
    failure_notes: list[str] = []

    for account in accounts:
        my_stores = assignments[account.id]
        label = f"{account.label}#{account.id}"

        # ① token 鉴权校验（真校验；失效 → 标记 expired + ERROR 告警 + 跳过该账号）
        if cfg_bool(cfg, "whoami_check"):
            t0 = time.perf_counter()
            try:
                bridge.proto_whoami(bridge.build_client(account))
                ms = int((time.perf_counter() - t0) * 1000)
                ok_count += 1
                ms_total += ms
                ms_hits += 1
                _record(db, run.id, account, None, "whoami", 1, True, "ok", ms)
            except bridge.SessionExpiredError as e:
                ms = int((time.perf_counter() - t0) * 1000)
                expired_accounts += 1
                account.status = "expired"   # 与扫描/检查同语义：单账号失效不影响整体
                db.commit()
                fail_count += 1
                _record(db, run.id, account, None, "whoami", 1, False, "expired", ms, str(e)[:200])
                log_op(level="ERROR", action="keepalive.token_invalid", actor="keepalive",
                       target=label, result="expired", error=e,
                       params={"run_id": run.id, "phone": account.phone})
                from log_monitor import create_alert
                create_alert("keepalive_token_invalid", "ERROR",
                             f"保活跃：账号 {label} token 已失效（已标记 expired，需重新登录）",
                             {"run_id": run.id, "account_id": account.id,
                              "phone": account.phone, "error": str(e)[:300]})
                continue
            except Exception as e:   # whoami 网络/协议异常：记录但继续菜单访问（token 未必失效）
                fail_count += 1
                _record(db, run.id, account, None, "whoami", 1, False,
                        type(e).__name__, int((time.perf_counter() - t0) * 1000), str(e)[:200])
                failure_notes.append(f"{label} whoami: {type(e).__name__}")

        if not my_stores and min_stores > 0:
            failure_notes.append(f"{label} 未分到门店（门店数少于账号数）")
            continue

        # ② 以账号身份逐店访问菜单（token 头 + 账号设备 uuid/userId；间歇停顿）
        menu_client = bridge.ChageeMenuApi(
            uuid=account.device_uuid, timeout=cfg_int(cfg, "request_timeout", 20),
            user_id=(account.customer_id or None), token=account.token)
        for store in my_stores:
            res = _visit_menu(menu_client, store, cfg)
            if res["ok"]:
                ok_count += 1
                ms_total += res["ms"]
                ms_hits += 1
            else:
                fail_count += 1
                failure_notes.append(
                    f"{label} {store['store_no']}: {res['status']}")
            _record(db, run.id, account, store, "menu", res["attempt"], res["ok"],
                    res["status"], res["ms"], res["error"])
            _pace(cfg)

    total = ok_count + fail_count
    run.requests_total, run.requests_ok, run.requests_failed = total, ok_count, fail_count
    run.accounts_expired = expired_accounts
    run.avg_ms = f"{(ms_total / ms_hits):.1f}" if ms_hits else ""
    run.finished_at = datetime.now()
    if total == 0:
        run.status = "failed"
    elif fail_count == 0:
        run.status = "success"
    else:
        run.status = "partial"
    run.note = "；".join((enum_errors + failure_notes)[:8])[:512]
    db.commit()

    # 失败率告警（阈值 + 最少失败数双门槛；冷却去重由 log_monitor 负责）
    if fail_count >= max(1, cfg_int(cfg, "alert_min_failures", 3)):
        rate = fail_count / total if total else 1.0
        if rate >= max(0.01, cfg_float(cfg, "alert_failure_rate", 0.5)):
            from log_monitor import create_alert
            create_alert("keepalive_failures", "WARN",
                         f"保活跃 run#{run.id}：失败率 {rate:.0%}（{fail_count}/{total}）",
                         {"run_id": run.id, "failed": fail_count, "total": total,
                          "expired_accounts": expired_accounts,
                          "notes": failure_notes[:10], "enum_errors": enum_errors[:5]})

    log_op(action="keepalive.run", actor="keepalive", target=f"run#{run.id}",
           result=run.status,
           params={"trigger": trigger, "stores": len(stores), "store_total": store_total,
                   "accounts": len(accounts), "expired": expired_accounts,
                   "ok": ok_count, "failed": fail_count, "avg_ms": run.avg_ms})
    return {"run_id": run.id, "status": run.status, "requests_total": total,
            "requests_ok": ok_count, "requests_failed": fail_count,
            "stores": len(stores), "accounts": len(accounts),
            "expired_accounts": expired_accounts}


# ---------------- 调度线程 ----------------

def _scheduler_loop() -> None:
    target: datetime | None = None
    last_cfg_load = 0.0
    while True:
        try:
            now = time.time()
            if now - last_cfg_load > 30:   # 配置热更新：30s 重读（run_at/enabled 即时生效）
                cfg = load_config()
                last_cfg_load = now
                target = (next_run_time(cfg)
                          if str(cfg.get("enabled")).strip().lower() not in ("0", "false", "no", "off")
                          else None)
                with _lock:
                    _state["next_run_at"] = target.strftime("%Y-%m-%d %H:%M:%S") if target else ""
            manual = _trigger_event.is_set()
            due = target is not None and datetime.now() >= target
            if not (manual or due):
                time.sleep(5)
                continue
            _trigger_event.clear()
            trigger = "manual" if manual else "schedule"
            with _lock:
                if _state["running"]:
                    continue   # 上一轮未结束（长跑/手动与定时撞车）：本轮跳过留 oplog
                _state["running"] = True
            try:
                with SessionLocal() as db:
                    run_keepalive(db, trigger=trigger)
            except Exception:
                logger.exception("保活跃任务执行异常")
                log_op(level="ERROR", action="keepalive.run", actor="keepalive",
                       result="failed", error="unhandled exception")
            finally:
                with _lock:
                    _state["running"] = False
        except Exception:
            logger.exception("保活跃调度线程异常（10s 后继续）")
            time.sleep(10)


def start_keepalive_thread() -> None:
    """幂等启动 daemon 调度线程（name="keepalive"）。CHAGEE_KEEPALIVE_THREAD<=0
    不启动（离线测试必须禁用，与 menu-refresh 线程同约定）。"""
    global _thread_started
    if _thread_started:
        return
    try:
        if float(_THREAD_ENV) <= 0:
            return
    except ValueError:
        pass
    _thread_started = True
    threading.Thread(target=_scheduler_loop, name="keepalive", daemon=True).start()


def trigger_manual() -> tuple[bool, str]:
    """手动触发一轮（router 调用）：空闲 → 置信号返回 True；运行中 → False。"""
    with _lock:
        if _state["running"]:
            return False, "保活跃任务正在运行中，请稍后再试"
    _trigger_event.set()
    return True, "已触发"


def snapshot_status(db: Session) -> dict:
    """当前状态快照：运行中标记/当前 run_id/下次定时时刻 + 最近一轮运行摘要。"""
    cfg = load_config()
    target = next_run_time(cfg)
    last = (db.query(KeepaliveRun).order_by(KeepaliveRun.id.desc()).first())
    with _lock:
        running, current_run_id = _state["running"], _state["current_run_id"]
    return {
        "enabled": cfg_bool(cfg, "enabled"),
        "running": running,
        "current_run_id": current_run_id,
        "next_run_at": target.strftime("%Y-%m-%d %H:%M:%S") if target else "",
        "last_run": ({
            "id": last.id, "trigger": last.trigger, "status": last.status,
            "started_at": last.started_at.strftime("%Y-%m-%d %H:%M:%S") if last.started_at else "",
            "finished_at": last.finished_at.strftime("%Y-%m-%d %H:%M:%S") if last.finished_at else "",
            "city_total": last.city_total, "store_total": last.store_total,
            "stores_planned": last.stores_planned, "requests_total": last.requests_total,
            "requests_ok": last.requests_ok, "requests_failed": last.requests_failed,
            "accounts_total": last.accounts_total, "accounts_expired": last.accounts_expired,
            "avg_ms": last.avg_ms, "note": last.note,
        } if last else None),
    }
