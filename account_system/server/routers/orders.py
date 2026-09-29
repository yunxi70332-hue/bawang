"""订单域路由（F5 下单工作台 + F6 取餐查询，契约 docs/api_contract_f5f6.md）。

引擎：scripts/chagee_trade_api.ChageeTradeApi（三场景 wire 定案 2026-09-26）
  - settle   购物车加购 → 无券试算 → 服务端 draft 缓存（模块级 dict+Lock，10 分钟 TTL，新算覆盖）
  - create   draft 复跑选券试算 → createOrder → zero（OrderOutcome）/partial（PayLink）双分支
  - 列表/详情/状态/等待：order_list/order_detail/order_status/waiting_info，行数据回填 OrderRecord

异常链（契约 §0，与 ops.py 现状一致）：
  SessionExpired→409（置 expired+审计） → BridgeError→400 → OrderHang→409（引导先查单）
  → Consistency→409（含差异说明） → Trade/Chagee→502 → 其余→502
"""

import logging
import re
import threading
import time
import uuid as uuidlib
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from audit import log_audit
from database import SessionLocal, get_db
from models import (ChageeAccount, CouponRecord, CouponUsageLog, DecisionLog, OrderPlan,
                    OrderRecord, PayEventLog, SystemUser, VoucherCostCategory,
                    VoucherCostRule)
from oplog import log_op
from schemas import CashierUrlRequest, OrderCreateRequest, OrderSettleRequest, PayModeRequest
from security import require_perm
from services import chagee_bridge as bridge
from services import decision as decision_svc
from services import events_bus
from services import pay_params
from services.order_reconcile import (
    RECONCILE_GRACE_SECONDS, reconcile_expired_pending_orders, reconcile_order, rollback_coupon_usage,
)
from services.pay_session import (
    EVENT_CASHIER_UPDATED, EVENT_ORDER_CANCELLED, PaySessionError, build_h5_url,
    clamp_pay_deadline, get_by_order_no, mark_session, pay_link_payload_with_session,
    record_event, switch_order_to_full_price, token_prefix,
)

router = APIRouter(prefix="/api/ops/accounts/{account_id}/orders", tags=["orders"])
# 全局域端点（不挂在账号路径下）：券使用记录查询
global_router = APIRouter(prefix="/api/ops", tags=["orders"])

logger = logging.getLogger(__name__)

# 状态本地映射（前端 utils/format.js 同步；服务端有 orderStatusText 时优先服务端文案）
ORDER_STATUS_LABELS = {1: "待支付", 3: "制作中", 6: "已完成", 7: "已取消"}

DRAFT_TTL_SECONDS = 600      # draft 缓存 10 分钟（与茶姬支付窗一致）
PAY_WINDOW_SECONDS = 600     # 待支付单自动取消窗口（wire 实证 paymentExpiryType=autoCancel）

# 模块级 draft 缓存：key=account_id，value={draft_id,target,cart,settle_base,store_no,store_name,
# goods_desc,created_at}；新 settle 覆盖旧 draft（单账号单草稿）
_drafts: dict[int, dict] = {}
_draft_lock = threading.Lock()


def _get_account(db: Session, account_id: int) -> ChageeAccount:
    account = db.get(ChageeAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    return account


def _fail(account: ChageeAccount, db: Session, request: Request, user: SystemUser,
          action: str, e: Exception):
    """契约 §0 异常链：统一转 HTTPException（本函数必抛，无返回值）。"""
    label = f"{account.label}#{account.id}"
    if isinstance(e, bridge.SessionExpiredError):
        account.status = "expired"
        db.commit()
        log_audit(db, request, user, action, label, {"result": "expired", "error": str(e)[:200]})
        log_op(level="ERROR", actor=user.username, target=label, action=action,
               result="expired", error=e, params={"http_status": 409})
        raise HTTPException(409, f"账号凭证已失效，请重新登录后重试: {e}")
    if isinstance(e, bridge.ChageeBridgeError):
        log_op(level="ERROR", actor=user.username, target=label, action=action,
               result="failed", error=e, params={"http_status": 400})
        raise HTTPException(400, str(e))
    if isinstance(e, bridge.OrderHangError):
        log_op(level="ERROR", actor=user.username, target=label, action=action,
               result="failed", error=e, params={"http_status": 409})
        # createOrder 无显式幂等键：结果不确定时严禁直接重试，先到取餐查询页查单
        raise HTTPException(409, "下单结果不确定，订单可能已创建——请到取餐查询页查单后再决定是否重试")
    if isinstance(e, bridge.ConsistencyError):
        log_op(level="ERROR", actor=user.username, target=label, action=action,
               result="failed", error=e, params={"http_status": 409})
        raise HTTPException(409, f"下单一致性校验失败，已中止提交: {e}")
    if isinstance(e, (bridge.TradeError, bridge.ChageeError)):
        msg = str(e)
        if "网络异常" in msg:
            # trade 服务校验失败的兜底文案（82041201/91010009/8202020200007 同文案不同码）：
            # 最常见根因是门店非营业时间（2026-09-27 定案，docs/order_error_82041201_20260927.md）
            msg += "（服务端校验兜底文案：最常见原因是所选门店已打烊/非营业时间，请换营业中门店或营业时段重试）"
        log_op(level="ERROR", actor=user.username, target=label, action=action,
               result="failed", error=e, params={"http_status": 502})
        raise HTTPException(502, f"协议错误: {msg}")
    log_op(level="ERROR", actor=user.username, target=label, action=action,
           result="failed", error=e, params={"http_status": 502})
    raise HTTPException(502, f"请求失败: {type(e).__name__}: {e}")


def _fmt_money(v) -> str:
    """金额统一两位小数字符串（协议层金额一律字符串，此处仅服务端派生值的展示格式化）。"""
    try:
        return f"{Decimal(str(v)).quantize(Decimal('0.01'))}"
    except Exception:
        return str(v)


def _status_label(status, server_text: str = "") -> str:
    st = int(status or 0)
    return server_text or ORDER_STATUS_LABELS.get(st, str(st) if st else "")


def _parse_deadline(expire_at: str) -> datetime | None:
    # '+' 容错：biz_content 表单解码的空格形如 "2026-09-27+16:20:42"，先归一化再解析
    try:
        return datetime.strptime(str(expire_at or "").replace("+", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _upsert_order(db: Session, account_id: int, order_no, **fields):
    """OrderRecord 落库：order_no 存在则更新；空值不覆盖已有值（如待支付单无 pickupNo）。"""
    order_no = str(order_no or "").strip()
    if not order_no:
        return None
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
    if not rec:
        rec = OrderRecord(account_id=account_id, order_no=order_no)
        db.add(rec)
    for k, v in fields.items():
        if k == "status":
            if v is not None:
                rec.status = int(v)
        elif v not in (None, ""):
            # pay_deadline 统一钳制到官方 10 分钟 autoCancel 窗（与
            # pay_session.upsert_order_record 同步；官方倒计时不随 continuePay 重置）
            if k == "pay_deadline":
                v = clamp_pay_deadline(v, rec.created_at)
            setattr(rec, k, v)
    db.commit()
    return rec


def _goods_desc_from_items(order_items) -> str:
    """orderItems → 商品快照文案（"青青糯山、伯牙绝弦 x2"），截断 255 与列宽一致。"""
    parts = []
    for it in order_items or []:
        name = str(it.get("skuName") or "").strip()
        num = int(it.get("buyNum") or 0)
        if name:
            parts.append(f"{name} x{num}" if num > 1 else name)
    return "、".join(parts)[:255]


def _order_row_fields(r: dict) -> dict:
    """getOrderList 行 → OrderRecord 落库字段（单账号列表与全量扫描同一映射口径；
    wire 实证行含 orderItems/businessTypeText/orderTime，样本 output/pay_complete_capture_20260926.json）。"""
    items = r.get("orderItems") or []
    return {
        "store_no": r.get("storeNo") or "",
        "store_name": r.get("storeName") or "",
        "goods_desc": _goods_desc_from_items(items),
        "quantity": sum(int(i.get("buyNum") or 0) for i in items) or 1,
        "total_amount": str(r.get("totalAmount") or ""),
        "pay_amount": str(r.get("payAmount") or ""),
        "status": int(r.get("orderStatus") or 0) or None,
        "status_label": r.get("orderStatusText") or "",
        "pickup_no": r.get("pickupNo") or "",
        "unique_pos_order_no": r.get("uniquePosOrderNo") or "",
        "order_time": r.get("orderTime") or "",
        "biz_type": r.get("businessTypeText") or "",
    }


# ---------------- 券档案：券ID ↔ 归属账号(token) 唯一映射 + 完整名称全量存储 ----------------

def _token_fp(account: ChageeAccount) -> str:
    """归属账号 token 指纹（与账号接口一致形态，不落全量 token）。"""
    t = account.token or ""
    return (t[:16] + f"...len={len(t)}") if t else ""


def _parse_face(benefit_text: str) -> str:
    """benefitText「20元」→ 面额字符串「20」。
    折扣率券（「7折」）无元面额语义 → 空串（历史缺陷：首数字正则把 7折 存成 7，
    展示成「7元」；2026-09-28 修正，券型/展示统一走 coupon_kind/amount_display）。"""
    kind = bridge.ChageeTradeApi.coupon_kind({"benefitText": benefit_text})
    if kind == "rate":
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)", str(benefit_text or ""))
    return m.group(1) if m else ""


# 使用范围编码 → 文案（与 scripts/chagee_coupon_api.USABLE_SCENES 一致）
_SCENE_LABELS = {2: "自取", 6: "外卖", 62: "团餐自提"}


def _normalize_scenes(scenes) -> str:
    """使用范围归一化为 '自取/外卖' 形态：兼容 F4 归一化字符串 / 原始编码数组 / 单编码。"""
    if not scenes:
        return ""
    if isinstance(scenes, str):
        return scenes.strip()
    if isinstance(scenes, (int, float)):
        return _SCENE_LABELS.get(int(scenes), f"场景{int(scenes)}")
    if isinstance(scenes, (list, tuple)):
        parts = []
        for s in scenes:
            if isinstance(s, str) and s.strip():
                parts.append(s.strip() if not s.strip().isdigit() else _SCENE_LABELS.get(int(s), f"场景{s}"))
            elif isinstance(s, (int, float)):
                parts.append(_SCENE_LABELS.get(int(s), f"场景{int(s)}"))
        return "/".join(dict.fromkeys(parts))   # 去重保序
    return str(scenes)


def _upsert_coupon(db: Session, account: ChageeAccount, entry: dict,
                   bucket: str, source: str) -> None:
    """券档案落库：coupon_code 全局唯一（= 券ID↔token 映射锚点）；完整名称等字段全量存储。
    来源：F4 券查询（effective/historical）与 F5 试算（settle_available，含 canDiscount/门槛/有效期）。"""
    code = str(entry.get("couponCode") or "").strip()
    if not code:
        return
    rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == code).first()
    if not rec:
        rec = CouponRecord(coupon_code=code)
        db.add(rec)
    rec.account_id = account.id
    rec.token_fingerprint = _token_fp(account)
    rec.template_name = str(entry.get("templateName") or "")[:255]
    rec.benefit_text = str(entry.get("benefitText") or "")[:64]
    rec.benefit2_text = str(entry.get("benefit2Text") or "")[:64]
    rec.biz_type = str(entry.get("bizType") or "")[:16]
    rec.amount = _parse_face(entry.get("benefitText"))
    rec.usable_scenes = _normalize_scenes(entry.get("usableScenes"))[:64]
    rec.threshold_tips = str(entry.get("thresholdTips") or "")[:128]
    rec.use_start_time = entry.get("useStartTime") if isinstance(entry.get("useStartTime"), int) else None
    rec.use_end_time = entry.get("useEndTime") if isinstance(entry.get("useEndTime"), int) else None
    rec.can_discount = entry.get("canDiscount") if isinstance(entry.get("canDiscount"), bool) else None
    rec.bucket = bucket
    rec.synced_from = source
    db.commit()


def _validate_coupon(db: Session, account: ChageeAccount, draft: dict,
                     coupon_code: str, coupon_entry: dict) -> str | None:
    """券使用有效性验证（create 前置五重检查）：
    ① 归属映射（券ID↔token 安全验证）② 可用性 ③ 有效期 ④ 使用门槛 ⑤ 试算在列。
    返回 None=通过；否则返回中文拒绝原因。"""
    # ① 归属映射：券档案中该券绑定其他账号 → 拒绝（防止跨账号盗用券ID）
    owner = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
    if owner and owner.account_id != account.id:
        return (f"券 {coupon_code} 归属于其他账号（token 映射校验失败），"
                f"不能在账号「{account.label}」的下单中使用")
    # ② 可用性：服务端明确标记不可用（canDiscount=false）
    if coupon_entry.get("canDiscount") is False:
        reason = coupon_entry.get("unavailableReason") or "服务端标记该券当前不可用"
        return f"券不可用：{reason}"
    # ③ 有效期：毫秒时间戳窗口
    now_ms = int(time.time() * 1000)
    start, end = coupon_entry.get("useStartTime"), coupon_entry.get("useEndTime")
    if isinstance(start, int) and now_ms < start:
        return f"券未到生效时间（生效于 {_fmt_ts(start)}）"
    if isinstance(end, int) and now_ms > end:
        return f"券已过期（有效期至 {_fmt_ts(end)}）"
    # ④ 使用门槛：thresholdTips「满N元可用」对照试算总额（引擎要求 Decimal 入参）
    total_dec = Decimal(str(draft["settle_base"].total_trade_price or "0"))
    if not bridge.ChageeTradeApi.coupon_threshold_ok(coupon_entry, total_dec):
        tips = coupon_entry.get("thresholdTips") or "门槛未满足"
        return f"使用门槛未满足：{tips}（当前订单总额 {_fmt_money(total_dec)} 元）"
    return None


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _mark_coupon_used(db: Session, coupon_code: str, order_no: str) -> None:
    """成单后回填券档案使用痕迹（本地缓存性质；真实状态以茶姬侧为准，F4 查询会刷新）。"""
    rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
    if rec:
        rec.last_used_at = datetime.now()
        rec.last_order_no = order_no
        db.commit()


def _log_coupon_usage(db: Session, account: ChageeAccount, user: SystemUser,
                      coupon_code: str, coupon_name: str, result: str,
                      order_no: str = "", deduction: str = "",
                      total_amount: str = "", pay_amount: str = "",
                      scenario: str = "", fail_reason: str = "") -> None:
    """券使用日志：成功/被拒/失败三类均落库（时间/订单号/券ID/账号与操作人/金额）。"""
    db.add(CouponUsageLog(
        coupon_code=coupon_code, coupon_name=(coupon_name or "")[:255],
        account_id=account.id, account_label=f"{account.label}#{account.id}",
        operator=user.username if user else "",
        order_no=order_no, deduction=deduction,
        total_amount=total_amount, pay_amount=pay_amount, scenario=scenario,
        result=result, fail_reason=(fail_reason or "")[:255],
    ))
    db.commit()


def _order_target_snapshot(draft: dict) -> dict:
    """settle target 快照（OrderRecord.order_target JSON 列）：
    存下单时的完整算价入参 + 必选加料 + 门店/商品描述，供 switch-full-price 原价重下逐字段复用。"""
    return {
        "target": draft["target"],
        "extra_list": draft.get("extra_list") or [],
        "store_no": draft.get("store_no") or "",
        "store_name": draft.get("store_name") or "",
        "goods_desc": draft.get("goods_desc") or "",
    }


def _backfill_decision_log(db: Session, body: OrderCreateRequest, account: ChageeAccount,
                           order_no: str, coupon_code: str, deduction: str,
                           pay_actual: str, user: SystemUser) -> None:
    """成单挂钩（契约 decision_api_contract.md §6）：请求带 decision_log_id 时，把该
    DecisionLog 行（decide 评估时 order_no 为空）回填为真实成单信息——order_no/
    account_id/coupon_code（原行空时）/deduction_actual（选券复跑的服务端抵扣）/
    pay_actual（OrderOutcome.pay_amount 或 PayLink.total_amount）+ plan_json 补 order_no。
    找不到该 id 或已被其他单占用时不报错（仅 log_op WARN），绝不影响下单主流程；
    未传 decision_log_id（缺省 0）时行为与决策系统引入前完全一致。"""
    log_id = int(getattr(body, "decision_log_id", 0) or 0)
    if log_id <= 0:
        return
    row = db.get(DecisionLog, log_id)
    if row is None:
        log_op(level="WARN", action="decision.backfill", actor=user.username,
               target=order_no, result="missed",
               params={"decision_log_id": log_id, "reason": "not_found"})
        return
    if str(row.order_no or "").strip():
        log_op(level="WARN", action="decision.backfill", actor=user.username,
               target=order_no, result="skipped",
               params={"decision_log_id": log_id, "reason": "already_bound",
                       "bound_order_no": row.order_no})
        return
    row.order_no = order_no
    row.account_id = account.id
    # 一律写实际用券：券自动切换（§10）后实际券可能与决策推荐不同，流水以事实为准
    if coupon_code:
        row.coupon_code = coupon_code
    row.deduction_actual = deduction
    row.pay_actual = pay_actual
    plan = dict(row.plan_json or {})
    plan["order_no"] = order_no
    row.plan_json = plan
    db.commit()
    log_op(action="decision.backfill", actor=user.username, target=order_no,
           params={"decision_log_id": log_id, "account_id": account.id,
                   "coupon_code": coupon_code or None,
                   "deduction_actual": deduction, "pay_actual": pay_actual})


def _pay_link_payload(link, db: Session | None = None, account_id: int | None = None,
                      pay_mode: str | None = None, coupon_code: str | None = None, **extra) -> dict:
    """PayLink → create partial / continue-pay / pay(manual) 共用的响应字段。

    传 db（+account_id）时同步铸造/续铸 H5 支付会话：返回体附带 h5_url（真实支付宝
    收银台 URL，构造失败回退自建壳页）与 portal_url/pay_token；无 db 保持旧行为
    （h5_url=None、无 pay_token 键）以兼容存量调用与离线测试。
    pay_mode/coupon_code 仅在 create 场景显式传入——pay/续付场景不传以保留会话上的
    原有 mode/coupon（ensure_pay_session 空值跳过覆盖）。
    """
    if db is not None and account_id is not None:
        payload = pay_link_payload_with_session(db, link, account_id=account_id,
                                                mode=pay_mode, coupon_code=coupon_code)
    else:
        payload = {
            "result": "partial",
            "order_no": link.order_no,
            "pay_no": link.pay_no,
            "out_trade_no": link.out_trade_no,
            "total_amount": link.total_amount,
            "expire_at": link.expire_at,
            "pay_window_seconds": PAY_WINDOW_SECONDS,
            "order_str": link.order_str,
            "h5_url": None,
            # 时间戳同步契约：无会话（token 为空场景）无权威锚点，两字段 None
            "server_time": None,
            "pay_deadline_ts": None,
            "note": "支付宝侧扣款不在纯协议范围：人工模式请用手机完成支付；自动模式为实验性",
        }
    payload.update(extra)
    return payload


# ---------------- F5：试算（生成 draft） ----------------

@router.post("/settle")
def order_settle(account_id: int, body: OrderSettleRequest, request: Request,
                 db: Session = Depends(get_db),
                 user: SystemUser = Depends(require_perm("feature:order"))):
    started = time.time()   # oplog 耗时统计
    account = _get_account(db, account_id)
    # 请求体 → target（App 立即购买路径：goods/detail → calculatePrice → settlePrice 直发，
    # 全程无购物车端点——2026-09-26 抓包定案；价格占位/缺必选加料会被 [9105050200005] 拒绝）
    target = {
        "storeNo": body.store_no,
        "spuId": body.spu_id,
        "spuName": body.spu_name,
        "skuId": body.sku_id,
        "skuName": body.sku_name or body.spu_name or "",   # 服务端 [99997] 校验非空：SKU 名缺失回退 SPU 名
        "itemSkuId": body.item_sku_id,
        "quantity": body.quantity,
        "salePrice": body.sale_price,                        # SKU 原价（数值，calculatePrice 入参）
        "specList": body.spec_list,
        "attributeList": body.attribute_list,   # 属性项（温度/甜度等，名称保留原始空格）
        "imageUrl": body.image_url,
        "spuType": body.spu_type or "stand",
        "nutritionInfo": body.nutrition_info,
    }
    spec_desc = "/".join(str(o.get("specOptionName") or o.get("specName") or o.get("specOptionId") or "")
                         for o in body.spec_list if isinstance(o, dict))
    goods_desc = (body.spu_name or body.sku_name) + f" x{body.quantity}" + (f"（{spec_desc}）" if spec_desc else "")
    try:
        api = bridge.trade_api(account)
        price = api.calculate_price(target)                 # 服务端算价（折后单价/总额/折扣明细）
        extra_entries = [api.build_extra_entry(o) for o in (body.extra_list or [])]
        settle_base = api.settle_direct(target, price, extra_entries=extra_entries)  # 不带券（服务端自荐推荐券）
    except Exception as e:
        _fail(account, db, request, user, "feature.order_settle", e)

    now = time.time()
    draft = {
        "draft_id": "d-" + uuidlib.uuid4().hex[:8],
        "target": target,
        "price": price,
        "extra_list": body.extra_list or [],
        "settle_base": settle_base,
        "store_no": body.store_no,
        "store_name": body.store_name,
        "goods_desc": goods_desc[:255],
        "created_at": now,
    }
    with _draft_lock:
        _drafts[account_id] = draft   # 新 settle 覆盖旧 draft

    # 券档案同步：试算可用券全量入库（券ID↔token 映射 + 完整名称，来源 settle）
    for c in (settle_base.available_coupons or []):
        try:
            _upsert_coupon(db, account, c, "settle_available", "settle")
        except Exception as e:
            # 档案同步失败不阻塞下单主流程：吞异常继续（保持原 pass 语义），仅 WARN 留痕
            log_op(level="WARN", action="coupon.archive_sync", actor=user.username,
                   target=f"{account.label}#{account.id}", result="failed", error=e)

    # 预览：estimated_pay = max(total − 推荐券抵扣, 0)；无推荐券时即 total（scenario 按 total>0 判定）
    # 推荐抵扣优先取服务端事实：settle 请求 recommendCoupon=true 时服务端自行选券并回填
    # discountList/totalDiscountAmount（折扣率券也准确）；无回填再回退本地折率感知预估
    total = Decimal(str(settle_base.total_trade_price or "0"))
    server_deduction = Decimal(str((settle_base.trade_fund_info or {}).get("totalDiscountAmount") or "0"))
    if settle_base.discount_list and server_deduction > 0:
        deduction = server_deduction
        rec_codes = {str(r.get("discountId")) for r in settle_base.discount_list}
        recommended = next((c for c in (settle_base.available_coupons or [])
                            if str(c.get("couponCode")) in rec_codes), None)
    else:
        recommended, deduction = bridge.ChageeTradeApi.pick_coupon(
            settle_base.available_coupons, settle_base.total_trade_price)
    estimated = max(total - deduction, Decimal(0))
    goods = [{
        "name": (body.sku_name or body.spu_name or ""),
        "quantity": body.quantity,
        "price": _fmt_money(settle_base.total_trade_price),
    }]
    preview = {
        "goods": goods,
        "total_trade_price": _fmt_money(settle_base.total_trade_price),
        "buyer_real_price": _fmt_money(settle_base.buyer_real_price),
        "unit_price": str(price.get("totalGoodsItemPrice") or ""),
        "available_coupons": settle_base.available_coupons,   # 原样序列化（券面额/有效期/门槛直接透传）
        "recommended_coupon": recommended,
        "recommended_deduction": _fmt_money(deduction),
        "estimated_pay": _fmt_money(estimated),
        "scenario_preview": "zero" if estimated == 0 else "partial",
    }
    log_audit(db, request, user, "feature.order_settle", f"{account.label}#{account.id}",
              {"total": preview["total_trade_price"], "buyer_real": preview["buyer_real_price"],
               "coupons": len(settle_base.available_coupons), "estimated": preview["estimated_pay"],
               "scenario": preview["scenario_preview"]})
    log_op(action="feature.order_settle", actor=user.username,
           target=f"{account.label}#{account.id}",
           params={"goods": draft["goods_desc"], "store": body.store_no,
                   "total": preview["total_trade_price"], "estimated_pay": preview["estimated_pay"],
                   "coupons": len(settle_base.available_coupons),
                   "scenario": preview["scenario_preview"]},
           duration_ms=int((time.time() - started) * 1000))
    return {
        "draft_id": draft["draft_id"],
        "expires_at": datetime.fromtimestamp(now + DRAFT_TTL_SECONDS).strftime("%Y-%m-%d %H:%M:%S"),
        "preview": preview,
    }


# ---------------- F5：下单（zero / partial 双分支） ----------------

def _fallback_rank_codes(db: Session, draft: dict, primary_code: str, body) -> list[str]:
    """券自动切换的次优候选序列（§10 四级漏斗第 2/3 级 + §11 方案）：从本次试算在列券
    （settle_base.available_coupons = 服务端对本账号+购物车的权威可用集）构建候选，
    按成本规则折算 + 阈值过滤 + 策略排序 + 方案优先级层重排，返回券码列表（不含首选）。

    阈值口径：带 decision_log_id 时复用该决策流水的 threshold_json 与 revenue（与决策
    一致）；否则用全局配置且 revenue 未知 → 利润类阈值跳过、仅 max_order_cost 生效
    （执行期兜底语义——create 语境没有客户支付价，无法算利润）。
    方案口径：decision_log 的 plan_json.plan_id 优先，其次 body.plan_id（§11）。"""
    entries = [e for e in (draft["settle_base"].available_coupons or [])
               if str(e.get("couponCode") or "") not in ("", primary_code)]
    if not entries:
        return []
    rules = db.query(VoucherCostRule).all()
    cfg = decision_svc.load_config()
    min_profit = min_margin = max_cost = revenue = ""
    plan = None
    log_plan_id = 0
    if int(getattr(body, "decision_log_id", 0) or 0):
        row = db.get(DecisionLog, int(body.decision_log_id))
        if row is not None:
            tj = row.threshold_json or {}
            min_profit = str(tj.get("min_profit") or "")
            min_margin = str(tj.get("min_margin") or "")
            max_cost = str(tj.get("max_order_cost") or "")
            revenue = str(row.revenue or "")
            log_plan_id = int((row.plan_json or {}).get("plan_id") or 0)
    else:
        min_profit = str(cfg.get("min_profit") or "")
        min_margin = str(cfg.get("min_margin") or "")
        max_cost = str(cfg.get("max_order_cost") or "")
    for pid in (log_plan_id, int(getattr(body, "plan_id", 0) or 0)):
        if pid and plan is None:
            plan = db.get(OrderPlan, pid)
            break
    strategy = plan.strategy if plan is not None else "cost_first"
    candidates = []
    for e in entries:
        code = str(e.get("couponCode") or "")
        tname = str(e.get("templateName") or "")
        btext = str(e.get("benefitText") or "")
        cls = decision_svc.classify_coupon(btext, str(e.get("benefit2Text") or ""), tname, "")
        rec = {"coupon_code": code, "template_name": tname, "benefit_text": btext,
               "amount": str(cls["face"]) if cls["face"] is not None else ""}
        cost = decision_svc.resolve_cost(rules, rec, cfg)
        candidates.append({"record": rec, "cost": cost["cost"], "source": cost["source"],
                           "kind": cls["kind"], "face": cls["face"], "rate": cls["rate"],
                           "use_end_time": e.get("useEndTime")})
    ranked = decision_svc.rank_candidates(
        candidates, revenue, draft["settle_base"].total_trade_price,
        str(cfg.get("overhead") or "0"), min_profit, min_margin, max_cost,
        strategy=strategy)
    if plan is not None and plan.priorities:
        ranked, _tier_map = decision_svc.apply_priority_tiers(ranked, plan.priorities)
    return [c["record"]["coupon_code"] for c in ranked]

@router.post("/create")
def order_create(account_id: int, body: OrderCreateRequest, request: Request,
                 db: Session = Depends(get_db),
                 user: SystemUser = Depends(require_perm("feature:order"))):
    started = time.time()   # oplog 耗时统计
    account = _get_account(db, account_id)
    with _draft_lock:
        draft = _drafts.get(account_id)
    if (not draft or draft.get("draft_id") != body.draft_id
            or time.time() - draft.get("created_at", 0) > DRAFT_TTL_SECONDS):
        log_op(level="WARN", action="feature.order_create", actor=user.username,
               target=f"{account.label}#{account.id}", result="rejected",
               params={"reason": "draft_expired", "draft_id": body.draft_id})
        raise HTTPException(400, "草稿已过期，请重新试算")
    # 单次一单：该账号存在待支付订单（status==1）时拒绝再次下单
    pending = (db.query(OrderRecord)
                 .filter(OrderRecord.account_id == account_id, OrderRecord.status == 1)
                 .first())
    if pending:
        # 超时单先向茶姬核实，已自动取消则放行：pay_deadline 早于「现在-校准宽限期」时先调
        # reconcile_order 校准（pay_deadline 为空用 created_at+支付窗兜底），校准会把本地状态
        # 同步为真实态并回滚券，避免超时单永久卡死下单
        deadline = pending.pay_deadline or (
            pending.created_at + timedelta(seconds=PAY_WINDOW_SECONDS) if pending.created_at else None)
        if deadline and datetime.now() - deadline > timedelta(seconds=RECONCILE_GRACE_SECONDS):
            try:
                reconcile_order(db, pending, account, operator=user.username)
            except Exception as e:
                # 校准自身失败（如本地写库异常）不阻断下单主流程，重查后仍待支付则走 409
                db.rollback()
                log_op(level="WARN", action="order.reconcile_inline", actor=user.username,
                       target=f"{account.label}#{account.id}", result="failed", error=e)
        # 校准后重查待支付单：已消失（已取消/已支付均同步）则放行继续下单；
        # 仍存在（含校准返回 still_pending / skipped_error）则维持原 409
        pending = (db.query(OrderRecord)
                     .filter(OrderRecord.account_id == account_id, OrderRecord.status == 1)
                     .first())
    if pending:
        log_op(level="WARN", action="feature.order_create", actor=user.username,
               target=f"{account.label}#{account.id}", result="rejected",
               params={"reason": "pending_order", "pending_order_no": pending.order_no})
        raise HTTPException(409, f"存在待支付订单（{pending.order_no}），请先处理（支付/取消）后再下单")

    coupon_code = (body.coupon_code or "").strip()
    coupon_entry = None
    if coupon_code and not body.auto_fallback:
        # ⑤ 试算在列：券必须出现在本次试算的可用券列表（服务端对该账号/门店/购物车语境的可用集合）
        coupon_entry = next((c for c in draft["settle_base"].available_coupons
                             if str(c.get("couponCode")) == coupon_code), None)
        if coupon_entry is None:
            _log_coupon_usage(db, account, user, coupon_code, "", "rejected",
                              fail_reason="券不在本次试算可用券列表中（需重新试算）")
            log_op(level="WARN", action="coupon.rejected", actor=user.username,
                   target=f"{account.label}#{account.id}", result="rejected",
                   params={"coupon_code": coupon_code, "reason": "not_in_settle_available"})
            raise HTTPException(400, f"券 {coupon_code} 不在试算可用券列表中，请重新试算")
        # ①~④ 有效性验证：归属映射 / 可用性 / 有效期 / 门槛（拒绝即记使用日志并中止）
        reject = _validate_coupon(db, account, draft, coupon_code, coupon_entry)
        if reject:
            _log_coupon_usage(db, account, user, coupon_code,
                              coupon_entry.get("templateName") or "", "rejected",
                              fail_reason=reject)
            log_op(level="WARN", action="coupon.rejected", actor=user.username,
                   target=f"{account.label}#{account.id}", result="rejected",
                   params={"coupon_code": coupon_code, "reason": reject})
            raise HTTPException(400, f"优惠券验证未通过：{reject}")
    try:
        api = bridge.trade_api(account)
        settle = draft["settle_base"]
        rows = None
        coupon_deduction = Decimal(0)
        if coupon_code and body.auto_fallback:
            # —— 券自动切换（§10）：验证失败/不在列/settle 复跑券相关异常 → 次优券重试 ——
            # 候选序列 = 首选 + 按四级漏斗重排的本次试算在列券（同账号），最多试 3 张（含首选）；
            # 每张被跳过的券记 rejected 使用日志（fail_reason 带「券自动切换跳过」前缀）+ oplog，
            # 全部耗尽 → 400 列明各券失败原因。createOrder 本身不在重试范围（成单边界）。
            queue = [coupon_code] + _fallback_rank_codes(db, draft, coupon_code, body)
            tried = []
            extras = [api.build_extra_entry(o) for o in (draft.get("extra_list") or [])]
            for code in queue[:3]:
                entry = next((c for c in draft["settle_base"].available_coupons
                              if str(c.get("couponCode")) == code), None)
                reject = ("券不在本次试算可用券列表中（已被使用/失效或服务端不再认可）"
                          if entry is None else _validate_coupon(db, account, draft, code, entry))
                if reject is None:
                    try:
                        settle = api.settle_direct(draft["target"], draft.get("price") or {},
                                                   coupon_entry=entry, extra_entries=extras)
                    except Exception as se:   # 服务端对该券的实时拒绝（券态变化）→ 降级下一张
                        db.rollback()
                        reject = f"settle复跑异常: {type(se).__name__}: {se}"[:120]
                if reject:
                    tried.append(f"{code}（{reject}）")
                    _log_coupon_usage(db, account, user, code,
                                      (entry or {}).get("templateName") or "", "rejected",
                                      fail_reason=f"[券自动切换跳过] {reject}")
                    log_op(level="INFO", action="coupon.fallback_skip", actor=user.username,
                           target=f"{account.label}#{account.id}", result="rejected",
                           params={"coupon_code": code, "reason": reject})
                    continue
                coupon_entry, coupon_code = entry, code
                # 下单抵扣行权威口径与原路径一致（服务端回填 discountList 优先）
                rows = settle.discount_rows_for(code) or [
                    api.build_discount_row(entry,
                                           api.expected_deduction(entry, settle.total_trade_price))]
                coupon_deduction = Decimal(str((settle.trade_fund_info or {})
                                               .get("totalDiscountAmount") or "0"))
                break
            if coupon_entry is None:
                # §11 库存检查话术：所有优先级券均使用失败 → 暂无库存
                raise HTTPException(400, "暂无库存：券自动切换全部失败，已尝试 " + "；".join(tried))
            if coupon_code != (body.coupon_code or "").strip():
                log_op(level="INFO", action="coupon.fallback_applied", actor=user.username,
                       target=f"{account.label}#{account.id}", result="ok",
                       params={"from": body.coupon_code, "to": coupon_code,
                               "tried": tried})
        elif coupon_entry is not None:
            # 选券复跑直连试算，取服务端回填金额后的最终 SettleResult（金额自动抵扣的服务端事实）
            settle = api.settle_direct(draft["target"], draft.get("price") or {},
                                       coupon_entry=coupon_entry,
                                       extra_entries=[api.build_extra_entry(o)
                                                      for o in (draft.get("extra_list") or [])])
            # 下单抵扣行唯一权威 = 服务端回填的 discountList 行（折扣率券「7折」的真实抵扣
            # 由茶姬按券型计算回填，杜绝本地换算口径差→一致性断言拦截）；无回填才退本地
            # 折率感知预估（2026-09-28 修复，docs/rate_coupon_fix_20260928.md）
            rows = settle.discount_rows_for(coupon_code) or [
                api.build_discount_row(coupon_entry,
                                       api.expected_deduction(coupon_entry, settle.total_trade_price))]
            coupon_deduction = Decimal(str((settle.trade_fund_info or {}).get("totalDiscountAmount") or "0"))
        outcome = api.create_order(settle, draft["store_no"], draft["store_name"] or "", rows)
    except HTTPException:
        raise   # fallback 耗尽等业务 400 直通，不被 _fail 改写为协议错误
    except Exception as e:
        if coupon_code:
            _log_coupon_usage(db, account, user, coupon_code,
                              (coupon_entry or {}).get("templateName") or "", "failed",
                              fail_reason=f"{type(e).__name__}: {e}"[:250])
            log_op(level="WARN", action="coupon.failed", actor=user.username,
                   target=f"{account.label}#{account.id}", result="failed",
                   params={"coupon_code": coupon_code}, error=e)
        _fail(account, db, request, user, "feature.order_create", e)

    if isinstance(outcome, bridge.OrderOutcome):
        # 零元单：无支付腿，直接进入制作中（wire 实证）
        _upsert_order(db, account_id, outcome.order_no,
                      store_no=draft["store_no"], store_name=draft["store_name"],
                      goods_desc=draft["goods_desc"], quantity=draft["target"].get("quantity", 1),
                      coupon_code=coupon_code, total_amount=settle.total_trade_price,
                      pay_amount=outcome.pay_amount, scenario="zero",
                      status=outcome.status, status_label=_status_label(outcome.status, outcome.status_text),
                      pickup_no=outcome.pickup_no)
        if coupon_code:
            _mark_coupon_used(db, coupon_code, outcome.order_no)
            _log_coupon_usage(db, account, user, coupon_code,
                              coupon_entry.get("templateName") or "", "success",
                              order_no=outcome.order_no,
                              deduction=_fmt_money(coupon_deduction),
                              total_amount=_fmt_money(settle.total_trade_price),
                              pay_amount=_fmt_money(outcome.pay_amount), scenario="zero")
        # 决策挂钩：decide 评估行回填真实成单信息（未传 decision_log_id 时零行为变化）
        _backfill_decision_log(db, body, account, outcome.order_no, coupon_code,
                               _fmt_money(coupon_deduction) if coupon_code else "",
                               outcome.pay_amount, user)
        log_audit(db, request, user, "feature.order_create", f"{account.label}#{account.id}",
                  {"result": "zero", "order_no": outcome.order_no,
                   "pay_amount": outcome.pay_amount, "coupon": coupon_code or None})
        log_op(action="feature.order_create", actor=user.username,
               target=f"{account.label}#{account.id}",
               params={"result_kind": "zero", "order_no": outcome.order_no,
                       "pay_amount": outcome.pay_amount, "coupon": coupon_code or None,
                       "scenario": "zero"},
               duration_ms=int((time.time() - started) * 1000))
        return {
            "result": "zero",
            "order_no": outcome.order_no,
            "status": outcome.status,
            "status_label": _status_label(outcome.status, outcome.status_text),
            "pickup_no": outcome.pickup_no,
            "pay_amount": outcome.pay_amount,
            "coupon_code": coupon_code or None,
        }

    # 差额单：PayLink（order_str 完整下发，10 分钟支付窗）
    link = outcome
    rec = _upsert_order(db, account_id, link.order_no,
                        store_no=draft["store_no"], store_name=draft["store_name"],
                        goods_desc=draft["goods_desc"], quantity=draft["target"].get("quantity", 1),
                        coupon_code=coupon_code, total_amount=settle.total_trade_price,
                        pay_amount=settle.buyer_real_price, scenario="partial",
                        status=1, status_label=ORDER_STATUS_LABELS[1],
                        out_trade_no=link.out_trade_no, pay_deadline=_parse_deadline(link.expire_at))
    if rec is not None and not rec.order_target:
        # settle target 快照：switch-full-price 原价重下的逐字段复用依据（只补一次）
        rec.order_target = _order_target_snapshot(draft)
        db.commit()
    if coupon_code:
        _mark_coupon_used(db, coupon_code, link.order_no)
        _log_coupon_usage(db, account, user, coupon_code,
                          coupon_entry.get("templateName") or "", "success",
                          order_no=link.order_no,
                          deduction=_fmt_money(coupon_deduction),
                          total_amount=_fmt_money(settle.total_trade_price),
                          pay_amount=_fmt_money(settle.buyer_real_price), scenario="partial")
    # 决策挂钩：decide 评估行回填真实成单信息（未传 decision_log_id 时零行为变化）
    _backfill_decision_log(db, body, account, link.order_no, coupon_code,
                           _fmt_money(coupon_deduction) if coupon_code else "",
                           link.total_amount, user)
    log_audit(db, request, user, "feature.order_create", f"{account.label}#{account.id}",
              {"result": "partial", "order_no": link.order_no,
               "pay_amount": settle.buyer_real_price, "coupon": coupon_code or None})
    log_op(action="feature.order_create", actor=user.username,
           target=f"{account.label}#{account.id}",
           params={"result_kind": "partial", "order_no": link.order_no,
                   "pay_amount": settle.buyer_real_price, "coupon": coupon_code or None,
                   "scenario": "partial"},
           duration_ms=int((time.time() - started) * 1000))
    return _pay_link_payload(link, db=db, account_id=account_id,
                             pay_mode="partial" if coupon_code else "full",
                             coupon_code=coupon_code)


# ---------------- F5：支付模式 / 续付 / 取消 ----------------

@router.post("/{order_no}/pay")
def order_pay(account_id: int, order_no: str, body: PayModeRequest, request: Request,
              db: Session = Depends(get_db),
              user: SystemUser = Depends(require_perm("feature:order"))):
    account = _get_account(db, account_id)
    autopay_fn = None
    if body.mode == "auto":
        # try-import 接缝（契约 §6）：Agent D 的 scripts/alipay_autopay.py，模块缺失即 501
        try:
            from alipay_autopay import autopay as autopay_fn  # type: ignore[no-redef]
        except Exception as e:
            log_op(level="WARN", action="feature.order_pay", actor=user.username,
                   target=f"{account.label}#{account.id}", result="failed",
                   params={"mode": "auto", "stage": "import"})
            raise HTTPException(501, "自动支付模块未就绪（scripts/alipay_autopay.py 缺失或不可导入: "
                                     f"{type(e).__name__}）。请部署该模块并按需配置 "
                                     "account_system/data/autopay_config.json（会话 Cookie 等），或改用人工支付模式。")
    try:
        api = bridge.trade_api(account)
        link = api.continue_pay(order_no)   # 重铸全新支付串（10 分钟窗口内有效）
    except Exception as e:
        _fail(account, db, request, user, "feature.order_pay", e)
    _upsert_order(db, account_id, order_no, out_trade_no=link.out_trade_no,
                  pay_deadline=_parse_deadline(link.expire_at))
    if body.mode == "manual":
        log_op(action="feature.order_pay", actor=user.username,
               target=f"{account.label}#{account.id}",
               params={"mode": "manual", "order_no": order_no, "out_trade_no": link.out_trade_no})
        return _pay_link_payload(link, db=db, account_id=account_id, mode="manual", guide=(
            "人工支付指引：打开 h5_url 进入支付宝收银台完成付款；或复制下方支付串（order_str），"
            "在手机支付宝「扫一扫→相册/粘贴」完成付款；"
            f"支付窗口 {PAY_WINDOW_SECONDS // 60} 分钟，超时订单自动取消。支付完成后请到取餐查询页确认状态。"))
    # auto：实验性接缝，异常一律 501 并带配置说明
    try:
        result = autopay_fn(link.order_str)
    except Exception as e:
        log_op(level="ERROR", action="feature.order_pay", actor=user.username,
               target=f"{account.label}#{account.id}", result="failed",
               params={"mode": "auto", "stage": "execute"}, error=e)
        raise HTTPException(501, f"自动支付执行失败（{type(e).__name__}: {e}）。"
                                 "请检查 account_system/data/autopay_config.json 配置（会话 Cookie 等），或改用人工支付模式。")
    log_audit(db, request, user, "feature.order_autopay", f"{account.label}#{account.id}",
              {"order_no": link.order_no, "out_trade_no": link.out_trade_no,
               "ok": result.get("ok") if isinstance(result, dict) else None,
               "autopay_status": result.get("status") if isinstance(result, dict) else None})
    log_op(action="feature.order_pay", actor=user.username,
           target=f"{account.label}#{account.id}",
           params={"mode": "auto", "order_no": link.order_no,
                   "ok": result.get("ok") if isinstance(result, dict) else None})
    return {"mode": "auto", "experimental": True,
            "order_no": link.order_no, "out_trade_no": link.out_trade_no,
            "autopay": result}


@router.post("/{order_no}/continue-pay")
def order_continue_pay(account_id: int, order_no: str, request: Request,
                       db: Session = Depends(get_db),
                       user: SystemUser = Depends(require_perm("feature:order"))):
    account = _get_account(db, account_id)
    try:
        api = bridge.trade_api(account)
        link = api.continue_pay(order_no)
    except Exception as e:
        _fail(account, db, request, user, "feature.order_continue_pay", e)
    _upsert_order(db, account_id, order_no, out_trade_no=link.out_trade_no,
                  pay_deadline=_parse_deadline(link.expire_at))
    log_audit(db, request, user, "feature.order_continue_pay", f"{account.label}#{account.id}",
              {"order_no": link.order_no, "out_trade_no": link.out_trade_no,
               "total_amount": link.total_amount, "expire_at": link.expire_at})
    log_op(action="feature.order_continue_pay", actor=user.username,
           target=f"{account.label}#{account.id}",
           params={"order_no": order_no, "out_trade_no": link.out_trade_no,
                   "expire_at": link.expire_at})
    return _pay_link_payload(link, db=db, account_id=account_id)


@router.post("/{order_no}/cancel")
def order_cancel(account_id: int, order_no: str, request: Request,
                 db: Session = Depends(get_db),
                 user: SystemUser = Depends(require_perm("feature:order"))):
    account = _get_account(db, account_id)
    try:
        api = bridge.trade_api(account)
        resp = api.cancel(order_no)   # cancelOrder 静态端点，无 wire 样本——实验性
    except Exception as e:
        _fail(account, db, request, user, "feature.order_cancel", e)
    # 取消成功：回写本地订单快照为已取消（status=7）；该单用券时回滚券使用统计（rolled_back）
    order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
    coupon_rolled_back = False
    if order:
        order.status = 7
        order.status_label = "已取消"
        if order.coupon_code:
            coupon_rolled_back = rollback_coupon_usage(db, order.coupon_code, order_no,
                                                        operator=user.username)
    db.commit()
    # 支付会话联动（2026-09-28 取消链路漏洞修复）：此前只置 OrderRecord=7+券回滚，
    # PaySession 停在 issued——H5 收银台倒计时照走、watcher 继续探针已取消的单。
    # mark_session CAS 置 cancelled（无会话/已终态返回 False 静默），成功才记事件
    # （事件数与状态迁移严格一对一，与探针/watcher 同口径）；跨进程 SSE 通知由
    # mark_session 内部自动发出。
    sess = get_by_order_no(db, order_no)
    if mark_session(db, order_no, "cancelled"):
        record_event(db, order_no, token_prefix(sess.pay_token) if sess else "",
                     EVENT_ORDER_CANCELLED,
                     {"source": "manual-cancel", "operator": user.username})
    log_audit(db, request, user, "feature.order_cancel", f"{account.label}#{account.id}",
              {"order_no": order_no, "experimental": True})
    log_op(action="feature.order_cancel", actor=user.username,
           target=f"{account.label}#{account.id}",
           params={"order_no": order_no, "coupon_rolled_back": coupon_rolled_back,
                   "experimental": True})
    return {"experimental": True, "order_no": order_no, "coupon_rolled_back": coupon_rolled_back,
            "response": resp}


@router.post("/{order_no}/switch-full-price")
def order_switch_full_price(account_id: int, order_no: str, request: Request,
                            db: Session = Depends(get_db),
                            user: SystemUser = Depends(require_perm("feature:order"))):
    """券差额单 → 原价单（管理侧）：取消旧单并退还优惠券，用下单快照原价重下新单。
    语义与 H5 收银台 POST /pay/{token}/switch-full-price 一致（核心逻辑共用
    services.pay_session.switch_order_to_full_price），区别仅在鉴权与审计主体。"""
    account = _get_account(db, account_id)
    order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
    if not order or order.account_id != account_id:
        raise HTTPException(404, "订单不存在")
    if int(order.status or 0) != 1:
        raise HTTPException(409, f"订单当前状态不允许切换（status={order.status}，仅待支付可切换）")
    if order.scenario != "partial" or not order.coupon_code:
        raise HTTPException(400, "该订单非券差额单，无需切换原价")
    if not (order.order_target or {}).get("target"):
        raise HTTPException(400, "历史订单缺少商品快照，无法原价重下（仅快照落库后的新订单支持）")
    try:
        result = switch_order_to_full_price(db, account, order, operator=user.username)
    except bridge.SessionExpiredError as e:
        _fail(account, db, request, user, "feature.order_switch_full_price", e)
    except PaySessionError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.warning("原价重下失败（管理端）order_no=%s: %s", order_no, type(e).__name__,
                       exc_info=True)
        _fail(account, db, request, user, "feature.order_switch_full_price", e)
    log_audit(db, request, user, "feature.order_switch_full_price", f"{account.label}#{account.id}",
              {"old_order_no": result.get("old_order_no"), "new_order_no": result.get("new_order_no"),
               "coupon_rolled_back": result.get("coupon_rolled_back")})
    log_op(action="feature.order_switch_full_price", actor=user.username,
           target=f"{account.label}#{account.id}",
           params={"old_order_no": result.get("old_order_no"),
                   "new_order_no": result.get("new_order_no"),
                   "coupon_rolled_back": result.get("coupon_rolled_back")})
    return result


@router.post("/{order_no}/cashier-url")
def order_cashier_url(account_id: int, order_no: str, body: CashierUrlRequest, request: Request,
                      db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("feature:order"))):
    """回填云手机实时捕获的支付宝官方收银台 URL（mobilegw 会话仅捕获后短窗有效——
    2026-09-27 实证：过期后打开显示「你的访问已超时」mobileclientgw-42-9246）。
    流程：云手机 App 内拉起该单支付 → 从 h5 链路抓到新 cashierRoutePay 链接 → 贴到本端点
    → 壳页/info 与本响应即带新链接，浏览器直开官方收银台付款。"""
    _get_account(db, account_id)
    sess = get_by_order_no(db, order_no)
    if not sess or sess.account_id != account_id:
        raise HTTPException(404, "该订单没有支付会话（先获取支付串铸造会话）")
    if sess.status != "issued":
        raise HTTPException(409, f"会话当前状态 {sess.status} 不可回填（仅待支付可回填）")
    url = body.url.strip()
    if not (url.startswith("https://mclient.alipay.com/cashierRoutePay.htm") and "session=" in url):
        raise HTTPException(400, "链接形态不符：需要 https://mclient.alipay.com/cashierRoutePay.htm?...&session=... 的完整收银台 URL")
    sess.alipay_cashier_url = url[:512]
    db.commit()
    # 同步生成支付参数串快照（独立表；URL 参数不完整时跳过，不影响已回填的 URL）
    try:
        pay_params.save_pay_params(db, order_no, url, source="manual", sess=sess)
    except Exception:
        logger.warning("支付参数串生成失败（忽略）order_no=%s", order_no, exc_info=True)
    record_event(db, order_no, token_prefix(sess.pay_token), EVENT_CASHIER_UPDATED,
                 {"url_prefix": url[:80], "operator": user.username})
    log_audit(db, request, user, "feature.order_cashier_url", f"#{account_id}",
              {"order_no": order_no, "url_prefix": url[:80]})
    log_op(action="feature.order_cashier_url", actor=user.username, target=order_no,
           params={"url_prefix": url[:80]})
    return {"order_no": order_no, "alipay_cashier_url": sess.alipay_cashier_url,
            "h5_url": build_h5_url(sess.pay_token), "pay_token": sess.pay_token,
            **pay_params.payload_fields(db, order_no),
            "note": "mobilegw 会话短窗有效：回填后尽快在浏览器打开完成支付，超时重新捕获再回填"}


@router.get("/{order_no}/cashier")
def order_cashier_status(account_id: int, order_no: str, request: Request,
                         db: Session = Depends(get_db),
                         user: SystemUser = Depends(require_perm("feature:order"))):
    """frida 自动铸造的收银台直链状态查询（轮询用；get 支付串/续付时已自动触发铸造）。"""
    _get_account(db, account_id)
    sess = get_by_order_no(db, order_no)
    if not sess or sess.account_id != account_id:
        raise HTTPException(404, "该订单没有支付会话（先获取支付串铸造会话）")
    # try-import 接缝：铸造编排属旁路模块，缺失不影响状态查询（仅失去 alipay_cashier_url 透出）
    try:
        from services.cashier_mint import mint_status
        status = mint_status(order_no)
    except Exception:
        logger.warning("查询铸造状态失败（回退本地字段）order_no=%s", order_no, exc_info=True)
        status = None
    if not status:
        status = {"order_no": order_no,
                  "alipay_cashier_url": sess.alipay_cashier_url or None,
                  "session_status": sess.status}
    return {**status,
            "note": "铸造中请稍候；完成后 alipay_cashier_url 非空；重试=续付"}


@router.get("/{order_no}/pay-params")
def order_pay_params(account_id: int, order_no: str, request: Request,
                     db: Session = Depends(get_db),
                     user: SystemUser = Depends(require_perm("feature:order"))):
    """官方收银台支付参数串显式读取（JSON v1 契约见 services/pay_params.py；
    下单/续付响应、cashier 轮询均带同源字段，本端点是外部 Python 支付脚本/调试的稳定入口）。"""
    _get_account(db, account_id)
    sess = get_by_order_no(db, order_no)
    if not sess or sess.account_id != account_id:
        raise HTTPException(404, "该订单没有支付会话（先获取支付串铸造会话）")
    fields = pay_params.payload_fields(db, order_no)
    return {"order_no": order_no, "session_status": sess.status,
            "alipay_cashier_url": sess.alipay_cashier_url or None,
            "generated": fields["pay_params"] is not None,
            **fields,
            "note": "pay_param_str 为官方收银台全量支付参数（JSON v1，紧凑原文与库内存储一致）；"
                    "alipay_cashier_url 为空表示尚未捕获/铸造，可续付触发重铸"}


# ---------------- F6：订单查询（不审计；SessionExpired 仍置 expired+审计） ----------------

@router.get("")
def order_list_endpoint(account_id: int, request: Request,
                        tab: str = Query("today", pattern="^(today|history)$"),
                        page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=50),
                        db: Session = Depends(get_db),
                        user: SystemUser = Depends(require_perm("feature:pickup"))):
    account = _get_account(db, account_id)
    try:
        api = bridge.trade_api(account)
        rows = api.order_list(tab, page, page_size)
    except Exception as e:
        _fail(account, db, request, user, "feature.order_list", e)
    items = []
    for r in rows:
        st = int(r.get("orderStatus") or 0)
        label = _status_label(st, r.get("orderStatusText") or "")
        items.append({
            "order_no": r.get("orderNo") or "",
            "order_status": st,
            "status_label": label,
            "pickup_no": r.get("pickupNo") or "",
            "pay_amount": str(r.get("payAmount") or ""),
            "total_amount": str(r.get("totalAmount") or ""),
            "pay_type_text": r.get("payTypeText") or "",
            "store_no": r.get("storeNo") or "",
            "store_name": r.get("storeName") or "",
            "order_time": r.get("orderTime") or "",
            "unique_pos_order_no": r.get("uniquePosOrderNo") or "",
            "can_waiting": st == 3,
        })
        _upsert_order(db, account_id, r.get("orderNo"), **_order_row_fields(r))
    return {"total": len(rows), "items": items}


@router.get("/{order_no}")
def order_detail_endpoint(account_id: int, order_no: str, request: Request,
                          db: Session = Depends(get_db),
                          user: SystemUser = Depends(require_perm("feature:pickup"))):
    account = _get_account(db, account_id)
    try:
        api = bridge.trade_api(account)
        d = api.order_detail(order_no)
    except Exception as e:
        _fail(account, db, request, user, "feature.order_detail", e)
    st = int(d.get("orderStatus") or 0)
    label = _status_label(st, d.get("orderStatusText") or "")
    promotions = [{
        "promotionId": p.get("promotionId"),
        "promotionName": p.get("promotionName") or "",
        "discountAmount": str(p.get("discountAmount") or ""),
    } for p in (d.get("orderPromotions") or [])]
    _upsert_order(db, account_id, order_no,
                  store_no=d.get("storeNo"), store_name=d.get("storeName"),
                  status=st, status_label=label,
                  pickup_no=d.get("pickupNo"), unique_pos_order_no=d.get("uniquePosOrderNo"),
                  pay_amount=str(d.get("payAmount") or ""),
                  total_amount=str(d.get("totalAmount") or ""),
                  order_time=d.get("orderTime") or "", biz_type=d.get("businessTypeText") or "")
    return {
        "order_no": d.get("orderNo") or order_no,
        "status": st,
        "status_label": label,
        "pay_amount": str(d.get("payAmount") or ""),
        "total_amount": str(d.get("totalAmount") or ""),
        "pay_type_text": d.get("payTypeText") or "",
        "pickup_no": d.get("pickupNo") or "",
        "unique_pos_order_no": d.get("uniquePosOrderNo") or "",
        "store_no": d.get("storeNo") or "",
        "store_name": d.get("storeName") or "",
        "order_time": d.get("orderTime") or "",
        "pay_time": d.get("payTime") or "",
        "items": d.get("orderItems") or [],
        "promotions": promotions,
        "payment_expiry_ts": d.get("paymentExpiryTimestamp"),   # 仅待支付态非空
    }


@router.get("/{order_no}/status")
def order_status_endpoint(account_id: int, order_no: str, request: Request,
                          db: Session = Depends(get_db),
                          user: SystemUser = Depends(require_perm("feature:pickup"))):
    account = _get_account(db, account_id)
    try:
        api = bridge.trade_api(account)
        st = api.order_status(order_no)   # getOrderStatus 轻探针，data 裸 int
    except Exception as e:
        _fail(account, db, request, user, "feature.order_status", e)
    return {"status": int(st), "status_label": ORDER_STATUS_LABELS.get(int(st), str(st))}


@router.get("/{order_no}/waiting")
def order_waiting(account_id: int, order_no: str, request: Request,
                  db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("feature:pickup"))):
    account = _get_account(db, account_id)
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
    store_no = (rec.store_no if rec else "") or ""
    unique_pos = (rec.unique_pos_order_no if rec else "") or ""
    try:
        api = bridge.trade_api(account)
        if not (store_no and unique_pos):
            d = api.order_detail(order_no)   # 缺参时先补详情（并回填 OrderRecord）
            store_no = store_no or d.get("storeNo") or ""
            unique_pos = unique_pos or d.get("uniquePosOrderNo") or ""
            _upsert_order(db, account_id, order_no, store_no=d.get("storeNo"),
                          store_name=d.get("storeName"), status=d.get("orderStatus"),
                          status_label=_status_label(d.get("orderStatus"), d.get("orderStatusText") or ""),
                          pickup_no=d.get("pickupNo"), unique_pos_order_no=d.get("uniquePosOrderNo"))
        if not (store_no and unique_pos):
            raise HTTPException(400, "订单缺少门店编码/POS 单号（uniquePosOrderNo），无法查询等待信息")
        info = api.waiting_info(store_no, order_no, unique_pos)
    except HTTPException:
        raise
    except Exception as e:
        _fail(account, db, request, user, "feature.order_waiting", e)
    return {
        "waiting_cups": info.get("waitingCups"),
        "waiting_time": info.get("waitingTime"),
        "queue_limit": info.get("queueLimit"),
    }


# ---------------- 订单校准：超时待支付单向茶姬核实（全局手动触发） ----------------

@global_router.post("/orders/reconcile")
def orders_reconcile(request: Request, db: Session = Depends(get_db),
                     user: SystemUser = Depends(require_perm("feature:order"))):
    """手动校准全部账号的待支付订单：逐单向茶姬核实真实状态——已超时自动取消的同步本地
    状态并回滚券使用（rolled_back），已支付确认的回写完成态；单账号/单单网络失败不中断
    整体遍历（实现见 services/order_reconcile.py）。"""
    result = reconcile_expired_pending_orders(operator=user.username)
    log_audit(db, request, user, "feature.order_reconcile", "全账号", result)
    log_op(action="feature.order_reconcile", actor=user.username, target="全账号",
           params={k: result.get(k) for k in ("scanned", "cancelled", "confirmed",
                                              "pending", "skipped")})
    return {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"), "reconciled": result}


# ---------------- H5 收银台：支付会话事件流查询（需求5/6 的对账视图） ----------------

@global_router.get("/pay-events")
def pay_events(
    order_no: str = Query("", max_length=64),
    event: str = Query("", max_length=32),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(require_perm("feature:order")),
):
    """支付会话事件流分页查询：链接铸造/页面访问/探针/支付发现/取餐码回写/回调/回滚/
    原价切换全链路（时间线即问题排查依据）；order_no 精确过滤 + event 精确过滤。"""
    q = db.query(PayEventLog)
    if order_no:
        q = q.filter(PayEventLog.order_no == order_no)
    if event:
        q = q.filter(PayEventLog.event == event)
    total = q.count()
    rows = (q.order_by(PayEventLog.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total,
        "items": [{
            "id": r.id, "order_no": r.order_no, "pay_token_prefix": r.pay_token_prefix,
            "event": r.event, "payload": r.payload, "created_at": r.created_at,
        } for r in rows],
    }


# ---------------- 券使用记录与券档案查询（需求6：查询与统计分析） ----------------

_RESULT_LABELS = {"success": "使用成功", "rejected": "验证拒绝", "failed": "下单失败",
                  "rolled_back": "已回滚（取消退券）"}


@global_router.get("/coupon-usage-logs")
def coupon_usage_logs(
    keyword: str = Query("", max_length=64),
    account_id: int = Query(0),
    result: str = Query("", pattern="^(success|rejected|failed|rolled_back)?$"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(require_perm("feature:order")),
):
    q = (db.query(CouponUsageLog)
           .outerjoin(ChageeAccount, CouponUsageLog.account_id == ChageeAccount.id))
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(CouponUsageLog.coupon_code.like(like)
                     | CouponUsageLog.order_no.like(like)
                     | CouponUsageLog.coupon_name.like(like)
                     | CouponUsageLog.operator.like(like)
                     | CouponUsageLog.account_label.like(like)
                     | ChageeAccount.phone.like(like))
    if account_id:
        q = q.filter(CouponUsageLog.account_id == account_id)
    if result:
        q = q.filter(CouponUsageLog.result == result)
    total = q.count()
    rows = (q.order_by(CouponUsageLog.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    # 归属账号手机号回填：mask_phone 已改为返回完整号码（2026-09-28 起不再脱敏）
    from schemas import mask_phone
    acc_ids = {r.account_id for r in rows if r.account_id}
    acc_map = {a.id: a for a in db.query(ChageeAccount).filter(ChageeAccount.id.in_(acc_ids)).all()} if acc_ids else {}
    owned = set(user.role.permissions or []) if user.role else set()
    can_full_phone = "account:update" in owned

    def _phone_fields(account_id: int) -> dict:
        acc = acc_map.get(account_id)
        if not acc or not acc.phone:
            return {}
        out = {"account_phone_masked": mask_phone(acc.phone)}
        if can_full_phone:
            out["account_phone_full"] = acc.phone
        return out

    # 统计摘要（当前筛选下的分类计数与累计抵扣）
    stat = {"success": 0, "rejected": 0, "failed": 0, "rolled_back": 0,
            "total_deduction": Decimal("0")}
    for r in q.with_entities(CouponUsageLog.result, CouponUsageLog.deduction).all():
        if r[0] in stat:
            stat[r[0]] += 1
        if r[0] == "success":
            try:
                stat["total_deduction"] += Decimal(str(r[1] or "0"))
            except Exception:
                pass
        elif r[0] == "rolled_back":
            # 回滚视为券未使用，从累计抵扣中扣除（下限 0）
            try:
                stat["total_deduction"] -= Decimal(str(r[1] or "0"))
            except Exception:
                pass
    if stat["total_deduction"] < 0:
        stat["total_deduction"] = Decimal("0")
    return {
        "total": total,
        "stats": {**stat, "total_deduction": _fmt_money(stat["total_deduction"])},
        "items": [{
            "id": r.id, "used_at": r.used_at,
            "result": r.result, "result_label": _RESULT_LABELS.get(r.result, r.result),
            "coupon_code": r.coupon_code, "coupon_name": r.coupon_name,
            "account_label": r.account_label, **_phone_fields(r.account_id),
            "operator": r.operator,
            "order_no": r.order_no, "deduction": r.deduction,
            "total_amount": r.total_amount, "pay_amount": r.pay_amount,
            "scenario": r.scenario, "fail_reason": r.fail_reason,
        } for r in rows],
    }


@global_router.get("/accounts/{account_id}/coupons/records")
def coupon_records(
    account_id: int,
    keyword: str = Query("", max_length=64),
    bucket: str = Query(""),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(require_perm("feature:order")),
):
    """账号券档案（券ID↔token 映射 + 完整名称全量存档，含使用痕迹）。"""
    _get_account(db, account_id)
    q = db.query(CouponRecord).filter(CouponRecord.account_id == account_id)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(CouponRecord.coupon_code.like(like)
                     | CouponRecord.template_name.like(like))
    if bucket:
        q = q.filter(CouponRecord.bucket == bucket)
    total = q.count()
    rows = (q.order_by(CouponRecord.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total,
        "items": [{
            "id": r.id, "coupon_code": r.coupon_code,
            "template_name": r.template_name,
            "benefit_text": r.benefit_text, "benefit2_text": r.benefit2_text,
            "amount": r.amount, "usable_scenes": r.usable_scenes,
            "threshold_tips": r.threshold_tips,
            "use_start_time": r.use_start_time, "use_end_time": r.use_end_time,
            "can_discount": r.can_discount, "bucket": r.bucket,
            "token_fingerprint": r.token_fingerprint,
            "last_used_at": r.last_used_at, "last_order_no": r.last_order_no,
            "updated_at": r.updated_at,
        } for r in rows],
    }


# 券成本/子类联动字段兜底值（上下文加载或逐张计算失败时 fail-soft，不阻塞搜索主流程）
_COST_FIELDS_FALLBACK = {"cost_price": "", "cost_source": "",
                         "cost_category_id": 0, "cost_category_name": "", "biz_type": ""}


@global_router.get("/coupons/search")
def coupons_search(
    keyword: str = Query("", max_length=64),
    bucket: str = Query("", pattern="^(effective|historical|settle_available)?$"),
    account_id: int = Query(0),
    scene: str = Query("", max_length=32, description="使用范围筛选（如 自取/外卖/团餐）"),
    cost_category: int = Query(0, description="券成本子类筛选（0=不筛，-1=未分类；匹配含正则语义，须 resolve 后内存过滤）"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(require_perm("feature:coupon")),
):
    """全库券档案模糊搜索：keyword 单串多维度模糊匹配（券码/券名/权益/使用范围/门槛/token 指纹/归属账号），
    可叠加 bucket / 使用范围 / 归属账号 / 券成本子类 过滤；返回分页明细 + 命中维度统计（联动仪表盘）。

    券成本联动（子代理 F，2026-09-28）：每行带 cost_price/cost_source（resolve_cost：
    "rule:N"|"fallback"）与 cost_category_id/name/biz_type（classify_category 子类归类，
    0=未分类）——每请求查一次 enabled 成本规则/子类 + decision 配置，循环内复用；
    整体 try fail-soft，失败时这些字段给默认值不阻塞搜索。cost_category 筛选因匹配为
    正则语义无法下推 SQL，在 resolve/classify 后内存过滤，分页 total 按过滤后计；
    stats.by_category（含 0=未分类）统计当前 keyword/bucket/scene/account 筛选后的全集。"""
    q = db.query(CouponRecord).join(ChageeAccount, CouponRecord.account_id == ChageeAccount.id, isouter=True)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(CouponRecord.coupon_code.like(like)
                     | CouponRecord.template_name.like(like)
                     | CouponRecord.benefit_text.like(like)
                     | CouponRecord.benefit2_text.like(like)
                     | CouponRecord.usable_scenes.like(like)
                     | CouponRecord.threshold_tips.like(like)
                     | CouponRecord.token_fingerprint.like(like)
                     | ChageeAccount.label.like(like))
    if bucket:
        q = q.filter(CouponRecord.bucket == bucket)
    if account_id:
        q = q.filter(CouponRecord.account_id == account_id)
    if scene:
        q = q.filter(CouponRecord.usable_scenes.like(f"%{scene}%"))
    rows = q.order_by(CouponRecord.id.desc()).all()

    # 券成本/子类上下文（每请求一次，循环内复用）；加载失败 → rules=None 触发全行兜底
    rules = categories = None
    cfg: dict = {}
    try:
        rules = (db.query(VoucherCostRule)
                   .filter(VoucherCostRule.enabled.is_(True)).all())
        categories = (db.query(VoucherCostCategory)
                        .filter(VoucherCostCategory.enabled.is_(True)).all())
        cfg = decision_svc.load_config()
    except Exception:
        logger.warning("券成本/子类联动加载失败（搜索降级默认值）", exc_info=True)
        rules = categories = None

    def _cost_fields(r: CouponRecord) -> dict:
        """券 → 成本+子类字段（fail-soft：任何异常回默认值，绝不阻塞搜索）。"""
        if rules is None:
            return dict(_COST_FIELDS_FALLBACK)
        try:
            res = decision_svc.resolve_cost(rules, r, cfg)
            cat = decision_svc.classify_category(categories, r)
            return {"cost_price": _fmt_money(res["cost"]),
                    "cost_source": str(res["source"]),
                    "cost_category_id": int(cat["category_id"]),
                    "cost_category_name": str(cat["category_name"]),
                    "biz_type": str(cat["biz_type"])}
        except Exception:
            return dict(_COST_FIELDS_FALLBACK)

    def _coupon_display(r: CouponRecord) -> tuple[str, str]:
        """(coupon_kind, amount_display)：面额展示服务端派生——折扣率券「7折」原样、
        固定券「20元」、不可解析（免次卡等）回退 benefit_text 原文；前端不再自行拼「元」。"""
        kind = bridge.ChageeTradeApi.coupon_kind({"benefitText": r.benefit_text})
        if kind == "rate":
            return kind, r.benefit_text
        if kind == "fixed":
            return kind, (f"{r.amount}元" if r.amount else r.benefit_text)
        return kind, r.benefit_text

    # 命中集合的维度统计（与分页及 cost_category 过滤解耦，基于同一筛选全集；
    # by_category 含 0=未分类，键为字符串化的子类 id）
    stat = {"effective": 0, "historical": 0, "settle_available": 0, "used": 0,
            "by_category": {}}
    cost_by_code: dict[str, dict] = {}
    for r in rows:
        if r.bucket in stat:
            stat[r.bucket] += 1
        if r.last_order_no:
            stat["used"] += 1
        fields = _cost_fields(r)
        cost_by_code[r.coupon_code] = fields
        key = str(fields["cost_category_id"])
        stat["by_category"][key] = stat["by_category"].get(key, 0) + 1

    # cost_category 内存过滤（正则语义无法下推 SQL）→ 过滤后计 total 再分页
    # -1=未分类（cost_category_id==0，前端档案库「未分类」筛选项约定值）
    if cost_category:
        want = 0 if cost_category == -1 else cost_category
        rows = [r for r in rows if cost_by_code[r.coupon_code]["cost_category_id"] == want]
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    items = []
    for r in page_rows:
        kind, display = _coupon_display(r)
        items.append({
            "id": r.id, "coupon_code": r.coupon_code,
            "template_name": r.template_name,
            "benefit_text": r.benefit_text, "benefit2_text": r.benefit2_text,
            "amount": r.amount,
            "coupon_kind": kind, "amount_display": display,
            "usable_scenes": r.usable_scenes,
            "threshold_tips": r.threshold_tips,
            "use_start_time": r.use_start_time, "use_end_time": r.use_end_time,
            "can_discount": r.can_discount, "bucket": r.bucket,
            "synced_from": r.synced_from,
            "account_id": r.account_id,
            "account_label": (r.account.label + f"#{r.account.id}") if r.account else "",
            "token_fingerprint": r.token_fingerprint,
            "last_used_at": r.last_used_at, "last_order_no": r.last_order_no,
            "updated_at": r.updated_at,
            **cost_by_code[r.coupon_code],
        })
    return {
        "total": total,
        "stats": stat,
        "items": items,
    }


# ---------------- F6 全量取餐码：遍历所有账号 token 批量拉单 + 本地多维模糊搜索（2026-09-28） ----------------

PICKUP_SCAN_PAGE_SIZE = 50          # 每页拉单数（官方 getOrderList pageSize）
PICKUP_SCAN_TODAY_MAX_PAGES = 10    # 今日 tab 翻页上限（覆盖全部在制/待取单）
PICKUP_SCAN_HISTORY_MAX_PAGES = 3   # 历史 tab 翻页上限（回填近单，不深翻全历史）

_pickup_scan_lock = threading.Lock()
# 内存态：running/started_at/progress 供 scan-status 轮询；last 为上次扫描摘要（进程重启即失，前端引导重扫）
_pickup_scan: dict = {"running": False, "started_at": "", "progress": {"done": 0, "total": 0}, "last": None}


def _mask_phone(phone: str) -> str:
    # 内部系统不再脱敏（2026-09-28）：返回完整手机号，函数名仅为兼容历史调用方
    return str(phone or "")


def _run_pickup_scan(username: str) -> None:
    """扫描线程主体：逐账号分页拉单落库，进度经 events_bus 推流 pickup_scan topic；
    单账号凭证失效/协议异常不中断整体遍历（模式同 coupons_sync_all）。"""
    account_rows: list[dict] = []
    ok = expired = failed = 0
    orders_seen = 0
    pickup_codes: set[str] = set()
    try:
        with SessionLocal() as db:
            accounts = (db.query(ChageeAccount)
                        .filter(ChageeAccount.status != "disabled", ChageeAccount.token != "")
                        .order_by(ChageeAccount.id).all())
            _pickup_scan["progress"] = {"done": 0, "total": len(accounts)}
            for idx, account in enumerate(accounts, 1):
                row = {"id": account.id, "label": account.label, "nickname": account.nickname,
                       "phone_masked": _mask_phone(account.phone),
                       "result": "ok", "orders": 0, "pickups": 0, "error": ""}
                try:
                    api = bridge.trade_api(account)
                    seen_codes: set[str] = set()
                    for tab, max_pages in (("today", PICKUP_SCAN_TODAY_MAX_PAGES),
                                           ("history", PICKUP_SCAN_HISTORY_MAX_PAGES)):
                        for page in range(1, max_pages + 1):
                            rows = api.order_list(tab, page, PICKUP_SCAN_PAGE_SIZE)
                            for r in rows:
                                _upsert_order(db, account.id, r.get("orderNo"), **_order_row_fields(r))
                                code = str(r.get("pickupNo") or "")
                                if code:
                                    seen_codes.add(code)
                                    pickup_codes.add(code)
                            row["orders"] += len(rows)
                            if len(rows) < PICKUP_SCAN_PAGE_SIZE:
                                break   # 尾页，本 tab 结束
                    ok += 1
                    orders_seen += row["orders"]
                    row["pickups"] = len(seen_codes)
                except bridge.SessionExpiredError as e:
                    expired += 1
                    account.status = "expired"
                    db.commit()
                    row.update(result="expired", error=f"凭证已失效: {str(e)[:120]}")
                except Exception as e:   # 协议/网络层错误：记录后继续下一个账号
                    failed += 1
                    row.update(result="error", error=f"{type(e).__name__}: {str(e)[:120]}")
                account_rows.append(row)
                _pickup_scan["progress"] = {"done": idx, "total": len(accounts)}
                events_bus.publish("pickup_scan", "account_done", {"index": idx, "total": len(accounts), **row})
        summary = {
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scanned": len(account_rows), "ok": ok, "expired": expired, "failed": failed,
            "orders": orders_seen, "distinct_pickup_codes": len(pickup_codes),
        }
    except Exception as e:   # 线程顶层兜底：任何漏网异常也要解除 running 态并推 scan_done
        logger.error("全量取餐码扫描线程异常终止", exc_info=True)
        summary = {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"), "aborted": True,
                   "scanned": len(account_rows), "ok": ok, "expired": expired, "failed": failed,
                   "orders": orders_seen, "distinct_pickup_codes": len(pickup_codes),
                   "error": f"{type(e).__name__}: {str(e)[:200]}"}
    _pickup_scan["last"] = {"summary": summary, "accounts": account_rows}
    _pickup_scan["running"] = False
    events_bus.publish("pickup_scan", "scan_done", summary)
    log_op(level="INFO", actor=username, target="全账号", action="feature.pickup_scan_all",
           result="failed" if summary.get("aborted") else "success", params=summary)


@global_router.post("/pickup/scan-all")
def pickup_scan_all(request: Request, db: Session = Depends(get_db),
                    user: SystemUser = Depends(require_perm("feature:pickup"))):
    """全量取餐码扫描：遍历系统内所有可登录账号的 token，逐账号批量翻页拉取订单，
    收集全部取餐码并落库 OrderRecord；后台线程执行，进度经 SSE
    （GET /api/events?topics=pickup_scan）实时推流；模糊搜索（/api/ops/pickup/search）
    查本地库，不随查询反复打官方接口。"""
    with _pickup_scan_lock:
        if _pickup_scan["running"]:
            raise HTTPException(409, "全量扫描进行中，请等待完成（GET /api/ops/pickup/scan-status 查进度）")
        accounts = (db.query(ChageeAccount)
                    .filter(ChageeAccount.status != "disabled", ChageeAccount.token != "")
                    .order_by(ChageeAccount.id).count())
        if not accounts:
            raise HTTPException(400, "系统内没有可查询的账号（需要有 token 且未停用的账号）")
        _pickup_scan.update(running=True,
                            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                            progress={"done": 0, "total": accounts})
    threading.Thread(target=_run_pickup_scan, args=(user.username,),
                     daemon=True, name="pickup-scan").start()
    log_audit(db, request, user, "feature.pickup_scan_all", "全账号", {"accounts": accounts})
    return {"started": True, "accounts": accounts, "started_at": _pickup_scan["started_at"],
            "events": "GET /api/events?topics=pickup_scan（SSE 实时进度）"}


@global_router.get("/pickup/scan-status")
def pickup_scan_status(_: SystemUser = Depends(require_perm("feature:pickup"))):
    """扫描状态：进行中返回进度，空闲返回上次扫描摘要（进程重启后 last 为空，前端引导重扫）。"""
    return {"running": _pickup_scan["running"], "started_at": _pickup_scan["started_at"],
            "progress": _pickup_scan["progress"], "last": _pickup_scan["last"]}


@global_router.get("/pickup/search")
def pickup_search(
    keyword: str = Query("", max_length=64),
    status: int = Query(0, ge=0, le=99),
    account_id: int = Query(0, ge=0),
    has_pickup: bool = Query(False),
    scenario: str = Query("", pattern="^(zero|partial)?$"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("feature:pickup")),
):
    """全量取餐码多维模糊搜索（本地 OrderRecord 库，毫秒级、零官方 API 调用）：
    keyword 单串多维度模糊匹配（取餐码值/订单号/饮品快照/门店/履约方式/归属账号
    备注昵称手机号——"名称/使用范围"维度映射），可叠加状态/账号/仅看有码/场景过滤；
    返回分页明细 + 命中维度统计（与分页解耦，供筛选栏计数联动仪表盘）。"""
    base = db.query(OrderRecord).join(ChageeAccount, OrderRecord.account_id == ChageeAccount.id, isouter=True)
    if keyword:
        like = f"%{keyword}%"
        base = base.filter(OrderRecord.pickup_no.like(like)
                           | OrderRecord.order_no.like(like)
                           | OrderRecord.goods_desc.like(like)
                           | OrderRecord.store_name.like(like)
                           | OrderRecord.store_no.like(like)
                           | OrderRecord.biz_type.like(like)
                           | ChageeAccount.label.like(like)
                           | ChageeAccount.nickname.like(like)
                           | ChageeAccount.phone.like(like))
    if account_id:
        base = base.filter(OrderRecord.account_id == account_id)
    if scenario:
        base = base.filter(OrderRecord.scenario == scenario)
    # 命中集合维度统计（与分页及 status/has_pickup 过滤解耦）
    by_status: dict[int, int] = {}
    with_pickup = 0
    for st, pn in base.with_entities(OrderRecord.status, OrderRecord.pickup_no).all():
        by_status[st] = by_status.get(st, 0) + 1
        if pn:
            with_pickup += 1
    q = base
    if status:
        q = q.filter(OrderRecord.status == status)
    if has_pickup:
        q = q.filter(OrderRecord.pickup_no != "")
    total = q.count()
    rows = (q.order_by(OrderRecord.updated_at.desc(), OrderRecord.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total,
        "stats": {"by_status": {str(k): v for k, v in by_status.items()}, "with_pickup": with_pickup},
        "items": [{
            "order_no": r.order_no, "pickup_no": r.pickup_no,
            "order_status": r.status,
            "status_label": r.status_label or ORDER_STATUS_LABELS.get(r.status, str(r.status)),
            "pay_amount": r.pay_amount, "total_amount": r.total_amount,
            "goods_desc": r.goods_desc, "quantity": r.quantity,
            "store_name": r.store_name, "store_no": r.store_no,
            "biz_type": r.biz_type, "scenario": r.scenario,
            "order_time": r.order_time,
            "created_at": r.created_at, "updated_at": r.updated_at,
            "account": ({"id": r.account.id, "label": r.account.label,
                         "nickname": r.account.nickname,
                         "phone_masked": _mask_phone(r.account.phone),
                         "status": r.account.status} if r.account else None),
        } for r in rows],
    }
