# 下单决策系统设计方案（2026-09-28）

> 版本：v1 ｜ 分支：feature/decision-system ｜ 契约：`docs/decision_api_contract.md`（冻结版）
> 范围：套餐圈定 + 券成本核算 + 阈值判定 + decide 逐单决策 + 成单回填 + 盈利报表 + 前端三块 UI
> 实施波次：波1（数据层/前端骨架）→ 波2（后端 API/前端集成）→ 波3（本文档 + `server/test_decision_offline.py`）

---

## 1. 业务背景：瑞幸模式 → 霸王茶姬映射

参考业界成熟的「瑞幸代下单」运营模式：客户在第三方渠道下单，商家用**囤积的低价券**在品牌
App 内完成下单，赚取「客户支付价 − 券采购成本 − 差额实付 − 杂费」的利润差。该模式的核心
不是下单本身（F5 已具备），而是**每一单接不接、用哪张券、在哪个账号下单的量化决策**。

| 瑞幸参考模式环节 | 本系统（霸王茶姬）实现 | 落点 |
|---|---|---|
| 客户在第三方平台拍下链接（含 linkId/规格/支付价） | 客户平台 linkId = 茶姬 skuId，文案规格（大杯/少冰/半糖）经本地菜单规格库解析 | `services/menu_spec.resolve_by_sku` / `resolve_spec_texts` |
| 商家囤券（9.9 购 20 元券等渠道价） | 券采购成本规则库：模板/面额/券码前缀 → 实际采购价；未命中按面额×系数保守计 | `voucher_cost_rules` 表 + `resolve_cost` |
| 接单前心算利润（售价−成本−差额） | decide 端点折算 cost_breakdown（收入/总额/抵扣/券成本/差额/杂费→利润/利润率） | `evaluate_cost` |
| 利润太低不接 / 换券 | 阈值判定 pass/blocked（全局 + 套餐级最低利润、可选最低利润率） | `check_threshold` |
| 可售商品与价格区间约定（如 9.9 元专区） | 套餐（Packet）：客户支付价区间 + 可用时段 + 商品圈定 + 套餐级利润下限 | `packet_configs` / `packet_items` |
| 多账号囤券轮换使用 | 候选券跨在线账号初筛 + 成本升序排序，推荐账号与备选票 | `rank_candidates` |
| 日终对账（赚了多少） | 成单回填 decision_logs + 盈利报表（按套餐/按券模板聚合）+ 仪表盘盈利卡 | `_backfill_decision_log` / `profit-report` |

与瑞幸场景的差异化处理：茶姬券型更杂（面额券/折扣率券/兑换券/多次卡），故券型分型
（`classify_coupon`）与**折扣券估算语义**（服务端才算得准，本地仅估）是本系统相对参考模式的
主要增量。

---

## 2. 数据模型（models.py 四表，契约 §1）

金额列全 String（与全仓 `Mapped[String(16)]` 惯例一致），全 Decimal 计算、两位小数
ROUND_HALF_UP 出入；margin 为百分比一位小数字符串（不带 %）。

```
packet_configs ──1:N── packet_items          套餐与商品行（uq_packet_item: packet_id+sku_id）
voucher_cost_rules                          券成本规则（独立，无外键）
decision_logs                               决策流水（每次 decide 落一条；成单后回填）
coupon_records（既有）                       券档案：decide/扫描的候选来源与成本盘点对象
```

| 表 | 关键列 | 说明 |
|---|---|---|
| `packet_configs` | `name`(unique) `open_flag` `min/max_order_amount`(客户支付价区间，max=0 不限) `available_start/end`(HH:MM:SS，空=不限) `min_profit`(空=用全局) | 套餐=可接单范围。items 为空 = 全品类 |
| `packet_items` | `spu_id/sku_id` `face_price`(选品回填) `is_premium` `normal/premium_coupon_rule`(JSON，null=不限券) | 同 sku 仅一行；溢价商品走 premium 规则选券 |
| `voucher_cost_rules` | `match_type`(template_exact/contains/benefit_regex/coupon_prefix) `match_value` `face_value`(面额校验) `cost_price`(★采购成本) `priority`(小者优先) `enabled` | 券 → 采购成本的映射链 |
| `decision_logs` | `order_no`(空=纯评估) `revenue/total_trade_price/voucher_cost/pay_cost/overhead/total_cost/profit/margin` `verdict`(pass/blocked) `threshold_json` `plan_json` `deduction_actual/pay_actual`(成单回填) | 全量留痕：评估快照 + 成单事实对账 |

---

## 3. 券码三段流程：扫描识别 → 匹配 → 应用

### 3.1 扫描识别（POST /api/ops/decision/scan）

复用 F4 `ops.coupons_sync_all` 的全账号遍历语义（status≠disabled 且 token≠""，
`account_ids` 可圈定；单账号凭证失效/协议异常不中断整体）：拉取 effective/historical 两桶
券入 `coupon_records`（券ID↔账号 token 指纹映射），随后对全库 effective/settle_available
券逐张 `resolve_cost` 成本盘点，返回 `{accounts_scanned, coupons_total, coupons_with_cost,
coupons_unknown_cost, total_face, total_cost_estimate}`——**有多少券能算出成本、库存总面额
与总成本预估**，即「囤货盘点」。

### 3.2 匹配（resolve_cost 规则链 + classify_coupon 分型）

每次 decide / 券库存查询时逐券计算：

1. **分型** `classify_coupon(benefitText, benefit2Text, templateName)`：兑换/换购关键词 ＞
   折扣（"X折" → rate=X/10，含 "7.5折"）＞ 面额（"N元"/"满M减N"→face=N）＞ unknown
   （模板名保守解析面额）；
2. **成本** `resolve_cost`：enabled 规则按 priority 升序逐条尝试四类命中
   （template_exact = 模板名全等 / template_contains ∈ 模板名 / benefit_regex re.search 于
   模板名+benefit 拼接串 / coupon_prefix 券码前缀）；`face_value` 非空须等于券面额；
   非法 regex 等异常安全吞掉视为不命中；全部未命中 → **fallback = 面额 ×
   cost_fallback_ratio**（缺省 1.0 保守计），source="fallback"。

### 3.3 应用（decide 候选初筛 → 排序 → 选券下单）

候选 = 在线账号 + 未使用（last_order_no=""）+ effective/settle_available 两桶 +
本地静态初筛（canDiscount 标记 + 有效期毫秒窗口）+ 门槛初筛（thresholdTips 数字高于估算
总额即剔除）→ 套餐 item 券规则过滤 → `rank_candidates` 排序取 top1；应用推荐由工作台
「应用推荐」把账号/券/`decision_log_id` 带回既有 F5 下单流。

---

## 4. 成本核算公式（evaluate_cost）

```
pay_cost   = max(total_trade_price − deduction, 0)      # 差额实付（抵扣不足部分）
total_cost = voucher_cost + pay_cost + overhead         # 总成本 = 券采购成本 + 差额 + 杂费
profit     = revenue − total_cost                       # revenue = 客户支付价
margin     = profit / revenue × 100（一位小数；revenue ≤ 0 时为空）
```

**预计抵扣 `estimate_deduction(kind, face, rate, total)` 与 estimated 标志**：

| 券型 | 抵扣 | estimated 语义 |
|---|---|---|
| face（面额券） | min(face, total) | False —— 面额抵扣是确定值 |
| discount（折扣率券） | total × (1 − rate)，负值钳 0 | True —— 茶姬按券型服务端计算（如会员折上折），本地只能估 |
| exchange（兑换/换购） | total 全额 | True —— 0 元语义按全额覆盖估算 |
| unknown 且有面额 | min(face, total) | True —— 券型不明，保守按面额估 |
| 其余（无面额可算） | 0 | True |

`deduction_estimated=True` 的行，成单后以 `deduction_actual`（settle 复跑服务端回填）对账
估算偏差；`price_source=menu`（本地菜单价×数量）或 `settle`（deep=true 真实试算探针）。

---

## 5. 阈值判定规则（check_threshold）

- `profit < min_profit` → blocked（原因：利润 X 元低于最低利润 Y 元）；**等于阈值 pass**；
- `margin < min_margin` → blocked（仅当双方非空；margin 算不出时跳过该项）；
- 阈值来源：**套餐级 min_profit 非空时覆盖全局**（threshold_json.source=packet），
  min_margin 恒取全局；`data/decision_config.json` 的 min_profit/min_margin 空 = 不启用该项；
- 无 pass 候选时：`allow_full_price=true` 允许按无券原价单再判一次阈值；否则 blocked
  （有候选给「最佳券候选未过阈值 + 差距参考明细」，无候选给「无可用券候选」）。

---

## 6. decide 决策流程（POST /api/ops/orders/decide，权限 feature:order）

```
① 商品解析   resolve_by_sku（本地菜单规格库，未命中 422）+ 文案规格 resolve_spec_texts
② 收入       revenue = customer_price（必填合法金额）
③ 套餐命中   match_packets（价格区间含边界 + 时段含跨零点 + 商品圈定）；
             packet_id 指定则校验其在命中集内（否则 422），缺省取命中首个
④ 总额估算   total = menu 价 × quantity（price_source=menu）
⑤ 券候选     在线账号 + 未用 + 两桶 + 有效期/门槛初筛 → 分型 + 算成本
⑥ 券规则     套餐 item 的 normal/premium_coupon_rule 过滤（is_premium 商品走 premium 规则）
⑦ 排序       rank_candidates：逐候选折算 breakdown + 阈值 → pass 者 total_cost 升序、
             同成本面额大者优先
⑧ deep 探针  deep=true 时对 top1 候选账号真实 settle_direct(no_recommend) 取服务端总额
             （price_source=settle），失败降级 menu 估值不中断
⑨ 判定       top1 候选 pass / allow_full_price 原价单 / blocked（附最佳候选差距参考）
⑩ 落库       DecisionLog（order_no 空，blocked 也写）→ 响应带 decision_log_id 供成单回填
```

响应含：verdict/blocked_reason、packet、item、account_id/account_label、coupon、
cost_breakdown（§4 全字段 + price_source/deduction_estimated/cost_source/coupon_kind）、
threshold、alternatives（备选票 ≤5）、settle_prefill（对齐 OrderSettleRequest 的直发预填）。

---

## 7. 成单回填与盈利报表口径

**成单回填（orders.py `_backfill_decision_log`，契约 §6）**：`order_create` 成功落库处，
请求带 `decision_log_id` 时把该行回填 `order_no / account_id / coupon_code（原行空时）/
deduction_actual（选券复跑的服务端确认抵扣）/ pay_actual（实付）`，plan_json 补 order_no。
找不到 id 或已被其他单占用仅 WARN 留痕不报错；**不传该字段（缺省 0）行为与决策系统引入前
完全一致**（向后兼容）。

**盈利报表（GET /api/ops/decision/profit-report）口径**：

- **成单行** = order_no 非空 **且** verdict=pass —— revenue/cost/profit/margin_avg 仅按该
  集合累计（未成单的纯评估行不计金额，避免报价刷高利润）；
- `blocked_count` 统计窗口内全部 blocked 评估（拦截留痕）；
- `by_packet` 按成单行 packet_id 聚合（订单数/收入/成本/利润）；
- `by_coupon_template` 按 coupon_code join coupon_records.template_name 聚合
  （uses / voucher_cost / pay_cost；无券成单行不进该归组）；
- 时间参数支持 `YYYY-MM-DD`（日界 00:00:00~23:59:59）与 `YYYY-MM-DD HH:MM:SS` 双格式。

**仪表盘盈利卡（dashboard_stats._collect_profit_domain）**：近 7 日同口径聚合
（orders/revenue_total/cost_total/profit_total/margin_avg/blocked_count），margin 取
ROUND_HALF_UP 与 profit-report 完全对齐；单次范围查询，任何异常（表未建/值非法）返回零值
不抛错（随 SSE 周期推送，绝不阻断 stats 主流程）。

---

## 8. 前端功能清单

| UI | 路由/位置 | 权限 | 功能 |
|---|---|---|---|
| 套餐配置页 PacketConfigView | `/decision/packets`，菜单「下单决策/套餐配置」 | decision:manage | 搜索（名称/开放状态）+ 表格（区间/开放开关/商品数/最低利润/更新时间）+ 新增编辑弹窗（基础字段 + 内嵌商品子表格：菜单选品回填名称面价、溢价标记、券规则下拉） |
| 券成本页 VoucherCostView | `/decision/costs`，菜单「下单决策/券成本与阈值」 | decision:manage | Tab1 成本规则表格 + 弹窗 CRUD + JSON 文本域批量导入 + 每规则启用开关；Tab2 全局决策配置表单（min_profit/min_margin/overhead/cost_fallback_ratio） |
| 工作台决策面板（OrderWorkbenchView 步骤②后） | 既有下单工作台内 | feature:order | customer_price 输入 + 评估按钮 → cost_breakdown 明细与阈值灯；pass 可「应用推荐」（切账号/选券/提交带 decision_log_id）；blocked 显示原因（不硬断旧流程，不加决策参数时照常下单） |
| 仪表盘盈利卡（DashboardView） | 既有仪表盘 | 登录即可 | profit_total / margin_avg / blocked_count（近 7 日，profitReport/stats 双通道同源） |

---

## 9. 配置示例

**data/decision_config.json**（缺省自动兜底，save 时落盘）：

```json
{
  "min_profit": "2.00",
  "min_margin": "",
  "overhead": "0",
  "cost_fallback_ratio": "1.0"
}
```

**成本规则批量导入（POST /api/ops/decision/cost-rules/import）body 示例**：

```json
{
  "rules": [
    {"name": "20元代金券-渠道A", "match_type": "template_contains", "match_value": "代金券",
     "face_value": "20", "cost_price": "8.00", "priority": 10, "enabled": true, "note": "渠道A采购价"},
    {"name": "7折券-通用", "match_type": "benefit_regex", "match_value": "(\\d+(\\.\\d+)?)折",
     "face_value": "", "cost_price": "3.00", "priority": 20, "enabled": true, "note": ""},
    {"name": "福利券DTB前缀", "match_type": "coupon_prefix", "match_value": "DTB",
     "face_value": "", "cost_price": "9.50", "priority": 30, "enabled": true, "note": ""},
    {"name": "生日兑换券-精确", "match_type": "template_exact", "match_value": "霸王茶姬生日兑换券",
     "face_value": "", "cost_price": "5.00", "priority": 5, "enabled": true, "note": ""}
  ]
}
```

返回 `{imported, skipped, errors}`：name 与库内重复跳过；非法条目（如 match_type 拼错）进
errors 不中断。

---

## 10. 运营操作指引

1. **配套餐**（套餐配置页）：新增套餐 → 填客户支付价区间（如 5~30）与可用时段（可空）→
   内嵌表格从菜单选品（自动回填商品名/面价），需溢价券的商品勾「溢价」并配 premium 规则 →
   套餐级最低利润留空即用全局 → 保存。用「开放」开关临时下架。
2. **录成本**（券成本页 Tab1）：按采购渠道逐条新增规则（优先级小的先匹配；面额校验列可
   区分同模板不同面额）；渠道批发可多选 JSON 粘贴批量导入；改价走编辑，停用不停删。
3. **设阈值**（券成本页 Tab2）：全局最低利润（每单底线，如 2.00）与可选最低利润率
   （如 10 = 10%，留空不启用）；杂费 overhead 按每单固定成本计（包装/跑腿）；未命中规则的
   券按面额 × cost_fallback_ratio 保守计（宁可高估成本不低估）。
4. **日常扫描**（券成本页）：点「扫描」全账号拉券 + 成本盘点 → 看 coupons_with_cost 占比，
   unknown 占比高说明规则库缺该类券的采购价，回 Tab1 补规则；券库存列表可按可用性/关键字
   筛选，确认囤券水位与归属账号。
5. **接单决策**（工作台）：选完商品 → 输客户实际支付价 → 「评估」→ 绿灯（pass）可
   「应用推荐」一键带账号/券/decision_log_id 下单；红灯（blocked）按原因处置（见
   《用户操作文档》下单决策章节的 blocked 原因解读表）。deep 评估可对 top1 账号真实试算
   取服务端总额（折扣券场景建议开）。
6. **看报表**（仪表盘盈利卡 + profit-report）：成单口径的利润/利润率与 blocked 拦截数，
   按套餐与券模板两个维度复盘哪个套餐/哪类券在赚钱。

---

## 11. 测试与验证

- 离线测试 `server/test_decision_offline.py`（pytest 直跑，wire/快照回放零真实网络）：
  纯函数矩阵（分型/解析/抵扣/成本链/折算/阈值/套餐命中/排序/配置）+ 端点全往返（套餐 CRUD、
  成本规则 CRUD+import、config、券库存、decide pass/blocked/deep/packet 422、盈利报表双
  日期格式、成单回填、viewer 403）+ 仪表盘 profit 域 fail-soft；
- 实施中修复两处决策域缺陷（波3 测试发现）：① PUT /packets items 全量替换在 SQLAlchemy
  单次 flush 内 INSERT 先于 DELETE，新旧行同 (packet_id, sku_id) 撞 uq_packet_item——
  `packet.items.clear()` 后先 `db.flush()` 再重建；② 仪表盘 margin_avg 默认 HALF_EVEN
  舍入（25.65→25.6）与 profit-report 的 ROUND_HALF_UP（→25.7）口径不一致——统一
  ROUND_HALF_UP。

## 券成本子类（2026-09-29 增补）

**定位：子类只做分类，不做成本。** 券资产业务分类层（biz_type：paid 采购付费 / free 活动免费 / bank 银行渠道 / other 其他），按与成本规则相同的四类匹配语义自动归类每张券；成本金额仍由成本规则唯一决定，二者职责解耦、互不重复。

- 数据：`voucher_cost_categories` 新表 + `voucher_cost_rules.category_id`（0=未分类）；归类结果不持久化，全部实时由 `classify_category`（与 resolve_cost 共用 `_match_hit`）派生——库存池/档案库/decide 三处同一口径
- 联动（与优惠券查询模块）：档案库每行带 成本价/成本来源/子类/biz_type，支持子类筛选（`cost_category=-1` 为未分类）；成本子类页「查看券」深链跳转档案库并自动应用筛选；子类页统计（券数/面额/成本合计）仅计 effective+settle_available 两桶当前有效资产，档案库统计含历史全桶——两口径均为设计语义
- 运营：在「券成本与阈值 → 成本子类」维护分类（如 新人礼=free、浦发=bank、代金券=paid）；删除子类自动把挂靠规则归位未分类；详见契约 §9 与用户操作文档 §13
