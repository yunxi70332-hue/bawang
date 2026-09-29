# 下单决策系统 API 契约（冻结版 v1，2026-09-28）

> 本文是决策系统实施的唯一契约：后端路由、前端 api/index.js、视图、测试全部以本文字段名为准。
> 实施分支：feature/decision-system（基线 4cebc81）。

## 0. 文件独占表（并行子代理冲突防护）

| 文件 | 独占者 |
|---|---|
| server/models.py、server/permissions.py、server/services/decision.py（新） | 波1-A（数据层） |
| web/src/views/PacketConfigView.vue（新）、VoucherCostView.vue（新）、web/src/api/index.js、web/src/router/index.js、web/src/layouts/MainLayout.vue | 波1-B（前端骨架） |
| server/routers/decision.py（新）、server/app.py、server/routers/orders.py、server/schemas.py | 波2-C（后端 API） |
| web/src/views/OrderWorkbenchView.vue、web/src/views/DashboardView.vue、server/services/dashboard_stats.py | 波2-D（前端集成） |
| server/test_decision_offline.py（新）、docs/decision_system_design.md（新） | 波3-E（测试文档） |

## 1. 数据模型（models.py 追加 4 表，对齐现有 Mapped/String金额/JSON 风格）

```python
class PacketConfig(Base):
    __tablename__ = "packet_configs"
    id: int PK
    name: str(64) unique index          # 套餐名称
    open_flag: bool default True        # 是否开放
    min_order_amount: str(16) default "0"    # 最小下单金额（客户支付价下限，String 金额）
    max_order_amount: str(16) default "0"    # 最大下单金额（0=不设上限）
    available_start: str(8) default ""       # 可用时段开始 "00:00:00"，空=不限
    available_end: str(8) default ""         # 可用时段结束，空=不限
    min_profit: str(16) default ""           # 套餐级最低利润覆盖（元），空=用全局
    note: str(255) default ""
    created_at / updated_at: datetime

class PacketItem(Base):
    __tablename__ = "packet_items"
    id: int PK
    packet_id: int FK(packet_configs.id) index
    spu_id: str(32) index
    sku_id: str(32) index
    product_name: str(128) default ""
    face_price: str(16) default ""      # 面价（menu_goods_cache 自动回填）
    premium_price: str(16) default ""   # 溢价（可空）
    is_premium: bool default False      # 是否需溢价券商品
    normal_coupon_rule: JSON nullable   # {"match_type":"template_contains","match_value":"代金券"} | null=不限
    premium_coupon_rule: JSON nullable  # 同构
    created_at: datetime
    __table_args__ = (UniqueConstraint("packet_id","sku_id", name="uq_packet_item"),)

class VoucherCostRule(Base):
    __tablename__ = "voucher_cost_rules"
    id: int PK
    name: str(64)                       # 规则名
    match_type: str(32)                 # template_exact|template_contains|benefit_regex|coupon_prefix
    match_value: str(128)               # 匹配值（regex 时为正则）
    face_value: str(16) default ""      # 面额校验（非空时须等于券面额才命中）
    cost_price: str(16)                 # ★采购成本（元，String 金额）
    priority: int default 100           # 越小越优先
    enabled: bool default True
    note: str(255) default ""
    created_at / updated_at: datetime

class DecisionLog(Base):
    __tablename__ = "decision_logs"
    id: int PK
    order_no: str(64) default "" index       # 空=decide 评估（未成单）
    packet_id: int default 0 index
    packet_name: str(64) default ""          # 快照
    account_id: int default 0 index
    coupon_code: str(64) default "" index
    revenue: str(16) default ""              # 客户支付价
    total_trade_price: str(16) default ""    # 茶姬订单总额（估算或服务端）
    voucher_cost: str(16) default ""         # 券成本
    pay_cost: str(16) default ""             # 差额实付
    overhead: str(16) default ""             # 杂费
    total_cost: str(16) default ""
    profit: str(16) default ""
    margin: str(8) default ""                # 百分比 "23.5"（不带%）
    verdict: str(16) default "" index        # pass|blocked
    blocked_reason: str(255) default ""
    threshold_json: JSON default dict        # {"min_profit":"2.00","min_margin":"","source":"global|packet"}
    plan_json: JSON default dict             # 决策明细快照（cost_breakdown + coupon 摘要 + alternatives）
    deduction_actual: str(16) default ""     # 成单后服务端确认抵扣
    pay_actual: str(16) default ""           # 成单后实付
    created_at: datetime index
```

## 2. 全局配置 `data/decision_config.json`（services/decision.py 读写，缺省自动落盘）

```json
{
  "min_profit": "2.00",        // 全局每单最低利润（元）
  "min_margin": "",            // 全局最低利润率%（空=不启用）
  "overhead": "0",             // 每单杂费（元）
  "cost_fallback_ratio": "1.0" // 成本规则未命中时按面额×该系数保守计
}
```

## 3. 权限（permissions.py）

PERMISSION_CATALOG 追加：`("decision:manage", "决策管理", "下单决策", "套餐配置/券成本规则/决策配置管理、券库存扫描、盈利报表与决策流水查询")`
admin（自动全量）与 operator（permissions 列表追加）获得；viewer 不获得。
- 套餐/成本规则/配置/扫描/报表/流水 → `require_perm("decision:manage")`
- `POST /api/ops/orders/decide` → `require_perm("feature:order")`

## 4. services/decision.py 纯函数契约（波1-A 实现，波2-C 调用）

全 Decimal 计算，金额出入一律 str。

```python
classify_coupon(benefit_text, benefit2_text, template_name, biz_type) -> dict
    # {"kind": "face|discount|exchange|unknown", "face": Decimal|None, "rate": Decimal|None}
    # face: benefitText "N元"/"满M减N" → face=N；discount: "X折" → rate=X/10；
    # exchange: benefit2Text/templateName 含 兑换/换购；否则 unknown（按 face 尝试解析，解析不出 face=None）
parse_benefit(benefit_text) -> Decimal | None        # "20元"→20
resolve_cost(rules, record_fields, config) -> dict
    # {"cost": Decimal, "source": "rule:<id>|fallback", "rule": VoucherCostRule|None}
    # 匹配链：enabled 规则按 priority 升序逐条：template_exact(=template_name) /
    # template_contains(in template_name) / benefit_regex(re.search 于 template_name+benefit_text) /
    # coupon_prefix(coupon_code startswith)；face_value 非空须 == 券面额；未命中→cost=面额×fallback_ratio, source="fallback"
estimate_deduction(kind, face, rate, total) -> (Decimal deduction, bool estimated)
    # face → min(face,total), estimated=False；discount → total*(1-rate), estimated=True；
    # exchange → total, estimated=True；unknown且face → min(face,total) estimated=True；否则 (0, True)
match_packets(packets, sku_id, price: Decimal, now: datetime) -> list[PacketConfig]
    # 命中：open_flag 且 price∈[min,max(0=∞)] 且时段 且（items 为空=全品类 或 sku_id 在 items 中）
evaluate_cost(revenue, total, deduction, voucher_cost, overhead) -> dict  # cost_breakdown（见 §6 decide 响应）
check_threshold(breakdown, min_profit, min_margin) -> (verdict, reason)   # profit<min_profit 或 margin<min_margin → blocked
rank_candidates(candidates, revenue, total, overhead, min_profit, min_margin) -> list[dict]
    # candidates: [{record, cost, source, kind, face, rate}] → 每个算 break-down+verdict → pass 者按 total_cost 升序
load_config() -> dict / save_config(dict)
```

## 5. REST 端点（routers/decision.py，router 前缀 `/api/ops/decision`，app.py 注册）

响应风格对齐现有：列表 `{total, items}`；金额字段全 string；时间 `YYYY-MM-DD HH:MM:SS`。

### 套餐
- `GET /packets?keyword=&open=&page=1&page_size=20` → `{total, items:[packet_summary]}`
  packet_summary = `{id,name,open_flag,min_order_amount,max_order_amount,available_start,available_end,min_profit,note,item_count,created_at,updated_at}`
- `POST /packets` body `{name,min_order_amount,max_order_amount,available_start,available_end,min_profit,note,items:[packet_item_body]}` → packet_detail（同 GET /{id}）
- `GET /packets/{id}` → packet_detail = summary + `items:[{id,packet_id,spu_id,sku_id,product_name,face_price,premium_price,is_premium,normal_coupon_rule,premium_coupon_rule,created_at}]`
- `PUT /packets/{id}` 同 POST body（items 全量替换）→ packet_detail
- `DELETE /packets/{id}` → `{ok:true}`
- `POST /packets/{id}/toggle-open` → packet_detail

### 成本规则
- `GET /cost-rules` → `{items:[{id,name,match_type,match_value,face_value,cost_price,priority,enabled,note,created_at,updated_at}]}`
- `POST /cost-rules` / `PUT /cost-rules/{id}` / `DELETE /cost-rules/{id}`
- `POST /cost-rules/import` body `{rules:[{...同POST字段}]}` → `{imported:n, skipped:n, errors:[...]}`（name 重复跳过）

### 扫描与库存
- `POST /scan` body `{account_ids?:[int]}`（空=全部在线账号）→ 复用 ops.coupons_sync_all 内部逻辑拉券后，对全库 effective/settle_available 券算成本 → `{accounts_scanned, coupons_total, coupons_with_cost, coupons_unknown_cost, total_face, total_cost_estimate}`
- `GET /coupon-inventory?keyword=&account_id=&usable=&page=&page_size=` → `{total, items:[{coupon_code,template_name,benefit_text,account_id,account_label,amount,threshold_tips,usable_scenes,use_start_time,use_end_time,can_discount,bucket,coupon_kind,cost_price,cost_source,usable(bool),unusable_reason,last_order_no}]}`

### 配置 / 报表 / 流水
- `GET /config` → decision_config.json 原文；`PUT /config` body 同构 → 落盘后返回
- `GET /profit-report?from=&to=&packet_id=` → `{summary:{orders,revenue_total,cost_total,profit_total,margin_avg,blocked_count}, by_packet:[{packet_id,packet_name,orders,revenue,cost,profit}], by_coupon_template:[{template_name,uses,voucher_cost,pay_cost}]}`
- `GET /logs?page=&page_size=&verdict=&packet_id=&order_no=` → `{total, items:[DecisionLog 全字段]}`

### 决策（挂 orders 路由或 decision 路由均可，冻结为 decision.py 内）
- `POST /api/ops/orders/decide` body：
```json
{
  "sku_id": "655441178728091650",   // 客户平台 linkId
  "quantity": 1,
  "spec_list": ["大杯","少冰","半糖"],  // 文案，可空
  "store_no": "CN00529",
  "customer_price": "12.00",        // 客户支付价（revenue）
  "packet_id": 0,                   // 可选，指定则校验命中
  "allow_full_price": false,        // 无券时是否允许原价单
  "deep": false                     // true=对 top1 候选账号做真实 settle 探针
}
```
- 响应：
```json
{
  "verdict": "pass",
  "blocked_reason": "",
  "packet": packet_summary | null,
  "item": packet_detail.items[0] | null,
  "account_id": 3, "account_label": "账号3",
  "coupon": {coupon_code, template_name, benefit_text, amount, coupon_kind} | null,
  "cost_breakdown": {
    "revenue": "12.00", "total_trade_price": "18.00", "price_source": "menu|settle",
    "deduction": "18.00", "deduction_estimated": true,
    "voucher_cost": "8.00", "pay_cost": "0.00", "overhead": "0.00",
    "total_cost": "8.00", "profit": "4.00", "margin": "33.3",
    "cost_source": "rule:1", "coupon_kind": "face"
  },
  "threshold": {"min_profit": "2.00", "min_margin": "", "source": "global"},
  "alternatives": [ {"account_id":5,"account_label":"账号5","coupon_code":"...","template_name":"...","total_cost":"9.00","profit":"3.00"} ],
  "settle_prefill": { "spu_id":"...","spu_name":"...","sku_id":"...","sku_name":"...","item_sku_id":"...","sale_price":18.0,"quantity":1,"spec_list":[],"attribute_list":[],"extra_list":[],"spu_type":"stand" }
}
```
- decide 每次调用写一条 decision_logs（order_no 空）；blocked 也写。
- total_trade_price 估算源：本地 menu_goods_cache sku 价×quantity（price_source="menu"）；deep=true 时对 top1 账号真实 settle_direct(no_recommend) 取服务端总额（price_source="settle"）。

## 6. 成单挂钩（波2-C，orders.py）

`order_create` 成功落库处：若请求带 `decision_log_id`（OrderCreateRequest 新增可选字段 int，默认 0），把该行 decision_logs 回填 order_no/account_id/coupon_code/deduction_actual（settle 复跑的服务端抵扣）/pay_actual（outcome.pay_amount）。无 decision_log_id 时行为完全不变（向后兼容）。
decide 响应新增顶层字段 `decision_log_id`（供 create 传回）。

## 7. 前端契约（波1-B / 波2-D）

api/index.js 新增 `apiDecision`（全部端点封装）：`packets(params)` `packetCreate(data)` `packetUpdate(id,data)` `packetDelete(id)` `packetToggleOpen(id)` `costRules()` `costRuleCreate(data)` `costRuleUpdate(id,data)` `costRuleDelete(id)` `costRuleImport(rules)` `configGet()` `configPut(data)` `scan(data)` `couponInventory(params)` `decide(data)` `profitReport(params)` `decisionLogs(params)`。

路由与菜单（B 负责）：
- `/decision/packets` → PacketConfigView，meta.perm `decision:manage`，菜单"下单决策/套餐配置"
- `/decision/costs` → VoucherCostView，meta.perm `decision:manage`，菜单"下单决策/券成本与阈值"

视图要求（第一版实用优先，对齐仓库 AccountsView/UsersView 的表格+弹窗模式，Element Plus）：
- PacketConfigView：搜索（名称关键字/开放状态）+ 表格（名称/价格区间/开放 el-switch/商品数/最低利润/更新时间/操作 编辑删除）+ 新增/编辑弹窗（基础字段 + 内嵌商品子表格：行内选择 spu/sku（复用 apiOps.menu/apiOps.goods 拉菜单，弹层选择后自动回填 product_name/face_price）、溢价、券规则 match_type+match_value 简易下拉+输入）。
- VoucherCostView：Tab1 成本规则表格+弹窗 CRUD + 导入按钮（JSON 文本域 → costRuleImport）+ 每规则启用开关；Tab2 决策配置表单（min_profit/min_margin/overhead/cost_fallback_ratio → configPut）。

波2-D（工作台+仪表盘）：
- OrderWorkbenchView 步骤②选完商品后新增"决策评估"卡：调 decide（customer_price 输入框 + 评估按钮）→ 展示 cost_breakdown 明细与阈值灯；verdict=pass 提供"应用推荐"（自动切换 account_id、选中推荐券，提交时带 decision_log_id）；blocked 显示原因并禁用提交（仅提示，不硬断 existing 流程——不加决策参数时旧流程照常）。
- DashboardView 新增盈利卡（profit_total/margin_avg/blocked_count，调 profitReport 近7日）。
- dashboard_stats.py：stats 响应追加 `profit` 域（同 profit-report summary 口径，近 7 日）。

## 8. 环境与测试约定

- Python：`C:\baidunetdiskdownload\霸王茶姬\.venv_verify\Scripts\python.exe`（唯一环境）
- offline 测试：`CHAGEE_MINT_ENABLED=0`、`CHAGEE_OPLOG_DB` 指测试库、`CHAGEE_RECONCILE_INTERVAL_SECONDS<=0`、`CHAGEE_MENU_REFRESH_INTERVAL_SECONDS<=0`、`CHAGEE_PAYWATCH_INTERVAL_SECONDS<=0`、`CHAGEE_TUNNEL_ENABLED=0`
- 测试模式：`database.DB_PATH` 指向 `data/test_decision.db` 重建 engine + monkeypatch `services.chagee_bridge.build_client` 回放 wire（参考 test_orders_offline.py 头注）

## 9. 券成本子类（2026-09-29 增补：业务分类层，与优惠券查询联动）

**语义：子类只做分类，不做成本。** 成本金额仍由 voucher_cost_rules 的 resolve_cost 链唯一决定。

- 表 `voucher_cost_categories`：name(unique)/biz_type(paid|free|bank|other)/match_type(同规则四类)/match_value/priority/enabled/note/sort；`voucher_cost_rules` 加 category_id(int,0=未分类，seed 轻量迁移)
- 纯函数 `classify_category(categories, record_fields) -> {"category_id","category_name","biz_type"}`（enabled 按 priority 升序，四类匹配与 resolve_cost 共用 `_match_hit`，全不命中→0/""/""）
- 端点：`GET/POST/PUT/DELETE /api/ops/decision/cost-categories(/{id})`（GET 带 coupon_count/face_total/cost_total，**仅统计 effective+settle_available 两桶**；DELETE 时挂靠规则 category_id 重置 0；权限 decision:manage）
- cost-rules 全端点透传 category_id 并回显 category_name
- `GET /api/ops/coupons/search`（优惠券查询模块联动，权限不变 feature:coupon）：行加 cost_price/cost_source/cost_category_id/cost_category_name/biz_type（fail-soft）；新参数 `cost_category`（0=不筛，**-1=未分类**——前端档案库筛选项约定值）；stats.by_category 为 {category_id: count}（**含全部桶**，与子类统计的两桶口径区分）
- coupon-inventory 行加 cost_category_* 三字段；scan 汇总加 by_category；decide 的 cost_breakdown 加 cost_category_name
- 前端：VoucherCostView 首位 Tab「成本子类」（CRUD+统计+查看券跳转 /ops/coupons?tab=archive&cost_category=id）；CouponQueryView 档案库加「成本/子类」列+子类筛选（fail-soft）+路由深链预设

## 10. 券选择优先级 + 自动切换 + 最大承受金额（2026-09-29）

**四级漏斗（券选择优先级的规范表述）**：
1. 资格（硬性）：在线账号 → 桶∈effective/settle_available → last_order_no 空 → 有效期 → can_discount → 满减门槛≤总额 → 套餐券规则（is_premium 只认溢价券规则，null=不限）
2. 成本合格（硬性）：min_profit / min_margin / **max_order_cost** 三阈值任一不过即出局
3. 排序（软性）：total_cost 升序 → 同成本面额大者优先 → 同成本同面额 **use_end_time 近者优先（临期因子）**
4. 兜底：无合格券 → allow_full_price 评估原价单，否则 blocked

**库存检查两阶段**：decide=档案库口径预筛；create=服务端实时事实（试算在列+五重验证+settle 复跑）——create 阶段失败即触发自动切换。

**auto_fallback（OrderCreateRequest.auto_fallback: bool，默认 false）**：true 时首选券（不在列/验证拒绝/settle 复跑异常）→ `_fallback_rank_codes` 从 draft.settle_base.available_coupons 按四级漏斗排次优（阈值口径：带 decision_log_id 复用其 threshold_json+revenue，否则全局且利润类跳过、仅 max_order_cost 生效）→ 逐张完整验证+settle 复跑，最多 3 张；跳过记 CouponUsageLog(rejected, fail_reason 前缀「券自动切换跳过」)+oplog coupon.fallback_skip；耗尽 400「券自动切换全部失败，已尝试…」；成单记实际用券 + oplog coupon.fallback_applied；createOrder 不在重试范围。跨账号切换不在本期（decide 推荐已覆盖）。

**max_order_cost（最大承受下单金额）**：`total_cost > max_order_cost` → blocked「订单成本 X 元超过最大承受金额 Y 元」。全局 decision_config.json（默认 ""=不限）+ 套餐级 packet_configs.max_order_cost 覆盖（任一套餐级阈值非空 → threshold.source="packet"）；check_threshold 追加第 4 参数（默认 "" 向后兼容）；rank_candidates 排序键第 ③ 因子=use_end_time；decide 响应 threshold 块与 alternatives 元素均含 max_order_cost/use_end_time。

## 11. 下单方案：策略 + 券优先级层级（2026-09-29）

**方案 = 用户可建可选的下单控制单元**：一个排序策略 + 有序券优先级层级链。decide 与 create 自动切换按方案执行；未选方案（plan_id=0）保持 §10 自动行为。

- 表：`order_plans`（name unique/strategy/note/enabled）+ `order_plan_coupon_priorities`（plan_id FK/level 1..N UQ(plan_id,level)/name/match_type 四类/match_value/face_value 校验）
- 策略枚举（rank_candidates 第 strategy 参数）：`cost_first` 成本最优（默认原行为）/ `zero_pay` 零元优先（pay_cost==0 在前）/ `expiry_first` 临期优先
- 纯函数 `apply_priority_tiers(ranked, tiers) -> (重排后, tier_map)`：按 level 升序逐层匹配（_match_hit + face_value），层间按层序、层内保持策略排序，不匹配任何层的候选排全部层后；tier_map={coupon_code:{level,name}}
- 端点：`GET/POST /decision/order-plans`、`PUT/DELETE /decision/order-plans/{id}`（priorities 全量替换 clear+flush；重名/层重复 400；decision:manage）
- `DecideRequest.plan_id`（0=自动；不存在 422）：响应新增 `plan` 块 {plan_id,plan_name,strategy,strategy_label}|null；alternatives 元素加 tier_level(0=不匹配)/tier_name；**blocked 且带方案 → blocked_reason 以「暂无库存：方案「X」各优先级券（第一优先「…」…）均不可用…」开头**；DecisionLog.plan_json 快照 plan_id/plan_name/strategy
- `OrderCreateRequest.plan_id`：fallback 候选按方案策略+层序排序（decision_log_id 的 plan_json.plan_id 优先于 body.plan_id）；**fallback 耗尽 400 detail 以「暂无库存：券自动切换全部失败…」开头**（无方案时同样话术，substring 兼容旧断言）

### §11 增补：方案饮品管理 Tab（2026-09-29）

- 表 `order_plan_drinks`（plan_id FK/sku_id/spu_id/drink_name 快照/face_price 快照；UQ(plan_id,sku_id)；随方案级联删除）
- `OrderPlanRequest.drinks: [{spu_id, sku_id, drink_name, face_price}]`（PUT/POST 全量替换，clear+flush 模式，sku 去重）
- `GET /decision/plan-drinks/search?keyword=&limit=`：菜单库 menu_goods_cache 按 SPU 名 LIKE → 展开 sku_index 为行（含 spec_desc/price/store_no），按 sku_id 去重；keyword 必填（空 422）；decision:manage
- **白名单语义**：方案 drinks 非空时，decide 指定该方案且 sku 不在关联内 → 422「方案「X」未关联此饮品…」；空 = 不限
- `_plan_detail` 含 drinks；前端编辑弹窗三 Tab（基础信息/优先级层级/饮品管理），饮品 Tab = 防抖 300ms 实时搜索 + 行多选 + 批量加入 + 已关联列表（已关联行复选禁用+状态标签）
