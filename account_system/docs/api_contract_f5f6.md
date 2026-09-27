# F5/F6 集成 API 契约（v1 — 2026-09-26，四智能体共同输入）

> 权限点已落地 `server/permissions.py`：新增 `feature:order`；`feature:pickup` 描述更新；operator 角色已含 order+pickup。
> 引擎已补 `ChageeTradeApi.waiting_info(store_no, order_no, unique_pos_order_no)`（wire 回放测试通过，全套 32/32 绿）。

## 0. 通用约定

- 所有登录态端点路径前缀：`/api/ops/accounts/{account_id}`，路径参数 `account_id: int`，`order_no: str`
- 认证：现有 JWT Bearer；权限注解：settle/create/continue-pay/cancel/pay → `require_perm("feature:order")`；列表/详情/状态/等待 → `require_perm("feature:pickup")`（operator/admin 均两权限齐备）
- 异常链（与 ops.py 现状一致）：`SessionExpiredError→409（置 account.status=expired+审计）` → `ChageeBridgeError→400` → `OrderHangError→409（detail 引导"先到取餐查询页查单"）` → `ConsistencyError→409（detail 含差异说明）` → `TradeError/ChageeError→502` → 裸 `Exception→502`
- 审计 action：`feature.order_settle` / `feature.order_create` / `feature.order_continue_pay` / `feature.order_cancel` / `feature.order_autopay`（查询类不审计）；target 格式 `{账号label}#{account_id}`；detail 放金额/订单号摘要
- 手机号脱敏用 `schemas.mask_phone`；token/sk 永不下发

## 1. 订单草稿与下单（F5）

### POST /api/ops/accounts/{id}/orders/settle  （feature:order）
请求体（OrderSettleRequest）：
```json
{
  "store_no": "CN11109", "store_name": "浙江杭州上城东站万象汇店",
  "spu_id": "1255931091750727681", "spu_name": "两种大米茶拉朵",
  "sku_id": "1255931091759116...", "sku_name": "中杯",
  "item_sku_id": "1255931091759...", "quantity": 1,
  "spec_list": [{"specId":"...","specOptionId":"..."}],
  "image_url": "https://...", "spu_type": "stand"
}
```
（spec_list/item_sku_id/image_url/spu_type 可选，透传引擎 cart_add 的 target；后端把请求体整理为 OrderTarget 形态 dict）

处理：`bridge.build_client(account)` → `ChageeTradeApi(client)` → `cart_add(target)` → `settle_with_cart(cart, store_no)`（不带券）→ 服务端 draft 缓存（模块级 dict + Lock，key=account_id，value={draft_id, target, cart, settle_base, store_no, store_name, created_at}，TTL 10 分钟，新 settle 覆盖旧 draft）。

响应：
```json
{
  "draft_id": "d-xxxxxxxx", "expires_at": "2026-09-26 12:00:00",
  "preview": {
    "goods": [{"name":"两种大米茶拉朵","quantity":1,"price":"18.00"}],
    "total_trade_price": "18.00", "buyer_real_price": "18.00",
    "available_coupons": [{"couponCode":"...","templateName":"霸王茶姬20元代金券-DN","benefitText":"20元","thresholdTips":"","useStartTime":1790006400000,"useEndTime":1797782399000,"canDiscount":true,"unavailableReason":""}],
    "recommended_coupon": {...}|null, "recommended_deduction": "18.00",
    "estimated_pay": "0.00", "scenario_preview": "zero|partial"
  }
}
```
（estimated_pay = max(total − recommended_deduction, 0)；无推荐券时 estimated_pay=total、scenario_preview 按 total>0 判定）

### POST /api/ops/accounts/{id}/orders/create  （feature:order）
请求体（OrderCreateRequest）：`{"draft_id": "d-xxx", "coupon_code": "1309...|null"}`
处理：
1. 取 draft（不存在/超 10 分钟 → 400 "草稿已过期，请重新试算"）
2. 单次一单：查 OrderRecord 该账号 status==1 → 409 "存在待支付订单，请先处理"
3. 若 coupon_code：从 settle_base.available_coupons 找到该券 entry；用缓存 cart 复跑 `settle_with_cart(cart, store_no, coupon_entry)` 得最终 SettleResult；否则直接用 settle_base
4. `pick_coupon`+`build_discount_row`（或无券 rows=None）→ `create_order(settle, store_no, store_name, rows)`

响应（两分支）：
```json
{"result":"zero","order_no":"2026...","status":3,"status_label":"制作中","pickup_no":"TA0001","pay_amount":"0","coupon_code":"1309..."}
```
```json
{"result":"partial","order_no":"2026...","pay_no":"CHP...","out_trade_no":"...","total_amount":"10.00",
 "expire_at":"2026-09-26 17:27:33","pay_window_seconds":600,
 "order_str":"alipay_sdk=...（完整支付串，供人工/自动支付用）","h5_url":"https://openapi.alipay.com/...|null",
 "note":"支付宝侧扣款不在纯协议范围：人工模式请用手机完成支付；自动模式为实验性"}
```
OrderOutcome → 字段映射；PayLink → 字段映射（h5_url 可空）。OrderRecord 落库（见 §3）+ 审计。

### POST /api/ops/accounts/{id}/orders/{order_no}/pay  （feature:order）
请求体（PayModeRequest）：`{"mode": "manual"|"auto"}`
- manual：返回该单最新 PayLink（`continue_pay` 重铸）+ 人工支付指引文案
- auto：调用 `scripts/alipay_autopay.autopay(...)` 接缝（实验性；模块缺失/未配置 → 501 + 配置说明）
审计 `feature.order_autopay`（仅 auto）。

### POST /api/ops/accounts/{id}/orders/{order_no}/continue-pay  （feature:order）
返回新 PayLink 字段（同 create partial 分支）。

### POST /api/ops/accounts/{id}/orders/{order_no}/cancel  （feature:order，实验性）
引擎 `cancel(order_no)`（无 wire 样本，响应标注 `"experimental": true`）。

## 2. 订单查询（F6）

### GET /api/ops/accounts/{id}/orders?tab=today|history&page=1&page_size=10  （feature:pickup）
处理：`order_list(tab, page, size)` → 行抽取 + OrderRecord upsert。
响应：`{"total": n, "items": [{"order_no","order_status":3,"status_label":"制作中","pickup_no","pay_amount","total_amount","pay_type_text","store_no","store_name","order_time","unique_pos_order_no","can_waiting":true}]}`

### GET /api/ops/accounts/{id}/orders/{order_no}  （feature:pickup）
`order_detail(order_no)` → `_outcome` 映射 + orderItems/orderPromotions/orderTime/payTime/paymentExpiryTimestamp（待支付态）+ upsert。
响应：`{"order_no","status","status_label","pay_amount","total_amount","pay_type_text","pickup_no","unique_pos_order_no","store_no","store_name","order_time","pay_time","items":[...],"promotions":[{"promotionId","promotionName","discountAmount"}],"payment_expiry_ts":null|1700000000000}`

### GET /api/ops/accounts/{id}/orders/{order_no}/status  （feature:pickup）
`{"status": 3, "status_label": "制作中"}`（getOrderStatus 轻探针，data 裸 int）

### GET /api/ops/accounts/{id}/orders/{order_no}/waiting  （feature:pickup）
处理：store_no/unique_pos_order_no 优先取 OrderRecord，缺失则先 `order_detail` 补（并 upsert）→ `waiting_info(...)`。
响应：`{"waiting_cups": 0, "waiting_time": 300, "queue_limit": 61}`

状态映射（前端 utils/format.js 同步）：1 待支付 / 3 制作中 / 6 已完成 / 7 已取消（服务端有 orderStatusText 时优先用服务端文案）。

## 3. OrderRecord 表（models.py 新增）

```
id PK / account_id FK chagee_accounts.id / order_no String(64) unique index
store_no String(32) / store_name String(128) / goods_desc String(255)   # 商品快照（spu_name xN + 规格）
quantity int / coupon_code String(64) / total_amount String(16) / pay_amount String(16)
scenario String(8)  # zero|partial / status int default 1 / status_label String(16)
pickup_no String(16) / unique_pos_order_no String(64)
out_trade_no String(64) / pay_deadline DateTime|None
created_at / updated_at（upsert：order_no 存在则更新 status/status_label/pickup_no/unique_pos/pay 相关字段）
```

## 4. goods 端点微扩（ops.py，Agent A 顺带）

`GET /api/ops/goods` 响应的 skus[] 每项增加 `"specOptionInfos": <原始数组透传>`，供 F5 前端构造 spec_list。

## 5. 前端（Agents B/C）

- 路由：`/ops/order`（name order-create，meta.perm `feature:order`，title 下单工作台）；`/ops/pickup` 保留重写（feature:pickup）
- 菜单（MainLayout 协议功能组，v-if 已含 order 判断）："下单 · F5"、"取餐查询 · F6"
- apiOps 新增：`orderSettle(id,payload)` `orderCreate(id,payload)` `orderList(id,params)` `orderDetail(id,orderNo)` `orderStatus(id,orderNo)` `orderWaiting(id,orderNo)` `orderContinuePay(id,orderNo)` `orderCancel(id,orderNo)` `orderPay(id,orderNo,mode)`
- F5 页面流程（OrderWorkbenchView）：步骤条引导（选账号[强制 online]→选门店商品 SKU→试算与选券→确认→结果）；确认弹窗必含：账号/门店/商品规格数量/券/应付金额 + 0 元时"0 元单会真实制作饮品，不取自然作废"红色警示；结果面板：zero→成功卡（取餐码+跳 F6 按钮）；partial→支付卡（10 分钟倒计时、人工模式=支付串展示+指引、自动模式=实验性开关+双重确认+触发后轮询 status 5s）
- F6 页面（PickupView 重写）：账号选择→订单列表（tab 切换）→详情抽屉（状态/取餐码高亮/items/promotions）→制作中订单等待卡（3s 自动刷新可暂停）→待支付订单（5s status 探针 + 续付/支付模式入口）；移除 pickup-status 占位调用
- utils/format.js 增 ORDER_STATUS 映射与倒计时 helper

## 6. 支付宝自动支付接缝（Agent D，scripts/alipay_autopay.py）

```python
def autopay(order_str: str, config: dict | None = None) -> dict:
    # 返回 {"ok": bool, "status": "submitted"|"needs_interaction"|"failed",
    #        "message": str, "detail": {...}}
```
- config 来源：`account_system/data/autopay_config.json`（可选存在；含会话 Cookie 等，文档化，不硬编码凭据）
- 基于 `output/alipay_pay_link_20260926.json` 的 h5_cashier_url/h5_landing_302 与 `capture/chagee_native_phase0b_order_20260926.flows` 分析 H5 收银台链路；实现驱动骨架（会话保持/提交模板），离线测试比对 wire
- **不做真实扣款**；遇登录墙/交互环节返回 `needs_interaction` 并给出配置指引
- 后端 Agent A 以 try-import 方式接入（模块缺失 → auto 模式 501）
