"""权限目录与内置角色种子定义。

权限点命名规约：<资源>:<动作>，与后端路由依赖(require_perm)及前端按钮级
权限指令(v-permission)一一对应。内置角色为种子数据，admin 权限全量且不可编辑删除。
"""

# (code, 名称, 分组, 说明)
PERMISSION_CATALOG = [
    ("account:read",   "查看账号",   "账号管理", "查看茶姬账号列表与详情（手机号默认脱敏）"),
    ("account:create", "新建账号",   "账号管理", "创建茶姬账号（生成设备身份 uuid）"),
    ("account:update", "编辑账号",   "账号管理", "编辑账号备注/分组/手机号，查看完整手机号"),
    ("account:delete", "删除账号",   "账号管理", "删除账号及其本地会话文件"),
    ("account:login",  "协议登录",   "账号管理", "触发短信登录/登出/会话状态检查（生产外向动作）"),
    ("feature:menu",   "游客菜单查询", "协议功能", "功能3：游客模式城市/门店/菜单/SKU 查询"),
    ("feature:coupon", "优惠券查询", "协议功能", "功能4：登录态优惠券三列表分类查询"),
    ("feature:order",  "下单",       "协议功能", "功能5：购物车→试算选券→下单（0 元闭环 / 差额支付链接，双支付模式）"),
    ("feature:pickup", "取餐查询",   "协议功能", "功能6：订单列表/详情/取餐码/等待信息查询"),
    ("user:manage",    "用户管理",   "系统管理", "系统用户的增删改查与密码重置"),
    ("role:manage",    "角色管理",   "系统管理", "角色与权限矩阵配置"),
    ("audit:read",     "审计日志",   "系统管理", "查看操作审计日志"),
    ("decision:manage", "决策管理", "下单决策", "套餐配置/券成本规则/决策配置管理、券库存扫描、盈利报表与决策流水查询"),
    ("intake:manage",  "订单中枢管理", "异步订单", "客户订单登记单/队列/死信/接入密钥管理（异步下单中枢）"),
]

PERMISSION_CODES = [p[0] for p in PERMISSION_CATALOG]

BUILTIN_ROLES = [
    {
        "name": "admin",
        "description": "系统管理员：全部权限",
        "permissions": list(PERMISSION_CODES),
    },
    {
        "name": "operator",
        "description": "运营人员：账号维护（不可删除）+ 协议登录 + 功能查询 + 下单/取餐",
        "permissions": [
            "account:read", "account:create", "account:update",
            "account:login", "feature:menu", "feature:coupon", "feature:order",
            "feature:pickup", "audit:read", "decision:manage", "intake:manage",
        ],
    },
    {
        "name": "viewer",
        "description": "只读观察者：仅查看账号与游客菜单",
        "permissions": ["account:read", "feature:menu"],
    },
]

ADMIN_ALL = list(PERMISSION_CODES)


def permission_tree():
    """按分组组织权限目录，供前端角色编辑页渲染勾选矩阵。"""
    groups: dict = {}
    for code, name, group, desc in PERMISSION_CATALOG:
        groups.setdefault(group, []).append({"code": code, "name": name, "description": desc})
    return [{"group": g, "items": items} for g, items in groups.items()]
