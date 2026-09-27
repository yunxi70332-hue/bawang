"""收银台链接铸造 · 服务端编排（fire-and-forget 后台线程，protocol/frida 双 provider）。

背景：下单主流程拿到 order_str 后，把「打开该单官方收银台」变成系统动作，铸造出
mclient.alipay.com/cashierRoutePay 短窗链接并回填 PaySession.alipay_cashier_url
（浏览器可直开付款）。

两个 provider（CHAGEE_MINT_PROVIDER）：
  - protocol（默认）：scripts/alipay_msp_client.py 纯 HTTP 复刻支付宝 SDK 的
    mobilegw mcpay 加密 RPC（RSA-1024 会话密钥 + 3DES-CBC + gzip 分帧，逆向自
    APK 内嵌 SDK 15.8.35），无任何设备依赖、可并发、秒级完成。2026-09-27 在线
    实证：生产网关接受并铸造 session，链接 302→h5pay/landing 判活通过。
  - frida：scripts/frida_mint_cashier.py 云手机路径（PayTask.pay + DevTools 哨兵
    捕获），保留为降级/对照通道。
  - auto：protocol 优先，失败降级 frida。

时序设计（为什么这样做）：
  - trigger_mint 在请求线程里只做 环境开关/防抖/查会话 三件事（毫秒级），铸造动作
    全部丢进 daemon 线程——payload 主流程 <50ms 的预算绝不能被网络/adb/frida 往返吃掉
  - _mint_lock 仅约束 frida 路径：云手机 SDK 一次只能开一个收银台；protocol 纯
    HTTP 可并发，不占锁
  - 基线快照在「frida 线程内、锁内、mint 之前」采集：保证基线→PayTask.pay→捕获
    新页之间没有别的 frida 铸造线程插入污染基线
  - 失败路径全部只记 mint_failed 事件（事件流即排查时间线），绝不影响支付主流程
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

from database import SessionLocal
from services.pay_session import (
    EVENT_CASHIER_UPDATED, get_by_order_no, record_event, token_prefix,
)

logger = logging.getLogger(__name__)

# scripts/ 注入：frida_mint_cashier / alipay_msp_client 在仓库根 scripts/
# （chagee_bridge 已注入过同一路径，这里独立注入保证本模块不依赖 import 顺序）
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

EVENT_MINT_FAILED = "mint_failed"        # 铸造链路失败（加密/网络/捕获/反构/存活探测任一环节）
MINT_ENABLED_ENV = "CHAGEE_MINT_ENABLED"  # "0"=关闭（离线测试必置 0，杜绝真触铸造）
MINT_PROVIDER_ENV = "CHAGEE_MINT_PROVIDER"  # protocol(默认)|frida|auto
MINT_DEBOUNCE_SECONDS = 60.0             # 同单 60s 内不重复触发（防双击/重复续付重复铸造）
CAPTURE_WAIT_SECONDS = 30.0              # DevTools 捕获新页面的等待预算（仅 frida 路径）

# 全局串行：云手机 SDK 一次只能开一个收银台（并发触发会互相顶掉 H5PayActivity）
_mint_lock = threading.Lock()
# 防抖表：order_no → 上次触发 monotonic 时间戳（worker 结束时清理，防长期膨胀）
_last_mint: dict[str, float] = {}


def _provider() -> str:
    """当前铸造通道：protocol(默认) / frida / auto。"""
    val = (os.environ.get(MINT_PROVIDER_ENV, "") or "").strip().lower()
    return val if val in ("protocol", "frida", "auto") else "protocol"


def _load_minter():
    """import scripts/frida_mint_cashier（sys.path 已在模块头注入；失败向上抛由调用方记事件）。"""
    import frida_mint_cashier  # noqa: E402  # scripts/ 在 sys.path
    return frida_mint_cashier


def _record_failed(order_no: str, error: str, source: str = "frida-mint") -> None:
    """铸造失败落事件流：独立短事务 + 自兜底（观测数据落库失败只 WARN，不冒泡）。"""
    try:
        with SessionLocal() as db:
            record_event(db, order_no, "", EVENT_MINT_FAILED,
                         {"source": source, "error": str(error)[:200]})
        logger.warning("收银台铸造失败 provider=%s order_no=%s error=%s",
                       source, order_no, str(error)[:200])
    except Exception:
        logger.warning("mint_failed 事件落库异常 order_no=%s", order_no, exc_info=True)


def trigger_mint(db, order_no: str) -> dict:
    """fire-and-forget 铸造入口（pay_session.pay_link_payload_with_session 尾部调用）。

    绝不抛出、绝不阻塞：环境开关 → 防抖 → 查会话取 order_str 三步后即返回，铸造动作
    在 daemon 线程里做。返回值仅供调用方打点：
      {triggered: True} / {skipped: disabled|debounced|no_session|no_order_str|error}
    """
    try:
        if os.environ.get(MINT_ENABLED_ENV, "1") == "0":
            return {"skipped": "disabled"}
        order_no = str(order_no or "").strip()
        if not order_no:
            return {"skipped": "no_order_no"}
        now = time.monotonic()
        if now - _last_mint.get(order_no, 0.0) < MINT_DEBOUNCE_SECONDS:
            return {"skipped": "debounced"}
        sess = get_by_order_no(db, order_no)
        if not sess:
            return {"skipped": "no_session"}
        order_str = sess.order_str or ""
        if not order_str:
            return {"skipped": "no_order_str"}
        _last_mint[order_no] = now
        threading.Thread(target=_mint_serialized, args=(order_no, order_str),
                         daemon=True, name=f"cashier-mint-{order_no}").start()
        return {"triggered": True}
    except Exception:
        # 入口兜底：铸造是旁路增强，任何异常都不许影响支付主流程
        logger.warning("触发收银台铸造失败（忽略）order_no=%s", order_no, exc_info=True)
        return {"skipped": "error"}


def _mint_serialized(order_no: str, order_str: str) -> None:
    """daemon 线程体：按 provider 分流。

    protocol：纯 HTTP 可并发，不占 _mint_lock，直接铸造并回填。
    frida：全局串行 + 铸造前基线快照 + mint_worker（基线采集放线程内而非请求
    线程——adb/DevTools 往返秒级；放锁内保证快照与 PayTask.pay 之间没有别的
    frida 铸造线程插入污染基线）。
    auto：protocol 失败后降级走 frida 路径。
    """
    provider = _provider()
    if provider in ("protocol", "auto"):
        url = _mint_via_protocol(order_no, order_str)
        if url:
            _fill_back(order_no, url, "protocol-mint")
            _last_mint.pop(order_no, None)
            return
        if provider == "protocol":
            _last_mint.pop(order_no, None)
            return
        logger.warning("protocol 铸造失败，auto 降级 frida order_no=%s", order_no)
    with _mint_lock:
        try:
            baseline_urls = _load_minter().snapshot_mclient_urls()
        except Exception as e:
            _record_failed(order_no, f"import: {type(e).__name__}: {str(e)[:180]}")
            _last_mint.pop(order_no, None)
            return
        try:
            mint_worker(order_no, order_str, baseline_urls)
        except Exception:
            # mint_worker 已分段捕获，这里只是最后兜底（防御未预期路径）
            logger.warning("收银台铸造线程异常退出 order_no=%s", order_no, exc_info=True)
            _record_failed(order_no, "worker_crashed")
        finally:
            # 结束清理防抖表：无论成败，下一轮续付可立即重新触发
            _last_mint.pop(order_no, None)


def _mint_via_protocol(order_no: str, order_str: str) -> str | None:
    """纯协议铸造（scripts/alipay_msp_client，无设备依赖）。

    成功（含 probe 判活）返回入口 URL；任何失败记 mint_failed(source=protocol-mint)
    并返回 None（auto 模式下由调用方决定是否降级 frida）。
    """
    try:
        import alipay_msp_client as msp  # noqa: E402  # scripts/ 在 sys.path
    except Exception as e:
        _record_failed(order_no, f"import msp: {type(e).__name__}: {str(e)[:180]}",
                       source="protocol-mint")
        return None
    try:
        result = msp.mint_cashier_link(order_str, probe=True)
    except Exception as e:
        _record_failed(order_no, f"{type(e).__name__}: {e}", source="protocol-mint")
        return None
    if not result.get("ok"):
        _record_failed(order_no, str(result.get("error") or "mint_not_ok"),
                       source="protocol-mint")
        return None
    logger.info("纯协议收银台铸造成功 order_no=%s url=%s...", order_no, result["url"][:80])
    return result["url"]


def _fill_back(order_no: str, entry: str, source: str) -> None:
    """回填 PaySession.alipay_cashier_url + cashier_updated 事件（双 provider 共用 e 步）。

    独立短事务；铸造期间会话已被支付/取消/过期收口 → 放弃，不算失败。
    """
    with SessionLocal() as db:
        sess = get_by_order_no(db, order_no)
        if not sess or sess.status != "issued":
            logger.info("铸造完成但会话已非 issued，放弃回填 order_no=%s status=%s",
                        order_no, sess.status if sess else "gone")
            return
        sess.alipay_cashier_url = entry[:512]
        db.commit()
        record_event(db, order_no, token_prefix(sess.pay_token), EVENT_CASHIER_UPDATED,
                     {"source": source, "alive": True, "url_prefix": entry[:80]})


def mint_worker(order_no: str, order_str: str, baseline_urls: set[str]) -> None:
    """铸造全流程（daemon 线程内执行，分段捕获——失败只记 mint_failed 事件不冒泡）：
    a. import 铸造器 → b. frida 调 PayTask.pay → c. DevTools 捕获新页面 →
    d. 反构入口链接 + 存活探测 → e. 回填 PaySession + 事件。
    """
    # a. scripts 注入与导入（sys.path 已在模块头处理；这里失败即整链失败）
    try:
        fmc = _load_minter()
    except Exception as e:
        _record_failed(order_no, f"import: {type(e).__name__}: {str(e)[:180]}")
        return

    # b. frida 铸造（拉前台/attach/RPC 任一失败都在 mint_via_frida 内转 {ok:False}）
    result = fmc.mint_via_frida(order_str)
    if not result.get("ok"):
        _record_failed(order_no, str(result.get("error") or "mint_not_ok")[:200])
        return

    # c. DevTools 哨兵捕获新收银台页面（H5PayActivity 打开后 WebView 才会出现）
    url = fmc.capture_new_cashier(baseline_urls, wait_s=CAPTURE_WAIT_SECONDS)
    if not url:
        _record_failed(order_no, "capture_timeout")
        return

    # d. 反构入口链接 + 只读存活探测（会话未生效/过期即放弃，绝不回填死链）
    entry = fmc.reconstruct_entry(url)
    if not entry:
        _record_failed(order_no, f"reconstruct_failed: {url[:120]}")
        return
    probe = fmc.probe_alive(entry)
    if not probe.get("alive"):
        _record_failed(order_no, "session_not_alive")
        return

    # e. 回填（与 protocol 路径共用）
    _fill_back(order_no, entry, "frida-mint")
    logger.info("收银台链接铸造成功 order_no=%s url=%s...", order_no, entry[:80])


def mint_status(order_no: str) -> dict | None:
    """铸造状态查询（GET /orders/{order_no}/cashier 端点数据源；无会话返回 None→路由转 404）。

    独立开短事务读库（端点线程与铸造 daemon 线程并发安全）。
    """
    try:
        with SessionLocal() as db:
            sess = get_by_order_no(db, order_no)
            if not sess:
                return None
            return {"order_no": sess.order_no,
                    "alipay_cashier_url": sess.alipay_cashier_url or None,
                    "session_status": sess.status}
    except Exception:
        logger.warning("查询铸造状态失败 order_no=%s", order_no, exc_info=True)
        return None
