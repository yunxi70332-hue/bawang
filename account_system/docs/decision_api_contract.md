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

### §12 套餐配置 × 下单方案 同类功能合并（2026-09-29）

两模块核心业务语义不变（套餐=接单范围+阈值覆盖；方案=执行策略+选券层级），合并的是同类型的重复功能组件：

- **券规则判定唯一实现**：`services/decision.rule_satisfied(rule, record, face=None)` ——四类 match_type 判定 + face_value 面额校验的单条规则语义，`resolve_cost` / `classify_category` / `apply_priority_tiers` / decide ⑥ 的套餐 item 券规则过滤四调用点统一收敛（原 routers/decision._item_rule_hit 重复实现删除）。链式调用方（成本规则链/子类链）自行跳过空 match_value 规则以保持「空=不命中继续走链」；rule_satisfied 本体语义为「该规则对这张券放行」（rule=None / match_value 空 → True）
- **套餐 item 券规则结构对齐方案层级**：normal/premium_coupon_rule JSON 统一为 `{match_type, match_value, face_value?}`（face_value 面额校验非空时须等于券面额；旧两键数据兼容，缺 face_value=不限）；前端编辑字段组三态共用组件
- **SKU 搜索共享端点**：`GET /decision/sku-search?keyword=&limit=`（原 plan-drinks/search 逻辑原样，路径通用化；旧路径保留双路由兼容）；套餐商品清单与方案饮品管理统一走「本地菜单库模糊搜索 + 多选 + 批量加入」交互（套餐原城市→门店→SPU→SKU 三级在线菜单级联选品废弃，在线浏览仍在菜单库页面）
- **方案 CRUD 规范对齐套餐**：`POST /decision/order-plans/{id}/toggle-enabled`（仅翻转 enabled，层级/饮品原样保留——替代原前端全量 PUT 翻转，其 payload 缺 drink_info 必填字段必 422 且漏 drinks 会清空关联）；create/update/delete 补 audit+oplog 打标；列表 `GET /decision/order-plans?keyword=&enabled=&page=&page_size=`（响应加 total，缺省 page_size=100 保证工作台全量）
- **前端共享资产**：`constants/decision.js`（MATCH_TYPES 四枚举 + PLAN_STRATEGIES，三页唯一出处）、`components/MenuSkuPicker.vue`（选品器）、`components/CouponRuleFields.vue`（规则字段组）；套餐与方案编辑弹窗均 Tab 化同构（基础信息 / 明细管理），校验失败自动切回基础 Tab

## 13. 套餐并入下单方案体系（2026-09-29；绑定子功能当日按用户决策移除）

套餐配置功能的管理入口整体整合至下单方案系统，独立页面下线；**方案与套餐保持正交**（套餐管接单范围、方案管选券策略，decide 各自独立生效）：

- **入口收编**：前端菜单/路由移除「套餐配置」（/decision/packets 下线，直达 URL 由通配符重定向仪表盘）；PacketConfigView.vue 删除，套餐 CRUD（列表/搜索/开放开关/新增/编辑/删除 + 商品与券规则弹窗）整体迁入下单方案页「套餐库」页签。套餐 REST 端点（/packets CRUD + toggle-open）原样保留——仅前端入口合并，API 兼容不变
- ~~方案绑定套餐（order_plans.packet_id，decide 绑定优先/冲突 422/删除防悬挂）~~ **已移除（2026-09-29 用户决策）**：绑定后套餐商品白名单与方案饮品白名单为 AND 关系，空交集会使方案永久接不了单（两道 422 各说各话、保存时无交集校验的配置陷阱），且绑定提示语「以套餐为准」与饮品白名单独立生效的真实语义有偏差。移除后：OrderPlan 无 packet_id 字段（旧库列残留无害）、decide 恢复「body.packet_id 指定 / 自动匹配」二元逻辑、套餐恢复自由删除、方案编辑弹窗与列表无绑定相关 UI。如未来重引入，须先解决两白名单的交集校验与语义主从问题

## 14. 方案支付金额上限 max_pay_amount（2026-09-29，fail-closed 资金风控）

**方案级支付金额上限**：使用方案下单时，本单支付端实付金额（差额实付口径）不得超过 `max_pay_amount`；阈值未配置或配置非法时**默认拒绝交易**（fail-closed，防自动支付超额）。plan_id=0（自动模式）不受影响。

- **表列**：`order_plans.max_pay_amount VARCHAR(32) NOT NULL DEFAULT ''`（空串=未配置；seed._COLUMN_MIGRATIONS 存量库自动补列）
- **Schema**：`OrderPlanRequest.max_pay_amount` 必填，`^\d+(\.\d{1,2})?$` 非负金额（"15"/"15.7"/"15.70"），validator 中文文案「支付金额上限须为非负金额（如 15.70）」；detail/列表透出同名字段
- **纯函数**（services/decision.py，判定链文档同步）：
  - `parse_pay_threshold(raw) -> {"status": valid|empty|invalid, "value": Decimal|None}`（空/空白→empty；非数字/负数/NaN/Inf→invalid）
  - `check_pay_threshold(pay_amount, threshold_raw) -> (bool, str)`：empty/invalid → (False, "方案支付金额阈值未配置或配置非法，已默认拒绝交易")；实付>阈值 → (False, "当前支付金额超过方案限制：需支付 X.XX 元，超过方案阈值 Y.YY 元")；边界相等放行（<=）
- **decide 集成**（routers/decision.py orders_decide）：
  - plan 校验区（探针/deep 之前）解析阈值，非 valid → 422「方案「X」支付金额阈值未配置或配置非法，已默认拒绝交易（请在方案编辑中配置支付金额上限）」+ oplog ERROR
  - `_rank_with_pay_cap`：排序（初始+deep 重排两处）后过滤 `pay_cost ≤ 阈值` 的候选（首选+备选一并剔除）；threshold_json 增 `plan_max_pay_amount`
  - 原价兜底（allow_full_price）：原价 total 超阈值 → blocked「当前支付金额超过方案限制：原价单需支付 X 元，超过方案「X」阈值 Y 元」+ oplog WARN
  - 候选全被上限滤光 → blocked「当前支付金额超过方案限制：本单最优候选仍需支付 X 元，超过方案「X」阈值 Y 元」（区别于利润阈值的「暂无库存」话术）+ oplog WARN
  - plan_json 快照增 `max_pay_amount`
- **订单创建/支付确认两道校验**（routers/orders.py `_enforce_plan_pay_threshold`，create/pay 两 stage）：
  - create：选券复跑完成后、createOrder 之前，校验 `settle.buyer_real_price`（服务端确认差额实付，与 partial 落库 pay_amount 同源）
  - pay：`/{order_no}/pay` continue_pay 重铸支付串后、下发/autopay 执行前，校验 `link.total_amount`（支付宝侧实付）；方案来源=成单回填的 DecisionLog（`_plan_id_of_order`，无绑定=0 放行）
  - create 环节方案来源 `_resolve_create_plan_id`：decision_log_id 的 plan_json.plan_id 优先，回落 body.plan_id
  - 语义：plan_id≤0 放行；方案不存在 422（+plan_not_found WARN）；未配置/非法 422 + oplog ERROR；超限 422「方案「X」当前支付金额超过方案限制：…」+ oplog WARN（params 带 plan_id/plan_name/pay_amount/threshold/stage/result）
- **日志**（需求§4）：oplog action=`decision.plan_pay_threshold`（触发时间=created_at、方案ID/名称、实际金额 pay_amount、阈值 threshold、环节 stage=decide|create|pay、结果 result=blocked|rejected_config）；decide 每次评估的 DecisionLog 行经 threshold_json.plan_max_pay_amount / plan_json.max_pay_amount 留档
- **已知旁路**（评估记录）：switch_order_to_full_price 原价重下产生无 DecisionLog 绑定的新单 → pay 环节查不到方案放行；仅 body.plan_id（无 decision_log_id）下的单同理。如需封死，需 OrderRecord 增加 plan_id 列（本次未做）
- **前端**：方案编辑弹窗基础信息 Tab 必填「支付金额上限(元)」（正则校验同 schema）+ 安全提示；方案列表「金额上限」列（空=红 tag「未配置」）；工作台方案下拉/结果信息展示上限
- **测试**：test_decision_offline test_28/29（CRUD+decide 六分支）、test_orders_offline +4（create 超限/未配置/自动模式/pay 环节）

## 15. 券类型聚合下拉 coupon-types（2026-09-29，方案优先级层级「优惠券绑定」）

**背景**：优惠券全量查询（功能4）把查询结果落库 `coupon_records`（券档案）；下单方案的优先级层级原只能手填匹配规则。本节新增「优惠券绑定」下拉，从券档案实时聚合券类型供选择绑定。

- **端点**：`GET /api/ops/decision/coupon-types?keyword=&page=1&page_size=20`（权限 decision:manage；routers/decision.py）
  - 语义：coupon_records 按 `template_name`（券类型）GROUP BY 实时聚合，**现查无缓存**——与功能4 全量查询/decide 扫描的最新落库记录天然同步
  - item 字段：template_name / benefit_text / amount / usable_scenes / total_count（档案张数）/ available_count（bucket∈{effective,settle_available} 张数）/ account_count（DISTINCT 持有账号数）/ use_start_time(min) / use_end_time(max) / updated_at
  - 过滤：keyword 对 template_name LIKE；空模板名行排除。排序：available_count desc → total_count desc → template_name。分页 page/page_size（≤100）
- **绑定语义**（前端 CouponTypeBindSelect.vue + OrderPlanView.vue 层级行）：
  - 下拉 = el-select 远程搜索（输入 300ms 防抖拉首页）+ 展开即刷新首页（visible-change，与库同步）+ 底部「加载更多」逐页追加（seq 竞态防护）
  - 选项展示：券类型名 + 权益 · 可用 x/y 张 · N 账号 · 有效期至
  - 选中回填层级行：match_type=`template_exact`（与 rule_satisfied 全等语义对齐）+ match_value=完整模板名 + face_value=档案面额（折扣/兑换券无面额不动）；层名留空时以 benefit_text 兜底
  - 镜像显示：层级行已有名称类规则（template_exact/template_contains）时绑定框回显 match_value；正则/券码前缀属手填规则不回显；清空绑定仅清 match_value/face_value
- **兼容**：不改库表、不改 decide 选券判定（绑定产物即普通层级规则）；与 §11 优先级层级完全同构
- **测试**：test_decision_offline test_30（聚合口径/keyword/分页/落库即时同步/空模板名排除/绑定往返/viewer 403）

## 16. 券剩余有效期「剩余 N 天」（2026-09-29，全链路服务端权威计算）

**口径（services/decision.coupon_validity，前后端唯一出处）**：有效期窗口为毫秒 epoch（绝对时刻），经 fromtimestamp 转服务器本地时区后按**自然日差**计——days_remaining = 截止日本地日期 − 今日本地日期（同日任何时刻查询稳定；避免毫秒差除法的日内跳变与字符串时区歧义）。

- **状态机**：pending 未生效（start>今天）/ active / expiring 临期（0≤days≤3，含今日到期 days=0）/ expired（days<0）/ unknown 无截止标注（长期，days=None）
- **落库设计**：不新增冗余列——coupon_records 已有 created_at（落库时间）+ use_start_time/use_end_time（有效期窗口两端点毫秒）；剩余天数为纯派生值，服务端实时计算不落库（与窗口字段保持单一事实源）
- **下发面**（服务端计算、前端只渲染）：
  - GET /coupons/search：每行 days_remaining/validity_status + stats.expiring/expired（命中口径）
  - GET /accounts/{id}/coupons/records：每行同名字段
  - POST /coupons/sync-all 与单账号 POST /accounts/{id}/coupons：响应 coupons 逐张附带（ops._validity_fields，fail-soft）
  - GET /decision/coupon-types：days_remaining = **最早到期的可用桶券**（MIN 忽略 NULL；历史桶更早不计；无可用=None）
- **前端**：components/CouponValidityTag.vue 统一渲染——已过期=红 / 今日到期与剩≤3天=橙（醒目）/ 未生效=蓝 / 剩>3天=灰字 / 无截止=「长期」；优惠券查询页明细+档案库两表「有效期」列（标签+日期区间两行）与统计行「临期/已过期」；方案绑定下拉元信息以「剩 N 天」替换「至 日期」（≤3 天整段橙色强调）
- **桶标覆盖修复（同日根因）**：wire historical-list 实测含全部券（可用40/历史40 完全重叠），_persist_coupon_records 原顺序先可用后历史，同券码 upsert 后写覆盖 → 40 张全 historical。修复：按「历史先写、可用后写」排序，同码两列表并见时 effective 胜出（routers/ops.py；仅历史列表的券不受影响）
- **测试**：test_decision_offline test_31（纯函数矩阵：当日/3天/4天/过期/未生效/无截止/23:30→次日00:30 日界 + coupon-types 最早到期口径）、test_coupons_offline test_coupon_days_fields_and_bucket_overlap（sync-all/search 字段 + 天数按同口径现场计算抗日期漂移 + 同码重叠桶标回归）

## 17. 券生命周期自动收口（2026-09-30，核销迁移历史桶 + 登录自动入库）

**背景（两处手动依赖收口）**：成单仅预记使用痕迹（last_used_at/last_order_no），已支付核销后 bucket 原地不动 → 可用数不随真实消耗扣减，须手动 F4 才迁移历史桶；新账号登录后券档案为空，亦须手动查询入库。

- **核销自动迁移（bucket→historical）**：`services/order_reconcile.confirm_coupon_usage(db, coupon_code, order_no, operator)`（与 rollback_coupon_usage 对称成对），四个触发点：
  1. pay-watcher `_handle_paid`（services/payment_events.py，CAS 胜者；operator=pay-watcher）
  2. H5 收银台探针 `_probe_remote` paid 分支（routers/payportal.py，另一 CAS 胜者；operator=pay-portal）
  3. 订单校准 paid 分支（order_reconcile.reconcile_order，st∈{3,6}；60s 线程/手动端点/create_core 内联三入口共用）
  4. create_core 零元单分支（成单即核销，无支付腿不等确认通道）
- **不变式一（无双算）**：confirm 只迁移桶位、保留使用痕迹，**不写 CouponUsageLog**——success 使用日志成单时已预记，抵扣/使用统计不重复计数
- **不变式二（手动 F4 仍为校准权威）**：后续 F4 查询 upsert 覆盖本地桶标（如退款后茶姬重显「可用」→ effective 胜出回迁，自愈）
- **两链互斥幂等**：cancelled→rollback（bucket→effective + 清痕迹 + rolled_back 日志，语义不变）；paid→confirm（bucket→historical + 留痕迹）；订单不可能既取消又支付，watcher/探针/校准并发双发时各自幂等（桶位未变则跳过 oplog）
- **登录自动同步券入库**：POST /api/accounts/{id}/login 成功后内联 best-effort 拉取 F4 两列表全量落库（复用 ops._persist_coupon_records，可用后写胜出口径同 §16）：
  - 响应新增 `coupons_synced: {total, effective, historical} | null`（失败为 null，绝不影响登录结果）；audit action=account.login 附 coupons_synced 计数
  - 开关 `CHAGEE_LOGIN_COUPON_SYNC`（默认 1；0 关闭）；失败 WARN 留痕 action=coupon.login_sync
  - 前端 AccountsView 登录成功 toast 追加「已同步 N 张券入库」
- **SSE/仪表盘零改动**：dashboard_push 5s 指纹推送天然联动——核销后 coupons_effective−1/historical+1、登录同步后 coupons_total 增加，均自动推送
- **测试**：test_coupon_lifecycle_offline（confirm 单元/幂等、三支付通道迁移、零元单成单迁移、登录同步成功/失败容错、指纹联动）；test_reconcile_offline test_timeout_paid_keeps_coupon_used 增补 bucket 断言；test_orders_offline 零元单增补 bucket 断言

## 18. 券使用日志状态机（2026-09-30，pending 预记 + 实时流转 + 历史回填）

**背景（分类失真根因）**：旧口径差额单成单即预记 `result="success"`（乐观记法），订单未支付即取消后 success 行残留 → 「使用成功 N 笔/累计抵扣」把未核销交易计入（回滚另补 rolled_back 行造成双行 + 补偿式扣减）。

**状态定义（CouponUsageLog.result 五态）**：
- `pending` 待支付（使用中）——差额单成单预记；券已占用但支付未完成
- `success` 使用成功（已核销）——零元单成单即达（born status 3，无支付腿）；差额单经支付确认通道迁移到达
- `rolled_back` 已回滚（取消退券）——订单取消（超时 autoCancel / 手动取消 / 原价切换）时迁移到达
- `rejected` / `failed` 终态——五重校验拒绝 / createOrder 异常，无订单关联，不变

**流转规则（单行生命周期：一次使用事件 = 一行日志，原地迁移不加行）**：
- `pending → success`：支付确认四通道（§17 watcher / H5 探针 / 校准 / ——零元单不经此态）触发 `confirm_coupon_usage`：bucket→historical + 日志原地迁移
- `pending → rolled_back`（或遗留 success → rolled_back）：`rollback_coupon_usage`：bucket→effective + 清使用痕迹 + 日志原地迁移（金额快照保留、operator 保持原下单人）
- 两链互斥幂等：已终态行再触发只复位档案字段不加行不重复迁移

**状态变更日志（state_history JSON 列，coupon_usage_logs 表）**：每次流转追加 `{at, from, to, by, reason}`（by=触发方：pay-watcher/pay-portal/system/操作人/seed-migration）；GET /coupon-usage-logs 每行下发 `state_history` 数组，前端结果标签带 `*` 悬停可见全轨迹。

**统计口径修正**：
- GET /coupon-usage-logs stats：五类计数（新增 pending）；**累计抵扣只计 result=success**（已核销），pending/rolled_back 不计；旧「success − rolled_back 补偿扣减」废止（单行生命周期下无双重计数）
- result 过滤参数增 `pending`；_RESULT_LABELS 五态中文
- dashboard_stats：使用趋势/卡片增 pending 维度（coupon_pending_pay）；仪表盘抵扣口径随 result 语义自动修正

**历史数据回填（seed._migrate_usage_log_lifecycle，启动时幂等执行）**：
① 旧双行（同券同单 success + rolled_back）→ 合并为单行 rolled_back（金额快照保留、历史并入 state_history、删除重复行）
② 残留 success 行按订单实况校正：order.status=7 → rolled_back；=1 → pending；订单缺失或已支付(3/6) 不动（零元单 born 3 合法）

**测试**：test_coupon_lifecycle_offline test_08/09/10（差额单 pending→success/rolled_back 全链路 + 统计口径 + 回填合并/校正/幂等）；test_reconcile_offline（stats 新口径 + 种子行改 pending）；test_orders_offline（rate 券差额单断言改 pending）；test_payportal_offline（原地迁移 + state_history actor 断言）
