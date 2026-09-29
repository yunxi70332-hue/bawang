"""ORM 模型：系统用户 / 角色 / 茶姬账号 / 登录工单 / 订单快照 / 审计日志 / 支付会话
/ 异步订单中枢（客户订单登记 + 持久化消息队列 + 接入密钥，2026-09-29）。"""

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
    order_time: Mapped[str] = mapped_column(String(32), default="")  # 官方下单时间原文（全量扫描回填，区别于 created_at 落库时刻）
    biz_type: Mapped[str] = mapped_column(String(16), default="")    # 履约方式（businessTypeText：自取/外卖，"使用范围"搜索维度）
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


class PayParamRecord(Base):
    """官方收银台支付参数串（独立存储域，2026-09-28 新增）：order_no 一单一活跃记录，
    param_str = 结构化 JSON v1 快照（全量支付参数，契约见 services/pay_params.py）。

    独立性：与 pay_sessions 仅以 order_no/account_id 快照弱关联（同 PayEventLog 口径，
    不设外键）——支付会话重铸/取消/重建后参数串仍完整可查，生命周期独立；
    关联性：order_no 与 order_records/pay_sessions 同键 join，account_id 供账号维度筛选。
    param_str 覆盖式更新（与 alipay_cashier_url 的「最新短窗」语义一致）。
    """

    __tablename__ = "pay_param_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    pay_token_prefix: Mapped[str] = mapped_column(String(16), default="")  # 关联支付会话（前 8 位）
    param_str: Mapped[str] = mapped_column(Text, default="")               # ★ 支付参数串（JSON v1，Python json.loads 直接消费）
    source: Mapped[str] = mapped_column(String(32), default="")            # protocol-mint|frida-mint|manual|static-config
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


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


# ---------------- 下单决策系统（2026-09-28，契约 docs/decision_api_contract.md §1）----------------
# 决策链：套餐（PacketConfig + PacketItem）圈定可接单的价格区间/时段/商品与券规则 →
# 券成本规则（VoucherCostRule）把券映射为采购成本 → decide 逐单评估落 DecisionLog 流水
# （pass/blocked + 阈值/成本明细快照），成单后回填服务端实际抵扣/实付对账。

class PacketConfig(Base):
    """下单套餐：客户支付价区间 + 可用时段 + 套餐级最低利润覆盖，圈定可接单范围。
    items 为空 = 全品类；min_profit 空 = 沿用全局配置（data/decision_config.json）。"""

    __tablename__ = "packet_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)    # 套餐名称
    open_flag: Mapped[bool] = mapped_column(Boolean, default=True)            # 是否开放
    min_order_amount: Mapped[str] = mapped_column(String(16), default="0")    # 最小下单金额（客户支付价下限）
    max_order_amount: Mapped[str] = mapped_column(String(16), default="0")    # 最大下单金额（0=不设上限）
    available_start: Mapped[str] = mapped_column(String(8), default="")       # 可用时段开始 "00:00:00"，空=不限
    available_end: Mapped[str] = mapped_column(String(8), default="")         # 可用时段结束，空=不限
    min_profit: Mapped[str] = mapped_column(String(16), default="")           # 套餐级最低利润覆盖（元），空=用全局
    max_order_cost: Mapped[str] = mapped_column(String(16), default="")       # 套餐级最大承受下单金额覆盖（元），空=用全局（§10）
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    items: Mapped[list["PacketItem"]] = relationship(
        back_populates="packet", cascade="all, delete-orphan")   # PUT items 全量替换由 delete-orphan 兜底


class PacketItem(Base):
    """套餐商品行（packet_id + sku_id 唯一）：spu/sku 圈定可接商品，face_price 由
    menu_goods_cache 自动回填；券规则 JSON 控制选券
    （{"match_type":"template_contains","match_value":"代金券"}，null=不限）。"""

    __tablename__ = "packet_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    packet_id: Mapped[int] = mapped_column(ForeignKey("packet_configs.id"), index=True)
    spu_id: Mapped[str] = mapped_column(String(32), index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    product_name: Mapped[str] = mapped_column(String(128), default="")
    face_price: Mapped[str] = mapped_column(String(16), default="")       # 面价（menu_goods_cache 自动回填）
    premium_price: Mapped[str] = mapped_column(String(16), default="")    # 溢价（可空）
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)      # 是否需溢价券商品
    normal_coupon_rule: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 常规券规则 | null=不限
    premium_coupon_rule: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 溢价券规则（同构）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("packet_id", "sku_id", name="uq_packet_item"),)

    packet: Mapped["PacketConfig"] = relationship(back_populates="items")


class VoucherCostRule(Base):
    """券采购成本规则：把券模板/面额映射为实际采购成本价（元，String 金额）。
    匹配链见 services/decision.resolve_cost —— enabled 规则按 priority 升序逐条：
    template_exact / template_contains / benefit_regex / coupon_prefix 四种命中方式，
    face_value 非空须等于券面额；全部未命中按 面额×cost_fallback_ratio 保守计。
    category_id 软关联券成本子类（业务分类层，0=未分类；子类删除时回退 0）。"""

    __tablename__ = "voucher_cost_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))                          # 规则名
    match_type: Mapped[str] = mapped_column(String(32))                    # template_exact|template_contains|benefit_regex|coupon_prefix
    match_value: Mapped[str] = mapped_column(String(128))                  # 匹配值（regex 时为正则）
    face_value: Mapped[str] = mapped_column(String(16), default="")        # 面额校验（非空时须等于券面额才命中）
    cost_price: Mapped[str] = mapped_column(String(16))                    # ★采购成本（元，String 金额）
    priority: Mapped[int] = mapped_column(Integer, default=100)            # 越小越优先
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    category_id: Mapped[int] = mapped_column(Integer, default=0)           # 关联券成本子类（0=未分类）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class VoucherCostCategory(Base):
    """券成本子类=业务分类层（采购付费/活动免费/银行渠道），自动归类匹配；
    成本金额仍由 voucher_cost_rules 唯一决定。
    归类链见 services/decision.classify_category —— enabled 子类按 priority 升序逐条，
    四类匹配语义与 resolve_cost 完全一致（共用 _match_hit），全不命中=未分类（0）；
    biz_type 为业务口径（paid|free|bank|other），sort 仅控展示顺序。"""

    __tablename__ = "voucher_cost_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 子类名称
    biz_type: Mapped[str] = mapped_column(String(16))                       # paid|free|bank|other
    match_type: Mapped[str] = mapped_column(String(32))                     # template_exact|template_contains|benefit_regex|coupon_prefix
    match_value: Mapped[str] = mapped_column(String(128))                   # 匹配值（regex 时为正则）
    priority: Mapped[int] = mapped_column(Integer, default=100)             # 越小越优先
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)                   # 展示排序（越小越靠前）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class DecisionLog(Base):
    """决策流水：每次 decide 评估（order_no 空=未成单）与阈值判定全量留痕。
    金额列全 String 快照；threshold_json 存阈值来源（global|packet），plan_json 存
    决策明细（cost_breakdown + coupon 摘要 + alternatives）；成单后回填
    deduction_actual/pay_actual（settle 服务端确认值）对账估算偏差。"""

    __tablename__ = "decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(64), default="", index=True)    # 空=decide 评估（未成单）
    packet_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    packet_name: Mapped[str] = mapped_column(String(64), default="")             # 快照
    account_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    coupon_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    revenue: Mapped[str] = mapped_column(String(16), default="")                 # 客户支付价
    total_trade_price: Mapped[str] = mapped_column(String(16), default="")       # 茶姬订单总额（估算或服务端）
    voucher_cost: Mapped[str] = mapped_column(String(16), default="")            # 券成本
    pay_cost: Mapped[str] = mapped_column(String(16), default="")                # 差额实付
    overhead: Mapped[str] = mapped_column(String(16), default="")                # 杂费
    total_cost: Mapped[str] = mapped_column(String(16), default="")
    profit: Mapped[str] = mapped_column(String(16), default="")
    margin: Mapped[str] = mapped_column(String(8), default="")                   # 百分比 "23.5"（不带%）
    verdict: Mapped[str] = mapped_column(String(16), default="", index=True)     # pass|blocked
    blocked_reason: Mapped[str] = mapped_column(String(255), default="")
    threshold_json: Mapped[dict] = mapped_column(JSON, default=dict)             # {"min_profit":"2.00","min_margin":"","source":"global|packet"}
    plan_json: Mapped[dict] = mapped_column(JSON, default=dict)                  # 决策明细快照（cost_breakdown + coupon 摘要 + alternatives）
    deduction_actual: Mapped[str] = mapped_column(String(16), default="")        # 成单后服务端确认抵扣
    pay_actual: Mapped[str] = mapped_column(String(16), default="")              # 成单后实付
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


# ---------------- 下单方案（2026-09-29 §11）：策略 + 券优先级层级 ----------------
# 方案 = 用户可建可选的下单控制单元：一个排序策略（成本最优/零元优先/临期优先）+
# 有序的券优先级层级（第一/第二/第三…每层一条券匹配规则）。decide 与 create 自动
# 切换按方案执行；未选方案时保持系统自动行为（§10 四级漏斗）。

# OrderPlan.strategy 排序策略枚举：rank_candidates 的第三级排序因子按此切换
PLAN_STRATEGIES = ("cost_first", "zero_pay", "expiry_first")
PLAN_STRATEGY_LABELS = {"cost_first": "成本最优", "zero_pay": "零元优先", "expiry_first": "临期优先"}


class OrderPlan(Base):
    """下单方案：名称 + 策略 + 券优先级层级链。priorities 按 level 升序逐层选券，
    层内按策略排序；不匹配任何层的券排在全部层之后（不浪费可用券）。
    注：方案与套餐不做绑定（2026-09-29 §13 曾实现 packet_id 绑定后按用户决策移除——
    套餐商品白名单与方案饮品白名单为 AND 关系，空交集会使方案永久接不了单）；
    套餐始终由 decide 自动匹配或 body.packet_id 指定，管理入口在方案页「套餐库」Tab。
    旧库 order_plans.packet_id 列残留无害（DEFAULT 0，模型不再引用）。"""

    __tablename__ = "order_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)      # 方案名称
    strategy: Mapped[str] = mapped_column(String(16), default="cost_first")    # cost_first|zero_pay|expiry_first
    drink_info: Mapped[str] = mapped_column(String(255), default="")           # 饮品信息（方案级必填编辑框；下单选此方案时自动带入订单）
    max_pay_amount: Mapped[str] = mapped_column(String(32), nullable=False, server_default="", default="")  # 方案级支付金额上限（元，空串=未配置；本单差额实付超限即拒单，未配置/非法 fail-closed 默认拒绝）
    note: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    priorities: Mapped[list["OrderPlanCouponPriority"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan",
        order_by="OrderPlanCouponPriority.level")   # 读取即按层级排序
    drinks: Mapped[list["OrderPlanDrink"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan")   # 饮品管理 Tab 多选关联（白名单，空=不限）


class OrderPlanCouponPriority(Base):
    """券优先级层级（plan_id + level 唯一）：level=1 即「第一优先」。匹配语义与
    成本规则/子类一致（四类 match_type），face_value 非空时须等于券面额才命中。"""

    __tablename__ = "order_plan_coupon_priorities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("order_plans.id"), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)                      # 1=第一优先，2=第二…
    name: Mapped[str] = mapped_column(String(64), default="")                  # 层名（如「20元DN券」）
    match_type: Mapped[str] = mapped_column(String(32), default="template_contains")
    match_value: Mapped[str] = mapped_column(String(128), default="")
    face_value: Mapped[str] = mapped_column(String(16), default="")            # 面额校验（空=不限）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("plan_id", "level", name="uq_plan_priority_level"),)

    plan: Mapped["OrderPlan"] = relationship(back_populates="priorities")


class OrderPlanDrink(Base):
    """方案关联饮品（plan_id + sku_id 唯一）：饮品管理 Tab 多选结果。drinks 非空时
    decide 指定该方案仅可下单这些饮品（白名单语义，空=不限）。"""

    __tablename__ = "order_plan_drinks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("order_plans.id"), index=True)
    spu_id: Mapped[str] = mapped_column(String(32), default="")
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    drink_name: Mapped[str] = mapped_column(String(128), default="")   # 饮品名快照（含规格）
    face_price: Mapped[str] = mapped_column(String(16), default="")    # 面价快照（选品时菜单价）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("plan_id", "sku_id", name="uq_plan_drink_sku"),)

    plan: Mapped["OrderPlan"] = relationship(back_populates="drinks")


# ---------------- 异步订单中枢（2026-09-29，docs/intake_system.md）----------------
# 生产-消费模型：接收层（内部 JWT / 外部 X-Api-Key）毫秒级落库即返回 202，重活
# （decide 选号选券 → settle 试算 → create 下单 → 支付收口取餐码）由 order-worker
# 线程池异步执行。队列即 SQLite 表（WAL 单写者天然无竞态；入队与订单登记同一事务
# 原子提交），重试退避 / 死信 / 宕机恢复（孤儿回收）全在应用层可控，语义 at-least-once
# + 状态机 CAS 推进的消费幂等。
#
# 关联锚：CustomerOrder.chagee_order_no ↔ order_records.order_no / pay_sessions.order_no
# 同键弱关联（同 PayEventLog 口径不设外键）；取餐码复用现有四通道（零元即时 /
# pay-watcher 2s / H5 探针 / 全量扫描）自动回填，中枢只做缓存列同步。

# CustomerOrder.status 状态机（登记单生命周期；completed/failed/cancelled 为终态）
INTAKE_STATUS = (
    "registered",        # 已登记（未入队瞬态：正常路径与消息同事务直接落 enqueued）
    "enqueued",          # 已入队待消费
    "processing",        # worker 认领执行中（step 记录 decide/settle/create 细进度）
    "awaiting_payment",  # 已成差额单，H5 支付链接已下发，等待 pay-watcher 收口
    "completed",         # 终态成功（零元单直接至此；差额单支付后回填取餐码至此）
    "failed",            # 终态失败（重试耗尽 / 致命错误 / 决策 blocked / 支付取消）
    "cancelled",         # 终态取消（入队后消费前人工取消）
)
INTAKE_STATUS_LABELS = {
    "registered": "已登记", "enqueued": "已入队", "processing": "处理中",
    "awaiting_payment": "待支付", "completed": "已完成", "failed": "已失败",
    "cancelled": "已取消",
}

# OrderMessage.status 消息生命周期（= SQLite 持久化队列的行状态；dead 即死信）
MSG_STATUS = ("pending", "processing", "done", "dead")


class CustomerOrder(Base):
    """客户订单登记单：外部平台 / 内部工作台提交的代下单请求（与 order_records 的
    茶姬官方订单一一弱关联）。customer_order_no 全局唯一 = 接收幂等键（重复提交返回
    现状不重复入队）。payload 为归一化后的内部标准报文（worker 据此驱动 decide 链），
    raw_payload 保留原始提交快照供审计；金额列全 String（协议层金额口径）。"""

    __tablename__ = "customer_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32), default="internal", index=True)  # internal|external:<platform>
    api_key_id: Mapped[int] = mapped_column(Integer, default=0, index=True)          # 外部来源归属密钥（0=内部）
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    callback_url: Mapped[str] = mapped_column(String(512), default="")               # 按单回调地址（空=不回调）
    status: Mapped[str] = mapped_column(String(24), default="registered", index=True)
    step: Mapped[str] = mapped_column(String(32), default="")                        # 当前执行步骤（decide/settle/create/…）
    progress: Mapped[str] = mapped_column(String(255), default="")                   # 人类可读进度/最近动作
    account_id: Mapped[int] = mapped_column(Integer, default=0, index=True)          # 执行时选定的茶姬账号
    chagee_order_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    pickup_no: Mapped[str] = mapped_column(String(16), default="")                   # 取餐码缓存（watcher 收口时同步）
    pay_url: Mapped[str] = mapped_column(String(512), default="")                    # H5 收银台链接（差额单）
    pay_amount: Mapped[str] = mapped_column(String(16), default="")                  # 实付差额快照
    coupon_code: Mapped[str] = mapped_column(String(64), default="")                 # 实际用券
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)                        # 消费尝试次数（消息侧口径镜像）
    trace_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)     # 首次被 worker 认领
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)    # 进入终态时刻
    __table_args__ = (
        Index("ix_customer_orders_status_created", "status", "created_at"),
    )


class OrderMessage(Base):
    """持久化消息（SQLite 队列行）：topic 默认 order.create。认领 = 单条
    UPDATE…RETURNING 原子置 processing（attempts 同步 +1，宕机崩溃不丢计数）；
    失败按退避回 pending（next_visible_at），attempts ≥ max_attempts 置 dead（死信，
    管理端点可重放）；processing 超 visibility timeout 由孤儿回收线程复位（崩溃恢复）。"""

    __tablename__ = "order_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(32), default="order.create", index=True)
    customer_order_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_visible_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    locked_by: Mapped[str] = mapped_column(String(64), default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("ix_order_messages_status_visible", "status", "next_visible_at"),
    )


class IntakeApiKey(Base):
    """外部接入密钥：X-Api-Key 的 sha256 哈希落库（明文仅创建响应返回一次）。
    active=False 即时吊销；last_used_at 供审计（节流更新，非每次必写）。"""

    __tablename__ = "intake_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # sha256 hex
    label: Mapped[str] = mapped_column(String(64), default="")                   # 用途备注（如「XX平台对接」）
    source: Mapped[str] = mapped_column(String(32), default="external")          # 登记单 source 前缀来源
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class KeepaliveRun(Base):
    """账号保活跃任务运行头（2026-09-29）：每天定时（默认 10:30）用在线账号 token
    间歇访问广东省内门店菜单接口的一轮执行快照。status：running/success/partial/
    failed；trigger：schedule 定时 / manual 手动。逐请求明细见 KeepaliveRecord。"""

    __tablename__ = "keepalive_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")     # schedule | manual
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    province: Mapped[str] = mapped_column(String(32), default="广东")
    city_total: Mapped[int] = mapped_column(Integer, default=0)              # 枚举到的省份城市数
    store_total: Mapped[int] = mapped_column(Integer, default=0)             # 省份门店总数（截断前）
    stores_planned: Mapped[int] = mapped_column(Integer, default=0)          # 本轮实际访问门店数
    accounts_total: Mapped[int] = mapped_column(Integer, default=0)
    accounts_expired: Mapped[int] = mapped_column(Integer, default=0)        # whoami 判失效并标记的账号数
    requests_total: Mapped[int] = mapped_column(Integer, default=0)          # 菜单+whoami 请求总数（终态计）
    requests_ok: Mapped[int] = mapped_column(Integer, default=0)
    requests_failed: Mapped[int] = mapped_column(Integer, default=0)
    avg_ms: Mapped[str] = mapped_column(String(16), default="")              # 成功请求平均耗时（ms，两位小数字符串）
    note: Mapped[str] = mapped_column(String(512), default="")               # 失败/告警摘要（城市枚举失败等）
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KeepaliveRecord(Base):
    """保活跃逐请求明细：action=whoami（token 鉴权校验）| menu（门店菜单访问）；
    status 记录终态（ok / expired / errcode=N / HTTP错误 / 异常名）；attempt 为最终
    成功/放弃时的尝试序号（1 起）。run_id 级联删除。"""

    __tablename__ = "keepalive_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("keepalive_runs.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    account_label: Mapped[str] = mapped_column(String(64), default="")
    store_no: Mapped[str] = mapped_column(String(32), default="")
    store_name: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(16), default="menu")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(64), default="")
    ms: Mapped[int] = mapped_column(Integer, default=0)                      # 最终一次尝试耗时
    error: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
