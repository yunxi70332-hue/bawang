"""Pydantic 请求/响应模型。敏感字段（token/sk）永不下发，手机号默认脱敏。"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

PHONE_RE = r"^1[3-9]\d{9}$"


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if phone and len(phone) == 11 else (phone or "")


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


class PayModeRequest(BaseModel):
    mode: str = Field(pattern="^(manual|auto)$")


class CashierUrlRequest(BaseModel):
    """云手机实时捕获的支付宝官方收银台 URL 回填（mobilegw 会话仅捕获后短窗有效）。"""
    url: str = Field(min_length=32, max_length=2048)
