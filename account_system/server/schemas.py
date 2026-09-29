"""Pydantic 请求/响应模型。敏感字段（token/sk）永不下发；手机号自 2026-09-28 起不再脱敏，
字段名沿用 phone_masked 历史命名，但值为完整号码。"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

PHONE_RE = r"^1[3-9]\d{9}$"


def mask_phone(phone: str) -> str:
    # 内部系统不再脱敏（2026-09-28）：返回完整手机号，函数名仅为兼容历史调用方
    return (phone or "")


# ---------- 认证 ----------

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=64)


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=64)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("新密码须同时包含字母与数字")
        return v


class RoleBrief(BaseModel):
    id: int
    name: str
    description: str
    is_builtin: bool

    class Config:
        from_attributes = True


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    role: RoleBrief

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    token: str
    user: UserOut
    permissions: list[str]


# ---------- 系统用户 ----------

class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    display_name: str = Field(default="", max_length=32)
    password: str = Field(min_length=8, max_length=64)
    role_id: int


class UserUpdate(BaseModel):
    display_name: str = Field(default="", max_length=32)
    role_id: int
    is_active: bool = True


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=64)


# ---------- 角色 ----------

class RoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    description: str = Field(default="", max_length=128)
    permissions: list[str]


class RoleUpdate(BaseModel):
    description: str = Field(default="", max_length=128)
    permissions: list[str]


# ---------- 茶姬账号 ----------

class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    phone: str = Field(pattern=PHONE_RE)
    group: str = Field(default="默认", max_length=32)
    note: str = Field(default="", max_length=500)


class AccountUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    phone: str = Field(pattern=PHONE_RE)
    group: str = Field(default="默认", max_length=32)
    note: str = Field(default="", max_length=500)
    status: str | None = None  # 仅允许 pending/disabled 人工切换

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in ("pending", "disabled"):
            raise ValueError("人工状态仅可设为 待登录/已停用")
        return v


class AccountOut(BaseModel):
    id: int
    label: str
    phone_masked: str
    phone_full: str | None = None   # 仅有 account:update 权限时下发
    device_uuid: str
    customer_id: str
    nickname: str
    status: str
    status_label: str
    group: str
    note: str
    token_fingerprint: str          # 形如 eyJhbGci...len=608，不泄露完整凭证
    has_token: bool
    last_login_at: datetime | None
    last_check_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class SmsCodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


class CouponSummaryOut(BaseModel):
    run_at: str
    account_id: int
    summary: dict
    coupons: list[dict]
    exchange_vouchers: list[dict]


# ---------- 订单（F5 下单 / F6 取餐查询，契约 docs/api_contract_f5f6.md） ----------

class OrderSettleRequest(BaseModel):
    """下单试算（购物车加购 + 无券试算，生成服务端 draft）。

    spec_list/item_sku_id/image_url/spu_type 可选：透传引擎 cart_add 的 target，
    缺省与引擎默认一致（specList=[] / spuType="stand" / imageUrl="" / quantity=1）。
    """
    store_no: str = Field(min_length=1, max_length=32)
    store_name: str = Field(default="", max_length=128)
    spu_id: str = Field(min_length=1, max_length=64)
    spu_name: str = Field(default="", max_length=128)
    sku_id: str = Field(min_length=1, max_length=64)
    sku_name: str = Field(default="", max_length=128)
    item_sku_id: str = Field(default="", max_length=64)
    quantity: int = Field(default=1, ge=1, le=99)
    spec_list: list[dict] = Field(default_factory=list)   # [{"specId","specOptionId","specName","specOptionName"},...]
    attribute_list: list[dict] = Field(default_factory=list)  # [{"attributeId","attributeName","attributeOptionId","attributeOptionName"},...]
    extra_list: list[dict] = Field(default_factory=list)  # 必选加料组的选项（goods.extraInfos[].extraOptions[] 原始条目）
    sale_price: float = Field(default=0, ge=0)            # SKU 原价（calculatePrice 入参，数值型）
    nutrition_info: dict | None = None                    # 按属性组合匹配的营养信息（settle 行随行）
    image_url: str = Field(default="", max_length=512)
    spu_type: str = Field(default="stand", max_length=16)


class OrderCreateRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=64)
    coupon_code: str | None = Field(default=None, max_length=64)   # None = 不用券
    # 决策挂钩（契约 decision_api_contract.md §6）：decide 响应的 decision_log_id 传回时，
    # 成单后回填该 DecisionLog 的 order_no/account_id/coupon_code/deduction_actual/pay_actual；
    # 缺省 0 = 行为与决策系统引入前完全一致（向后兼容）
    decision_log_id: int = Field(default=0, ge=0)
    # 券自动切换（契约 §10）：true 时券验证失败/试算不在列/settle 复跑券相关异常 →
    # 从本次试算可用券列表按四级漏斗取次优重试（同账号，最多试 3 张，含首选）；
    # 缺省 false = 行为与决策系统引入前完全一致（向后兼容）
    auto_fallback: bool = Field(default=False)
    # 下单方案（§11）：fallback 候选按方案策略与优先级层排序；0=自动。带 decision_log_id
    # 时以流水中记录的方案为准（decision_log_id 优先），两者都无则全局自动
    plan_id: int = Field(default=0, ge=0)


class PayModeRequest(BaseModel):
    mode: str = Field(pattern="^(manual|auto)$")


class CashierUrlRequest(BaseModel):
    """云手机实时捕获的支付宝官方收银台 URL 回填（mobilegw 会话仅捕获后短窗有效）。"""
    url: str = Field(min_length=32, max_length=2048)


# ---------- 下单决策（契约 docs/decision_api_contract.md §5） ----------

class PacketItemBody(BaseModel):
    """套餐商品行（POST/PUT /packets 的 items 元素）：spu/sku 圈定可接商品，
    券规则 JSON（{"match_type":"template_contains","match_value":"代金券"}）控制选券，null=不限。"""
    spu_id: str = Field(default="", max_length=32)
    sku_id: str = Field(min_length=1, max_length=32)
    product_name: str = Field(default="", max_length=128)
    face_price: str = Field(default="", max_length=16)        # 面价（前端选品时从菜单回填）
    premium_price: str = Field(default="", max_length=16)     # 溢价（可空）
    is_premium: bool = False                                  # 是否需溢价券商品
    normal_coupon_rule: dict | None = None                    # 常规券规则 | null=不限
    premium_coupon_rule: dict | None = None                   # 溢价券规则（同构）


class PacketCreateRequest(BaseModel):
    """套餐创建/编辑（PUT 同构，items 全量替换）。金额为 String 金额字符串。"""
    name: str = Field(min_length=1, max_length=64)
    min_order_amount: str = Field(default="0", max_length=16)   # 客户支付价下限
    max_order_amount: str = Field(default="0", max_length=16)   # 0=不设上限
    available_start: str = Field(default="", max_length=8)      # "HH:MM:SS"，空=不限
    available_end: str = Field(default="", max_length=8)
    min_profit: str = Field(default="", max_length=16)          # 套餐级最低利润覆盖，空=用全局
    max_order_cost: str = Field(default="", max_length=16)      # 套餐级最大承受下单金额覆盖，空=用全局
    note: str = Field(default="", max_length=255)
    items: list[PacketItemBody] = Field(default_factory=list)   # 空=全品类


class CostRuleRequest(BaseModel):
    """券采购成本规则：match_type 四种命中方式见 services/decision.resolve_cost。
    category_id 软关联券成本子类（业务分类层，0=未分类）——子类只做分类，成本金额仍由本规则唯一决定。"""
    name: str = Field(min_length=1, max_length=64)
    match_type: str = Field(pattern="^(template_exact|template_contains|benefit_regex|coupon_prefix)$")
    match_value: str = Field(min_length=1, max_length=128)     # 匹配值（regex 时为正则）
    face_value: str = Field(default="", max_length=16)         # 面额校验（非空时须等于券面额）
    cost_price: str = Field(min_length=1, max_length=16)       # 采购成本（元）
    priority: int = Field(default=100)                         # 越小越优先
    enabled: bool = True
    note: str = Field(default="", max_length=255)
    category_id: int = Field(default=0)                        # 关联券成本子类（0=未分类）


class CostCategoryRequest(BaseModel):
    """券成本子类（业务分类层：采购付费/活动免费/银行渠道）：自动归类匹配券，
    match_type 四种命中方式与成本规则一致（services/decision.classify_category）；
    子类只做分类，不做成本。"""
    name: str = Field(min_length=1, max_length=64)
    biz_type: str = Field(pattern="^(paid|free|bank|other)$")  # paid采购付费|free活动免费|bank银行渠道|other
    match_type: str = Field(pattern="^(template_exact|template_contains|benefit_regex|coupon_prefix)$")
    match_value: str = Field(min_length=1, max_length=128)     # 匹配值（regex 时为正则）
    priority: int = Field(default=100)                         # 越小越优先
    enabled: bool = True
    note: str = Field(default="", max_length=255)
    sort: int = Field(default=0)                               # 展示排序（越小越靠前）


class CostRuleImportRequest(BaseModel):
    """批量导入：逐条校验（非法条目进 errors 不中断），name 与库内重复跳过。"""
    rules: list[dict] = Field(default_factory=list)            # 元素字段同 CostRuleRequest


class DecisionConfigRequest(BaseModel):
    """全局决策配置（data/decision_config.json，services/decision.load_config/save_config）。"""
    model_config = {"extra": "allow"}   # 透传文件中的额外键（GET/PUT 原文往返）

    min_profit: str = Field(default="2.00", max_length=16)     # 全局每单最低利润（元）
    min_margin: str = Field(default="", max_length=16)         # 全局最低利润率%（空=不启用）
    max_order_cost: str = Field(default="", max_length=16)     # 全局最大承受下单金额（成本上限，空=不限，套餐级可覆盖）
    overhead: str = Field(default="0", max_length=16)          # 每单杂费（元）
    cost_fallback_ratio: str = Field(default="1.0", max_length=16)  # 规则未命中按面额×该系数


class OrderPlanPriorityBody(BaseModel):
    """券优先级层级（方案 priorities 元素）：level=1 即第一优先，匹配语义与成本规则一致。"""
    level: int = Field(default=1, ge=1, le=99)                 # 层级（1=第一优先）
    name: str = Field(default="", max_length=64)                # 层名（如「20元DN券」）
    match_type: str = Field(pattern="^(template_exact|template_contains|benefit_regex|coupon_prefix)$")
    match_value: str = Field(min_length=1, max_length=128)      # 匹配值（regex 时为正则）
    face_value: str = Field(default="", max_length=16)          # 面额校验（空=不限）


class OrderPlanRequest(BaseModel):
    """下单方案（§11）：策略 + 券优先级层级链。PUT 同构（priorities 全量替换）。"""
    name: str = Field(min_length=1, max_length=64)
    strategy: str = Field(pattern="^(cost_first|zero_pay|expiry_first)$")   # 成本最优|零元优先|临期优先
    note: str = Field(default="", max_length=255)
    enabled: bool = True
    priorities: list[OrderPlanPriorityBody] = Field(default_factory=list)   # 空=不设层，纯策略排序


class ScanRequest(BaseModel):
    """券库存扫描：account_ids 空=全部可登录账号（status!=disabled 且 token 非空）。"""
    account_ids: list[int] = Field(default_factory=list)


class DecideRequest(BaseModel):
    """决策评估（POST /api/ops/orders/decide）：客户平台 linkId + 文案规格 → 利润折算与选券推荐。"""
    sku_id: str = Field(min_length=1, max_length=64)           # 客户平台 linkId
    quantity: int = Field(default=1, ge=1, le=99)
    spec_list: list[str] = Field(default_factory=list)         # 文案规格（"大杯"…），空=用 sku 默认组合
    store_no: str = Field(min_length=1, max_length=32)
    customer_price: str = Field(min_length=1, max_length=16)   # 客户支付价（revenue，String 金额）
    packet_id: int = Field(default=0, ge=0)                    # 可选，指定则校验在命中集内
    allow_full_price: bool = False                             # 无券时是否允许原价单
    deep: bool = False                                         # true=对 top1 候选账号真实 settle 探针
    plan_id: int = Field(default=0, ge=0)                      # 下单方案（§11）：0=自动（系统推荐）
