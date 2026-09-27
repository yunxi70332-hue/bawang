"""ORM 模型：系统用户 / 角色 / 茶姬账号 / 登录工单 / 订单快照 / 审计日志 / 支付会话。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(128), default="")
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    users: Mapped[list["SystemUser"]] = relationship(back_populates="role")


class SystemUser(Base):
    __tablename__ = "system_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(32), default="")
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    role: Mapped[Role] = relationship(back_populates="users")


# 茶姬账号状态机：pending 待登录 → online 在线；online → expired 凭证失效（单会话语义，无 refresh）；
# disabled 为人工停用（协议操作全部拒绝）
ACCOUNT_STATUS = ("pending", "online", "expired", "disabled")
STATUS_LABELS = {"pending": "待登录", "online": "在线", "expired": "凭证失效", "disabled": "已停用"}


class ChageeAccount(Base):
    __tablename__ = "chagee_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), default="")          # 备注名
    phone: Mapped[str] = mapped_column(String(16), default="")          # 明文手机号（接口层脱敏下发）
    device_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token: Mapped[str] = mapped_column(Text, default="")                # 608B 裸 JWT，永不下发
    sk: Mapped[str] = mapped_column(String(128), default="")            # getsk 握手缓存
    customer_id: Mapped[str] = mapped_column(String(32), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")       # 茶姬侧昵称（whoami 回填）
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    group: Mapped[str] = mapped_column(String(32), default="默认")       # 账号分组
    note: Mapped[str] = mapped_column(Text, default="")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class LoginTicket(Base):
    """协议登录工单：短信发送 → 等待验证码 → 成功/失败，10 分钟过期。"""

    __tablename__ = "login_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("chagee_accounts.id"), index=True)
    phone: Mapped[str] = mapped_column(String(16), default="")
    stage: Mapped[str] = mapped_column(String(16), default="wait_code")  # sms_sent/wait_code/succeeded/failed
    last_error: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    account: Mapped[ChageeAccount] = relationship()


class OrderRecord(Base):
    """订单落库快照（F5 成单 / F6 查询回填）：order_no 唯一，存在则更新（upsert 语义）。"""

    __tablename__ = "order_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("chagee_accounts.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    store_no: Mapped[str] = mapped_column(String(32), default="")
    store_name: Mapped[str] = mapped_column(String(128), default="")
    goods_desc: Mapped[str] = mapped_column(String(255), default="")  # 商品快照（spu_name xN + 规格）
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    coupon_code: Mapped[str] = mapped_column(String(64), default="")
    total_amount: Mapped[str] = mapped_column(String(16), default="")
    pay_amount: Mapped[str] = mapped_column(String(16), default="")
    scenario: Mapped[str] = mapped_column(String(8), default="")     # zero|partial
    status: Mapped[int] = mapped_column(Integer, default=1, index=True)
    status_label: Mapped[str] = mapped_column(String(16), default="")
    pickup_no: Mapped[str] = mapped_column(String(16), default="")
    unique_pos_order_no: Mapped[str] = mapped_column(String(64), default="")
    out_trade_no: Mapped[str] = mapped_column(String(64), default="")
    pay_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 下单时的 settle target 快照（draft["target"] + extra_list + 门店/商品描述），
    # 供"券差额单取消后原价重下"（switch-full-price）复用——重下需逐字段的原始算价入参
    order_target: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    account: Mapped[ChageeAccount] = relationship()


class CouponRecord(Base):
    """券档案：coupon_code 全局唯一 + 归属账号（token 指纹）绑定 —— 券ID与token的唯一映射，
    用于下单前的归属安全验证与识别；完整名称等全量字段从 F4 券查询与 F5 试算两个来源同步。"""

    __tablename__ = "coupon_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coupon_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("chagee_accounts.id"), index=True)
    token_fingerprint: Mapped[str] = mapped_column(String(64), default="")   # 归属账号 token 指纹（不存全量 token）
    template_name: Mapped[str] = mapped_column(String(255), default="")      # 完整名称
    benefit_text: Mapped[str] = mapped_column(String(64), default="")
    benefit2_text: Mapped[str] = mapped_column(String(64), default="")
    biz_type: Mapped[str] = mapped_column(String(16), default="")
    amount: Mapped[str] = mapped_column(String(16), default="")              # 面额（benefitText 解析）
    usable_scenes: Mapped[str] = mapped_column(String(64), default="")       # 使用范围（自取/外卖/团餐自提）
    threshold_tips: Mapped[str] = mapped_column(String(128), default="")
    use_start_time: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 毫秒时间戳
    use_end_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    can_discount: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    bucket: Mapped[str] = mapped_column(String(32), default="")              # effective|historical|settle_available
    synced_from: Mapped[str] = mapped_column(String(32), default="")         # coupon_query|settle
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_order_no: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    account: Mapped[ChageeAccount] = relationship()


class CouponUsageLog(Base):
    """券使用日志：使用时间/订单号/券ID/账号与操作人/金额/结果（含校验失败记录），供查询与统计分析。"""

    __tablename__ = "coupon_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coupon_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    coupon_name: Mapped[str] = mapped_column(String(255), default="")
    account_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    account_label: Mapped[str] = mapped_column(String(64), default="")       # 茶姬账号快照（label#id）
    operator: Mapped[str] = mapped_column(String(32), default="")            # 系统操作用户
    order_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    deduction: Mapped[str] = mapped_column(String(16), default="")           # 抵扣金额
    total_amount: Mapped[str] = mapped_column(String(16), default="")
    pay_amount: Mapped[str] = mapped_column(String(16), default="")
    scenario: Mapped[str] = mapped_column(String(8), default="")             # zero|partial
    result: Mapped[str] = mapped_column(String(16), default="", index=True)  # success|rejected|failed
    fail_reason: Mapped[str] = mapped_column(String(255), default="")
    used_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64))          # 如 account.login_sms / user.create
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")     # JSON 字符串
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


# ---------------- H5 收银台：支付会话域 ----------------
# 设计要点：order_no ↔ 支付链接一对一（续付/重铸原地更新、token 不变），
# token 即凭证（无 JWT），事件流 PayEventLog 供 watcher / H5 页面 / 管理端三方对账。

# PaySession.status 状态机：issued 待支付 → paid 已支付 / cancelled 已取消 / expired 已过期
PAY_SESSION_STATUS = ("issued", "paid", "cancelled", "expired")
PAY_SESSION_STATUS_LABELS = {"issued": "待支付", "paid": "已支付",
                             "cancelled": "已取消", "expired": "已过期"}
# PaySession.mode：partial=券差额单（有券）、full=原价单（无券，含 switch 重下的单）
PAY_SESSION_MODES = ("partial", "full")

# PayEventLog.event 事件枚举（H5 页面 / watcher / 管理端共用的时间线语言）
PAY_EVENTS = (
    "link_issued",          # 首次铸出支付链接
    "remint",               # 续付/重铸支付串（token 不变）
    "page_opened",          # H5 收银台页面/信息接口被访问
    "probe",                # 远程订单状态探针（含失败原因）
    "paid_detected",        # 探针发现已支付（3/6）
    "pickup_fetched",       # 支付后取餐号回写
    "callback_dispatched",  # 支付成功回调已分发（watcher 侧）
    "callback_failed",      # 支付成功回调分发失败（watcher 侧重试依据）
    "rolled_back",          # 超时/取消后券使用回滚
    "order_cancelled",      # 订单取消（手动/超时/原价重下）
    "switch_full_price",    # 券差额单切换为原价单
    "cashier_updated",      # 回填实时捕获的官方收银台 URL（mobilegw 短窗）
    "mint_failed",          # frida 自动铸造收银台直链失败（services/cashier_mint 旁路观测）
)


class PaySession(Base):
    """支付会话：一单一链接，pay_token（secrets.token_urlsafe）即 H5 收银台访问凭证。
    续付（continuePay）重铸支付串时原地更新支付字段并追加 PayAttempt，token 永不变。"""

    __tablename__ = "pay_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("chagee_accounts.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    pay_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # token_urlsafe(32)≈43字符
    mode: Mapped[str] = mapped_column(String(16), default="partial")   # partial|full
    status: Mapped[str] = mapped_column(String(16), default="issued", index=True)  # issued|paid|cancelled|expired
    pay_no: Mapped[str] = mapped_column(String(64), default="")
    out_trade_no: Mapped[str] = mapped_column(String(64), default="")  # 每次重铸均变化
    order_str: Mapped[str] = mapped_column(Text, default="")           # 支付宝签名串（H5 页展示用）
    alipay_cashier_url: Mapped[str] = mapped_column(String(512), default="")  # 支付宝官方 H5 收银台 URL（设备级凭证铸造，见 pay_session.alipay_cashier_url）
    total_amount: Mapped[str] = mapped_column(String(16), default="")  # 订单总额
    pay_amount: Mapped[str] = mapped_column(String(16), default="")    # 实付差额（支付宝侧金额）
    coupon_code: Mapped[str] = mapped_column(String(64), default="")
    pay_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pickup_no: Mapped[str] = mapped_column(String(16), default="")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)        # 探针连续失败计数（降频依据）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class PayAttempt(Base):
    """支付串铸造记录：每次 createOrder/continuePay 追加一行（审计与过期时间溯源）。"""

    __tablename__ = "pay_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pay_session_id: Mapped[int] = mapped_column(ForeignKey("pay_sessions.id"), index=True)
    pay_no: Mapped[str] = mapped_column(String(64), default="")
    out_trade_no: Mapped[str] = mapped_column(String(64), default="")
    expire_at: Mapped[str] = mapped_column(String(32), default="")     # 支付宝侧 time_expire 原文
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PayEventLog(Base):
    """支付会话事件流：H5 页面访问/探针/支付发现/回调/回滚/取消/原价切换全链路落库。
    pay_token 只存前 8 位前缀（溯源够用，且事件表比 session 表更易外泄）。"""

    __tablename__ = "pay_event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    pay_token_prefix: Mapped[str] = mapped_column(String(16), default="")   # token 前 8 位
    event: Mapped[str] = mapped_column(String(32), index=True)              # 取值见 PAY_EVENTS
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


# ---------------- 本地菜单规格库（2026-09-27）----------------
# 依据：规格主数据为全局字典（甜度 623882672850116609 / 温度 745317722624679942 /
# 杯型 specId 653599312273510400 在杭州 CN00529 快照与佛山/衡阳/南京下单 wire 完全一致），
# 可本地缓存；权威源 = 游客菜单接口 goods/storeGoodsMenu + goods/detail（无登录态）。
# 用途：① /api/ops/goods 本地优先（免每击实时打线上）；② 客户平台文案规格 →
# ID 组合解析（适配器兜底链：本地库 → 实时回源 → 别名/模糊匹配 → 歧义拒猜）。

class MenuSpecOption(Base):
    """规格主数据（全局，跨门店共享）：kind=spec|attribute|extra。
    半糖=attributeOptionId 623882672850116613 等 ID 全局稳定；新品发售常伴随新选项行。"""

    __tablename__ = "menu_spec_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)              # spec|attribute|extra
    group_id: Mapped[str] = mapped_column(String(32), index=True)          # specId/attributeId/extraId
    group_name: Mapped[str] = mapped_column(String(64), default="")        # 杯型/温度/甜度…（去空格）
    option_id: Mapped[str] = mapped_column(String(32), index=True)         # specOptionId/attributeOptionId
    option_name: Mapped[str] = mapped_column(String(64), default="")
    defaulted: Mapped[bool] = mapped_column(Boolean, default=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    seen_count: Mapped[int] = mapped_column(Integer, default=0)            # 出现的 SPU×门店次数
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (
        UniqueConstraint("kind", "group_id", "option_id", name="uq_spec_option"),
        Index("ix_spec_option_name", "kind", "option_name"),
    )


class MenuGoodsCache(Base):
    """门店×SPU 菜单快照：sku_index 供 skuId 直查 ID 组合；attribute_groups 供文案解析。
    fetched_at 超 TTL 视为陈旧（单 SPU 回源刷新），全店刷新由每日线程/手动触发。"""

    __tablename__ = "menu_goods_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_no: Mapped[str] = mapped_column(String(32), index=True)
    spu_id: Mapped[str] = mapped_column(String(32), index=True)
    spu_name: Mapped[str] = mapped_column(String(255), default="")
    spu_type: Mapped[str] = mapped_column(String(16), default="stand")
    status: Mapped[int] = mapped_column(Integer, default=1)                 # 在售状态
    sale_out: Mapped[bool] = mapped_column(Boolean, default=False)
    default_price: Mapped[str] = mapped_column(String(16), default="")
    sku_index: Mapped[dict] = mapped_column(JSON, default=dict)             # skuId → {price,stock,itemSkuId,specs[]}
    spec_groups: Mapped[list] = mapped_column(JSON, default=list)           # [{groupId,groupName,options[{optionId,optionName,defaulted,sequence}]}]
    attribute_groups: Mapped[list] = mapped_column(JSON, default=list)      # 同上（含 must/multiSelected）
    extra_groups: Mapped[list] = mapped_column(JSON, default=list)          # 加料组（must 组 settle 必带）
    raw: Mapped[dict] = mapped_column(JSON, default=dict)                   # 原始 goods/detail（/goods 端点原样回放）
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (UniqueConstraint("store_no", "spu_id", name="uq_goods_cache"),)


class MenuRefreshLog(Base):
    """菜单刷新审计：手动/定时/TTL 回源三种触发；diff 摘要透出新品与新规格选项
    （地区限定饮品与新品发售的追踪入口）。"""

    __tablename__ = "menu_refresh_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_no: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")      # manual|scheduled|ttl_miss
    spu_total: Mapped[int] = mapped_column(Integer, default=0)
    spu_new: Mapped[int] = mapped_column(Integer, default=0)
    spu_changed: Mapped[int] = mapped_column(Integer, default=0)
    spec_new: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)                # {new_spus:[],new_spec_options:[],changed:[]}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
