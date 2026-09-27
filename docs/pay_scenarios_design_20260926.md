# 纯协议支付场景引擎设计（chagee_trade_api）

> 日期 2026-09-26 | 依据：当日三笔真实订单 wire 抓包（¥10 差额单、¥8 续付单、¥0 零元单）+ 9/23 样本 + 静态汇编证据

## 文档定位

本文是支付场景引擎需求的定稿归档，覆盖三部分内容：三种支付场景（待支付 / 零元支付 / 差额支付）的业务特征与技术表现总结、纯协议可行性评估结论，以及 `chagee_trade_api` 的实现方案设计。对应的工程实现见 `scripts/chagee_trade_api.py`，配套测试见 `tests/test_trade_api.py`。文中所有字段、数值与端点行为均以真实抓包与静态证据为凭，后续迭代以代码与测试为准。

## 一、三种支付场景的业务特征与技术表现

### 场景 1 待支付（createOrder 成功未付款）

- 业务特征：订单已创建（锁库存 / 锁券），资金尚未清算，存在有限支付窗口。
- `createOrder` 响应 `data = {orderNo, payUrl, payNo}`；其中 `payUrl` 为 JSON 字符串，内嵌 `requestJson.orderStr`（支付宝 `alipay.trade.app.pay`、RSA2 签名、银联商务 chinaums 收单、`timeout_express=30m`）。
- `getOrderDetail`：`orderStatus=1`（"待支付"）、`pickupNo=""`；`paymentExpiryTime`（下单时间 + 10 分钟）与 `paymentExpiryType="autoCancel"` 仅在该场景出现。
- 实证：¥10 单 16:40:50 下单 → 16:52:54 翻 `s7` 已取消；有效窗口 = min(茶姬 10min, 支付宝 30m) = **10 分钟**。
- 续付：`POST /chagee-biz-trade-web/trade-web/order/continuePay`，请求体 `{tcode:"CHAGEE", userId, orderNo, channelCode:"UnionPay", payType:60}`，响应与 `createOrder` 同构（全新 `out_trade_no` + 重签 `orderStr`），免走购物车。

### 场景 2 零元支付（券面额 = 订单金额）

- 请求 `paymentInfo = {payerId, payType:null, channelCode:null, currencyType:156, payAmount:"0"}`。
- 响应 `data` 仅含 `{orderNo}`，无 `payUrl` / `payNo` / 支付宝字段 / `commitPay`。
- 下一跳 `getOrderDetail` 直接进入 `orderStatus=3`（"制作中"）、`payType=0`（"免支付"）、`payTime == orderTime`、`pickupNo` 同步下发。

### 场景 3 差额支付（券面额 < 订单金额）

- 请求 `paymentInfo = {payerId, payType:60, channelCode:"UnionPay", currencyType:156, payAmount:"10"}`（20 元饮品 − 10 元券）。
- 响应含 `payUrl`（`biz_content.total_amount="10.00"`、`subject` 带门店名）与 `payNo`（CHP… 前缀）。
- 支付成功后：`s3` 制作中、`payAmount="10"`、`payTypeText="支付宝App支付"`、`orderPromotions[].promotionId == 券 couponCode`（核销凭证）。

### 共用事实

1. **券标识三处统一**：券列表 `couponCode` == 折扣行 `discountId` == 订单 `orderPromotions.promotionId`。
2. 下单可用券入口在 `settlePrice` 响应 `assetInfo.userCouponInfo.availableCouponList`（含 `canDiscount` / `selected` / `unavailableReason` / `thresholdTips` / `useEndTime`）；`order-coupon-list` 生产环境 404，弃用。
3. 标准链路：`shoppingCart/change` → `shoppingCart/get` → `settlePrice`（得 `confirmOrderKey` + `assetInfo`）→ `createOrder`（回传 `confirmOrderKey` + `discountList`，以 `currentSelect:true` 表达选券）→ [payUrl → 支付 → `getOrderStatus` 轮询] 或 [0 元直通 s3]。
4. 状态枚举：`1=待支付 3=制作中 6=已完成(T0xxx) 7=已取消`；金额一律字符串；`mobile` 字段 AES 加密（可直接取 `whoami` 的 `mobileEncrypt`，免本地加密）。
5. `getOrderStatus`：请求 `{userId, orderNo}`，响应 `data:int`。

## 二、纯协议可行性评估（结论表）

| 目标 | 结论 | 判据 / 依据 |
| --- | --- | --- |
| 场景识别 | 完全可行 | `settlePrice` 响应 `tradeFundInfo.buyerRealPrice` 为 `"0"` 即零元、`>0` 即差额，`createOrder` 前已知；待支付 = 响应含 `payUrl` 且 `orderStatus==1` |
| 状态流转 | 可行 | 服务端下发状态、客户端驱动：选券 → 试算 → 下单 →（支付）→ 轮询终态；`continuePay` 10 分钟窗口内幂等续付；0 元免支付腿直达 |
| 一致性 | 可行 | 双向断言：`settlePrice.buyerRealPrice == 构造 paymentInfo.payAmount == orderGroupList[].tradeFundInfo.buyerRealPrice`；`ΣdiscountAmount == totalDiscountAmount`；成单后 `getOrderDetail.payAmount == 期望` 且 `orderPromotions.promotionId == couponCode` |
| 异常处理 | 可行 | s1 超时 → autoCancel（预期行为）；支付中断 → `continuePay` 重铸支付串；售罄 / 库存 → `shoppingCart/get` 5 个失效列表（`saleOutSkuIds` / `invalidSkus` / `invalidCurrentGoodIds` / `stockChangeSkus` / `outSalePeriodSkuIds`）；`confirmOrderKey` 失效 → 重新试算；`createOrder` 超时 → 先 `getOrderList` 查单防悬挂再重试（`createOrder` 无显式幂等键，按非幂等处理） |
| 边界（不可行项，诚实标注） | 部分不可行 | 支付宝侧扣款动作本身不在纯协议范围（纯协议负责取得 `orderStr` / 收银台链接与成单判定）；订单超时翻 `s7` 后不可救单，只能重新下单 |

## 三、模块设计（chagee_trade_api.py）

### 异常体系

- `TradeError`：通用交易错误。
- `ConsistencyError`：一致性断言失败。
- `OrderHangError`：悬挂单，疑似重复创建。

### 枚举

- `OrderStatus{PENDING=1, MAKING=3, DONE=6, CANCELLED=7}`
- `PayScenario{ZERO, PARTIAL}`

### 数据类

- `SettleResult(confirmOrderKey, total_trade_price, buyer_real_price, available_coupons, discount_list, channel_mapping, order_group_list, trade_fund_info)`
- `PayLink(order_no, pay_no, order_str, out_trade_no, total_amount, expire_at)`
- `OrderOutcome(order_no, status, pay_amount, pay_type_text, pickup_no, promotions)`

### ChageeTradeApi(client) 接口

`cart_add` / `settle` / `classify`（静态） / `pick_coupon`（静态券匹配） / `build_create_body`（静态，以 wire 为模板） / `create_order` / `continue_pay` / `wait_status`（轮询） / `verify` / `cancel`（兜底，静态端点、无样本，标注待实测） / `pay_deadline`

### 异常矩阵

| 异常情形 | 处置 |
| --- | --- |
| `SessionExpired` | 停止并重新登录 |
| `settle` 与预期不符 | 中止流程 |
| `createOrder` 超时 | 查单（`getOrderList`）防悬挂后重试 |
| s1 过窗 | 预期 autoCancel，不做抢救 |
| `payUrl` 解析失败 | `continuePay` 兜底重铸 |

## 四、状态机

```
settlePrice ──buyerRealPrice==0──► createOrder ──{orderNo}──► s3 制作中(免支付)
      └──buyerRealPrice>0──► createOrder ──{payUrl}──► s1 待支付 ──支付──► s3
                                                    ├─continuePay(10min内)─┘
                                                    └─10min autoCancel──► s7 已取消
s3 制作中 ──► s6 已完成(取餐码 Txxxx)
```

## 五、测试与验收

### 离线 wire 回放

以三份存档驱动全部逻辑：`output/trade_samples_20260926.json`、`output/pay_lifecycle_20260926.json`、`output/pay_zero_capture_20260926.json`，覆盖：

- `classify` 场景判定；
- `createOrder` 两分支构造体（零元 / 差额）；
- `PayLink` 解析；
- 一致性断言正反例；
- 状态机三条转移。

### 验收标准

1. `classify` 对三份样本判定正确；
2. `createOrder` 两分支构造体与 wire 字段级一致；
3. 测试不依赖设备网络（纯离线回放）。
