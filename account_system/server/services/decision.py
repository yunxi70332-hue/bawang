"""下单决策系统纯函数层（2026-09-28，契约 docs/decision_api_contract.md §2/§4）。

职责：把「客户支付价 + 券 + 套餐 + 成本规则」折算为利润/利润率并给出 pass/blocked
判定。除 load_config/save_config（读写 data/decision_config.json）外全部为无副作用
纯函数，供 routers/decision.py（波2-C）与离线测试直接调用；不 import 任何仓储模块，
规则/套餐/记录以 ORM 对象或 dict 双形态传入（duck-typing 取字段）。

金额规约：全 Decimal 计算，出入一律 str（两位小数，ROUND_HALF_UP）；
margin 为利润率百分比，一位小数字符串（不带 %）。

判定链（契约 §4）：
  parse_benefit      benefitText 面额解析（"20元"→20，"满30减5"→5）
  classify_coupon    券文案 → face/discount/exchange/unknown + face/rate
  resolve_cost       券 → 采购成本（VoucherCostRule 匹配链，未命中按面额×fallback 系数）
  classify_category  券 → 成本子类归类（业务分类层：采购付费/活动免费/银行渠道；
                     子类只做分类不做成本——成本金额仍由 resolve_cost 唯一决定）
  estimate_deduction 券种 → 预计抵扣（返回值带"是否估算"标记）
  match_packets      套餐命中（价格区间 + 时段 + 商品圈定，支持跨零点时段）
  evaluate_cost      收入/总额/抵扣/成本/杂费 → cost_breakdown（profit/margin）
  check_threshold    cost_breakdown + 阈值 → (pass|blocked, 原因)
  rank_candidates    候选券逐个评估，pass 者按 total_cost 升序（同成本面额大者优先）

全局配置 data/decision_config.json（相对 account_system/ 定位，与 pay_session.py 的
autopay_config.json 同级）：min_profit / min_margin / overhead / cost_fallback_ratio。
文件不存在时按默认值返回（不落盘），save_config 时才写。
"""

import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

logger = logging.getLogger(__name__)

# data/ 定位：services/x.py 上三层 = account_system/（与 database.DATA_DIR、pay_session 同一定位方式）
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
_CONFIG_PATH = os.path.join(_DATA_DIR, "decision_config.json")

# 全局配置默认值（契约 §2/§10）
DEFAULT_CONFIG = {
    "min_profit": "2.00",          # 全局每单最低利润（元）
    "min_margin": "",              # 全局最低利润率%（空=不启用）
    "max_order_cost": "",          # 全局最大承受下单金额（成本上限，元；空=不限，套餐级可覆盖）
    "overhead": "0",               # 每单杂费（元）
    "cost_fallback_ratio": "1.0",  # 成本规则未命中时按面额×该系数保守计
}

# benefitText 面额解析："满30减5"（取减免额 N）优先于 "N元"（取面额 N）
_RE_FULL_REDUCTION = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*元?\s*减\s*(\d+(?:\.\d+)?)\s*元?")
_RE_FACE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
# 折扣："7折" → rate=0.7（X 可为小数，"8.8折" → 0.88）
_RE_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*折")


# ---------------- 基础工具：Decimal 安全转换 / 格式化 ----------------

def _dec(v) -> Decimal:
    """金额安全转 Decimal：None/空/非法 → 0（fail-soft，缺省值语义）。"""
    try:
        if v is None or v == "":
            return Decimal("0")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _dec_or_none(v) -> Decimal | None:
    """同 _dec，但 None/空/非法 → None（面额等"不可知"语义）。"""
    try:
        if v is None or v == "":
            return None
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(v) -> str:
    """金额统一两位小数字符串（ROUND_HALF_UP）。"""
    return f"{_dec(v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _field(obj, key, default=""):
    """ORM 对象 / dict 双态取字段（规则、套餐、券记录可能以任一形态传入，便于离线测试）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ---------------- 券文案解析 ----------------

def parse_benefit(benefit_text) -> Decimal | None:
    """benefitText 面额解析（契约 §4）："20元"→20；"满30减5"/"满30元减5元"→5（取减免额）；
    无面额可解析（"7折"/"兑换券"/空）→ None。"""
    text = str(benefit_text or "").strip()
    if not text:
        return None
    m = _RE_FULL_REDUCTION.search(text)
    if m:
        return Decimal(m.group(2))
    m = _RE_FACE.search(text)
    if m:
        return Decimal(m.group(1))
    return None


def classify_coupon(benefit_text, benefit2_text, template_name, biz_type="") -> dict:
    """券文案 → 券种判定：{"kind": face|discount|exchange|unknown, "face": Decimal|None, "rate": Decimal|None}。

    优先级（契约 §4）：兑换/换购（benefit2Text/templateName 含关键字）＞ 折扣
    （benefitText/templateName 含 "X折"，rate=X/10）＞ 面额（benefitText "N元"/"满M减N"）；
    均判定不出 → unknown（face 仍从 templateName 保守解析供估算，解析不出为 None）。
    """
    benefit_text = str(benefit_text or "")
    benefit2_text = str(benefit2_text or "")
    template_name = str(template_name or "")

    # 兑换/换购券优先判定（溢价券商品场景）
    for text in (benefit2_text, template_name):
        if "兑换" in text or "换购" in text:
            return {"kind": "exchange", "face": None, "rate": None}

    # 折扣券："X折" → rate = X/10
    for text in (benefit_text, template_name):
        m = _RE_DISCOUNT.search(text)
        if m:
            return {"kind": "discount", "face": None, "rate": Decimal(m.group(1)) / Decimal("10")}

    # 面额券："N元" / "满M减N"
    face = parse_benefit(benefit_text)
    if face is not None:
        return {"kind": "face", "face": face, "rate": None}

    # 判定不出：unknown（face 从 templateName 保守解析，供 estimate_deduction 估算）
    return {"kind": "unknown", "face": parse_benefit(template_name), "rate": None}


# ---------------- 成本与抵扣 ----------------

def _match_hit(match_type, match_value, template_name, benefit_text, coupon_code) -> bool:
    """四类匹配判定（resolve_cost 与 classify_category 共用，语义完全一致）：
    template_exact（=template_name）/ template_contains（∈template_name）/
    benefit_regex（re.search 于 template_name+" "+benefit_text 拼接串）/
    coupon_prefix（coupon_code startswith）。
    match_value 空 / 未知 match_type / 任何异常（regex 非法等）→ False（视为不命中）。"""
    try:
        match_type = str(match_type or "")
        match_value = str(match_value or "")
        if not match_value:
            return False
        if match_type == "template_exact":
            return template_name == match_value
        if match_type == "template_contains":
            return match_value in template_name
        if match_type == "benefit_regex":
            return bool(re.search(match_value, f"{template_name} {benefit_text}"))
        if match_type == "coupon_prefix":
            return coupon_code.startswith(match_value)
        return False
    except Exception:
        return False


def resolve_cost(rules, record_fields, config=None) -> dict:
    """券 → 采购成本：{"cost": Decimal, "source": "rule:<id>"|"fallback", "rule": 规则对象|None}。

    匹配链（契约 §4）：enabled 规则按 priority 升序逐条尝试——
    template_exact（=template_name）/ template_contains（∈template_name）/
    benefit_regex（re.search 于 template_name+" "+benefit_text 拼接串）/
    coupon_prefix（coupon_code startswith），四类命中判定见 _match_hit；
    face_value 非空时须等于券面额（record_fields["amount"]）才命中；
    全部未命中 → cost = 面额 × cost_fallback_ratio，source="fallback"。
    任何异常（regex 非法等）安全吞掉视为该条不命中。

    record_fields: {template_name, benefit_text, coupon_code, amount(面额), ...}，
    券记录 ORM 对象或 dict 均可。
    """
    cfg = config or {}
    record_fields = record_fields or {}
    template_name = str(_field(record_fields, "template_name") or "")
    benefit_text = str(_field(record_fields, "benefit_text") or "")
    coupon_code = str(_field(record_fields, "coupon_code") or "")
    face = _dec_or_none(_field(record_fields, "amount"))
    fallback_ratio = _dec_or_none(cfg.get("cost_fallback_ratio")) if isinstance(cfg, dict) else None
    if fallback_ratio is None or fallback_ratio < 0:
        fallback_ratio = Decimal("1")

    ordered = sorted(
        (r for r in (rules or []) if bool(_field(r, "enabled", True))),
        key=lambda r: _dec(_field(r, "priority", 100)),
    )
    for rule in ordered:
        try:
            if not _match_hit(_field(rule, "match_type"), _field(rule, "match_value"),
                              template_name, benefit_text, coupon_code):
                continue   # 未命中 → 继续走链
            face_check = str(_field(rule, "face_value") or "").strip()
            if face_check and (face is None or _dec(face_check) != face):
                continue   # 面额校验不通过 → 视为不命中，继续走链
            return {"cost": _dec(_field(rule, "cost_price")),
                    "source": f"rule:{_field(rule, 'id', 0)}", "rule": rule}
        except Exception:
            continue   # regex 非法等安全吞掉，视为不命中
    return {"cost": (face or Decimal("0")) * fallback_ratio, "source": "fallback", "rule": None}


def classify_category(categories, record_fields) -> dict:
    """券 → 成本子类归类：{"category_id": int, "category_name": str, "biz_type": str}。

    子类只做分类，不做成本——成本金额仍由 resolve_cost（voucher_cost_rules）唯一决定。
    enabled 子类按 priority 升序逐条尝试，四类匹配语义与 resolve_cost 完全一致
    （共用 _match_hit：template_exact / template_contains / benefit_regex（于
    template_name+" "+benefit_text 拼接串）/ coupon_prefix）；
    全不命中 → {"category_id": 0, "category_name": "", "biz_type": ""}（0=未分类）。
    异常（regex 非法等）安全吞掉视为不命中；categories 子类与 record_fields
    券记录均可为 ORM 对象或 dict（_field 双态取字段）。
    """
    record_fields = record_fields or {}
    template_name = str(_field(record_fields, "template_name") or "")
    benefit_text = str(_field(record_fields, "benefit_text") or "")
    coupon_code = str(_field(record_fields, "coupon_code") or "")
    ordered = sorted(
        (c for c in (categories or []) if bool(_field(c, "enabled", True))),
        key=lambda c: _dec(_field(c, "priority", 100)),
    )
    for cat in ordered:
        if not _match_hit(_field(cat, "match_type"), _field(cat, "match_value"),
                          template_name, benefit_text, coupon_code):
            continue   # 未命中 → 继续走链
        return {"category_id": int(_field(cat, "id", 0) or 0),
                "category_name": str(_field(cat, "name") or ""),
                "biz_type": str(_field(cat, "biz_type") or "")}
    return {"category_id": 0, "category_name": "", "biz_type": ""}


def estimate_deduction(kind, face, rate, total):
    """券种 → (预计抵扣 Decimal, 是否估算 bool)（契约 §4）：
    face → min(face,total)，精确值；discount → total×(1-rate)，估算；
    exchange → 全额抵扣 total，估算；unknown 有 face → min(face,total)，估算；
    其余（无面额可算）→ (0, True)。"""
    total = _dec(total)
    kind = str(kind or "")
    if kind == "face" and face is not None:
        return min(_dec(face), total), False
    if kind == "discount" and rate is not None:
        deduction = total * (Decimal("1") - _dec(rate))
        if deduction < 0:
            deduction = Decimal("0")
        return deduction.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), True
    if kind == "exchange":
        return total, True
    if kind == "unknown" and face is not None:
        return min(_dec(face), total), True
    return Decimal("0"), True


# ---------------- 套餐命中 ----------------

def match_packets(packets, sku_id, price, now=None) -> list:
    """套餐命中筛选（契约 §4）：open_flag 且 price∈[min_order_amount, max_order_amount]
    （max=0 不设上限）且可用时段命中且（items 为空=全品类 或 sku_id 在 items 中）。
    返回命中的套餐列表（保持入参顺序）。

    时段：available_start/end 为 "HH:MM:SS"，各自空=不限；start>end 视为跨零点区间
    （如 22:00:00~06:00:00 命中 t>=22:00:00 或 t<=06:00:00）。
    单个套餐读取异常（含 ORM 脱离会话的懒加载失败）跳过，不影响其余。
    """
    if now is None:
        now = datetime.now()
    price = _dec(price)
    sku_id = str(sku_id or "")
    t = now.strftime("%H:%M:%S")
    matched = []
    for packet in (packets or []):
        try:
            if not bool(_field(packet, "open_flag", True)):
                continue
            lo = _dec(_field(packet, "min_order_amount", "0"))
            hi = _dec(_field(packet, "max_order_amount", "0"))
            if price < lo or (hi > 0 and price > hi):
                continue
            start = str(_field(packet, "available_start", "") or "")
            end = str(_field(packet, "available_end", "") or "")
            if start and end and start > end:
                time_ok = t >= start or t <= end      # 跨零点区间
            else:
                time_ok = (not start or t >= start) and (not end or t <= end)
            if not time_ok:
                continue
            items = _field(packet, "items", None) or []
            sku_ids = {str(_field(item, "sku_id", "") or "") for item in items}
            if sku_ids and sku_id not in sku_ids:
                continue
            matched.append(packet)
        except Exception:
            continue
    return matched


# ---------------- 利润折算与阈值判定 ----------------

def evaluate_cost(revenue, total, deduction, voucher_cost, overhead) -> dict:
    """利润拆解（契约 §4，§5 cost_breakdown 的金额域）：
    pay_cost = max(total-deduction, 0)（差额实付）；total_cost = voucher_cost +
    pay_cost + overhead；profit = revenue - total_cost；margin = profit/revenue×100
    （一位小数，revenue<=0 时为 ""）。
    全部金额两位小数字符串；price_source / deduction_estimated / cost_source /
    coupon_kind 四个来源字段由调用方（routers/decision.py / rank_candidates）补充。"""
    revenue_d = _dec(revenue)
    total_d = _dec(total)
    deduction_d = _dec(deduction)
    pay_cost = total_d - deduction_d
    if pay_cost < 0:
        pay_cost = Decimal("0")
    total_cost = _dec(voucher_cost) + pay_cost + _dec(overhead)
    profit = revenue_d - total_cost
    margin = (profit / revenue_d * Decimal("100")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP) if revenue_d > 0 else ""
    return {
        "revenue": _money(revenue_d),
        "total_trade_price": _money(total_d),
        "deduction": _money(deduction_d),
        "voucher_cost": _money(voucher_cost),
        "pay_cost": _money(pay_cost),
        "overhead": _money(overhead),
        "total_cost": _money(total_cost),
        "profit": _money(profit),
        "margin": str(margin),
    }


def check_threshold(breakdown, min_profit, min_margin, max_order_cost=""):
    """阈值判定（契约 §4/§10）：profit < min_profit、margin < min_margin、
    total_cost > max_order_cost 任一触发 → ("blocked", 原因)，否则 ("pass", "")。
    各阈值空=不校验该项；margin 为空（revenue<=0 无法计算）时跳过利润率校验。
    max_order_cost=最大承受下单金额（成本上限，与 min_profit 利润下限互补）。"""
    breakdown = breakdown or {}
    profit = _dec(breakdown.get("profit"))
    min_profit_s = str(min_profit or "").strip()
    if min_profit_s and profit < _dec(min_profit_s):
        return "blocked", f"利润 {_money(profit)} 元低于最低利润 {_money(min_profit_s)} 元"
    margin_s = str(breakdown.get("margin") or "").strip()
    min_margin_s = str(min_margin or "").strip()
    if min_margin_s and margin_s and _dec(margin_s) < _dec(min_margin_s):
        return "blocked", f"利润率 {margin_s}% 低于最低利润率 {_dec(min_margin_s)}%"
    max_cost_s = str(max_order_cost or "").strip()
    if max_cost_s and _dec(breakdown.get("total_cost")) > _dec(max_cost_s):
        return "blocked", (f"订单成本 {_money(breakdown.get('total_cost'))} 元"
                           f"超过最大承受金额 {_money(max_cost_s)} 元")
    return "pass", ""


def rank_candidates(candidates, revenue, total, overhead, min_profit, min_margin,
                    max_order_cost="") -> list[dict]:
    """候选券评估排序（契约 §4/§10 四级漏斗的第 2/3 级）：candidates 每项
    {record, cost, source, kind, face, rate, use_end_time?}，逐个算 cost_breakdown
    （补充 deduction_estimated/cost_source/coupon_kind）+ 三阈值判定（min_profit /
    min_margin / max_order_cost），仅保留 pass，按
    total_cost 升序 → 同成本面额大者优先 → 同成本同面额有效期近者优先（临期因子，
    cand["use_end_time"] 毫秒时间戳升序，None 排最后）。
    每个返回元素：{record, cost, source, kind, face, rate, cost_breakdown, verdict, reason}。"""
    ranked = []
    for cand in (candidates or []):
        cand = cand or {}
        kind = str(cand.get("kind") or "")
        face = cand.get("face")
        rate = cand.get("rate")
        cost = _dec(cand.get("cost"))
        source = str(cand.get("source") or "fallback")
        deduction, estimated = estimate_deduction(kind, face, rate, total)
        breakdown = evaluate_cost(revenue, total, _money(deduction), _money(cost), overhead)
        breakdown["deduction_estimated"] = estimated
        breakdown["cost_source"] = source
        breakdown["coupon_kind"] = kind
        verdict, reason = check_threshold(breakdown, min_profit, min_margin, max_order_cost)
        if verdict != "pass":
            continue
        ranked.append({
            "record": cand.get("record"),
            "cost": cost,
            "source": source,
            "kind": kind,
            "face": face,
            "rate": rate,
            "use_end_time": cand.get("use_end_time"),
            "cost_breakdown": breakdown,
            "verdict": verdict,
            "reason": reason,
        })
    _BIG = 2 ** 62   # 临期排序哨兵：无 use_end_time（如 unknown 券型）排最后
    ranked.sort(key=lambda x: (
        _dec(x["cost_breakdown"]["total_cost"]),                        # ① total_cost 升序
        -(_dec(x["face"]) if x["face"] is not None else Decimal("0")),  # ② 同成本面额大者优先
        int(x.get("use_end_time") or _BIG),                             # ③ 同成本同面额临期优先
    ))
    return ranked


# ---------------- 全局配置：data/decision_config.json ----------------

def load_config() -> dict:
    """读 data/decision_config.json（契约 §2）：文件不存在/缺键/解析失败时以
    DEFAULT_CONFIG 兜底（fail-soft，不落盘——save_config 时才写）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)   # 保留文件中的额外键（GET /config 透出原文）
            for key, value in DEFAULT_CONFIG.items():
                if cfg.get(key) is None:
                    cfg[key] = value
    except FileNotFoundError:
        pass   # 文件不存在 → 默认值
    except Exception:
        logger.warning("decision_config.json 解析失败，使用默认配置", exc_info=True)
    return cfg


def save_config(cfg: dict) -> dict:
    """写 data/decision_config.json：以 DEFAULT_CONFIG 补全缺键（已知键统一转 str）后
    临时文件原子落盘（目录不存在则创建），返回落盘后的完整配置。"""
    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        merged.update(cfg)   # 额外键原样保留
    for key in DEFAULT_CONFIG:
        if merged.get(key) is None:
            merged[key] = DEFAULT_CONFIG[key]
        else:
            merged[key] = str(merged.get(key))
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CONFIG_PATH)
    return merged
