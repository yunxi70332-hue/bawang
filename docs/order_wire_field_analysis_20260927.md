# 指定订单「请求霸王」交互全字段分析（2026-09-27）

> 分析对象：订单 `202609260910110040362088250`（取餐码 TA0002，门店 CN08121 广东佛山顺德龙江沃达百货店，粉芭乐 大杯/标准冰/标准糖 ×1，20 元代金券-DN `1309465801635041280` 全额抵扣，免支付 0 元单，2026-09-26 22:28:11 下单即支付，压线门店 22:29 打烊前一分钟）。
> 「请求霸王」= 我方纯协议客户端（`scripts/chagee_client.py` + `scripts/chagee_trade_api.py`，经 `account_system/server/routers/orders.py` 门控调用）向霸王茶姬生产网关 `gw.chagee.com` 发起的 HTTPS/JSON 请求。
> 证据：2026-09-26 生产 wire 存档（`output/trade_samples_20260926.json`、`output/cart_capture_settle_20260926.json`、`output/pay_zero_capture_20260926.json`、`output/pay_complete_capture_20260926.json`、`output/pay_lifecycle_20260926.json`）+ 引擎 wire 模板 + F5/F6 契约。

---

## 一、完整请求-响应周期（该订单实际走过的 10 跳）

```text
F5 下单链（下单工作台，orders.py order_settle/order_create 门控）
 ① GET  /user-client/customer/userInfo/query        （whoami：取 userId + mobileEncrypt）
 ② POST /chagee-navigation-web/.../goods/sku/calculatePrice   （服务端算价：折后单价）
 ③ POST /chagee-biz-trade-web/trade-web/order/settlePrice     （无券试算 recommendCoupon=true → draft）
 ④ POST /chagee-biz-trade-web/trade-web/order/settlePrice     （选券复跑 currentSelect → confirmOrderKey）
 ⑤ POST /chagee-biz-trade-web/trade-web/order/createOrder     （零元形态 → data 仅 {orderNo}）
 ⑥ POST /chagee-biz-trade-web/trade-web/order/getOrderDetail  （成单确认 → pickupNo=TA0002）
F6 查询链（取餐查询页 /ops/pickup）
 ⑦ POST /chagee-biz-trade-web/trade-web/order/getOrderList    （列表 → uniquePosOrderNo）
 ⑧ POST /chagee-biz-trade-web/trade-web/order/getOrderDetail  （详情抽屉）
 ⑨ POST /chagee-biz-trade-web/trade-web/order/getOrderStatus  （轻探针：制作中 3s 轮询）
 ⑩ POST /chagee-biz-trade-web/trade-web/order/getWaitingInfo  （等待信息：制作中 3s 刷新）
```

差额单（本单不是，但同周期会出现的分支）：⑤ 响应含 `{orderNo, payUrl, payNo}` → `continuePay` 重铸支付串 → 支付成功后状态 1→3。待支付单 10 分钟 `paymentExpiryType=autoCancel` 自动转 7。

---

## 二、公共协议层（所有请求共享）

### 2.1 请求头（17 项，chagee_client.headers()）

| 头 | 类型 | 值/约束 | 说明 |
|---|---|---|---|
| ua | str | 固定 `Dart/2.12 (dart:io)` | 兼容头（伪装旧引擎） |
| user-agent | str | 固定 `Dart/3.8 (dart:io)` | 真实引擎版本 |
| avc | str | `2060`（versionCode） | 版本校验敏感：旧值 638 触发 [99997] 门店渠道no不存在 |
| apv | str | `1.0.3`（versionName） | 同上 |
| tcode | str | `CHAGEE` | 租户码 |
| channel | str | `APP` | 渠道 |
| os | str | `android` | |
| aid | str | `100001` | AppID |
| language | str | `zh_CN` | |
| region | str | `CN` | |
| devicetimezoneregion | str | `Asia/Shanghai` | |
| content-type | str | `application/json` | 空 body 请求不带（cityList 语义） |
| uuid | str | 36 字符安装标识 | 与 cid 同值；本地生成或复用设备值 |
| cid | str | 同 uuid | |
| sk | str | base64（明文 16 hex 字符） | getsk 下发的 AES 密钥；trade 域必带 |
| authorization | str | 608B 裸 JWT，**无 Bearer 前缀** | 失效即 401/12320120400401，无 refresh |

**加密/签名**：trade 域（本周期全部 10 跳）**无 sign 头、无字段加密**——sign 仅登录域触发（signFields 子集 HMAC-SHA1）；唯一密文字段是 `orderInfo.userInfo.mobile`（AES-128-ECB/PKCS7，从 whoami 的 `mobileEncrypt` 原样透传，免本地加密）。

### 2.2 响应包络（所有业务响应统一）

| 字段 | 类型 | 业务含义 | 取值范围 |
|---|---|---|---|
| errcode | string | 业务状态码（**字符串非 int**） | `"0"`=成功；会话 `"401"`/`"12320120400401"`；校验 `"99997"`族、`"91010009"`、`"9105050200005"`、`"8101000200010"`、`"82041201"` 等 |
| errmsg | string | 状态描述 | `"处理成功"`；失败时中文描述（注意 `"网络异常，请稍后重试"` 是 trade 校验失败兜底文案，最常见根因=门店打烊） |
| data | any | 业务载荷 | 裸标量（getOrderStatus 为 int）/对象/数组，结构逐接口见下文 |
| thirdTraceId | string | 三方追踪 | 常空串 |
| globalTicket | string | 32 位 hex | 每响应一枚，风控关联票据 |
| timestamp | int | 毫秒时间戳 | 服务器时间 |

**金额全局约定**：金额一律**字符串**（`"20"`/`"20.00"` 两种精度并存——trade 域去尾零、购物车域两位小数）；时间两种形态——毫秒 int（券有效期/timestamp）与 `"yyyy-MM-dd HH:mm:ss"` 字符串（orderTime/payTime）。

---

## 三、逐接口字段表

### ① whoami — GET /user-client/customer/userInfo/query

无请求体（查询参数无）。本周期消费的响应字段：

| 字段 | 类型 | 业务含义 | 示例/取值 |
|---|---|---|---|
| data.customerId | string | 会员 ID = 全链 userId 来源 | `1190018250`（10 位数字） |
| data.mobileEncrypt | string | AES 密文手机号（sk 派生 key 加密） | `YUUjd1xuMwOdN/NgckaJ6Q==`，原样透传给 createOrder |
| data.nickName | string | 昵称（会话自检展示） | `Hi，茶友` |

响应含完整会员资料（等级等），本周期仅依赖上列 3 字段。

### ② calculatePrice — POST /chagee-navigation-web/api/navigation/goods/sku/calculatePrice

**请求体**（wire：cart_capture_settle_20260926.json，恰好就是本单门店 CN08121 + 粉芭乐）：

| 字段 | 类型 | 约束 | 示例 |
|---|---|---|---|
| skuInfo.spuId | string | 必填，19 位 ID | `1253292834069934081` |
| skuInfo.spuType | string | `stand`/`extra`/套餐 | `stand` |
| skuInfo.skuId | string | 必填 | `1253292834082516992` |
| skuInfo.num | int | ≥1 | `1` |
| skuInfo.salePrice | **number(float)** | SKU 原价；此处为全链唯一数值型金额 | `20.0` |
| skuInfo.specList[] | array | `{specId, specOptionId}` 两键（无需名称） | 杯型/大杯 |
| skuInfo.attributeList[] | array | `{attributeId, attributeOptionId}` 两键 | 温度/标准冰、甜度/标准糖 |
| skuInfo.extraList[] | array | 可选加料行 `{spuId, skuId, extraId, num, salePrice, spuType:"extra"}` | 本单无 |
| saleChannel | string | `"2"` | |
| saleType | string | `"1"`（自取） | |
| storeNo | string | 门店编码 | `CN08121` |
| storeBusinessType | int | `1` | |
| storeChannelCode | string | `"android"`（缺失触发 [99997]） | |
| userId | string | whoami customerId | `1190018250` |

**响应 data**（金额均两位小数字符串）：

| 字段 | 类型 | 业务含义 | 示例 |
|---|---|---|---|
| spuId / spuType / skuId | string | 回显 | |
| totalSalePrice | string | 原价合计 | `"20.00"` |
| totalTradePrice | string | 交易合计（本单口径 20） | `"20.00"` |
| totalGoodsItemPrice | string | **折后单价合计**（SettleResult 行价映射源） | `"15.00"`（新人 5 元券后） |
| totalGoodsItemDiscountAmount | string | 商品侧折扣 | `"0.00"` |
| totalGoodsPaymentDiscountAmount | string | 支付侧折扣（券） | `"5.00"` |
| totalDiscountAmount | string | 折扣总计 | `"5.00"` |
| totalWrappingPrice | string | 包装费 | `"0.00"` |

### ③④ settlePrice — POST /chagee-biz-trade-web/trade-web/order/settlePrice

**请求体**（两次调用同构：③ `recommendCoupon:true` + `discountList:[]`；④ 选券 `recommendCoupon:false` + discountList 带 `currentSelect`）：

| 字段 | 类型 | 约束 | 示例 |
|---|---|---|---|
| settleBizInfo.businessType | int | `1`（自取；wire 定案，非静态注释的 2） | `1` |
| settleBizInfo.orderType | int | `0` | `0` |
| settleBizInfo.storeNo | string | 营业中门店（打烊店触发"网络异常"兜底文案） | `CN08121` |
| settleBizInfo.userId | string | | `1190018250` |
| settleBizInfo.userType | int | `3` | `3` |
| settleBizInfo.deliveryType | int | `1`（自取） | `1` |
| settleBizInfo.packageFeeSelected | null/str | 保温袋选择 | `null` |
| settleBizInfo.recommendCoupon | bool | true=服务端自荐推荐券 | `true` |
| goodsList[].uniqueKey | string | **UUIDv4，客户端生成**，响应原样回显、createOrder 回传 | `a75b2361-…` |
| goodsList[].currentGoodId | null/32hex | 直发路径恒 null（仅购物车 change 需要 MD5 指纹） | `null` |
| goodsList[].itemId / orderItemNo | null | 占位 | `null` |
| goodsList[].buyNum | int | ≥1 | `1` |
| goodsList[].salePrice | string | **= calculatePrice.totalGoodsItemPrice（折后）**；占位 `"0.00"` 被 [9105050200005] 拒绝 | `"15"` |
| goodsList[].marketPrice | string | SKU 原价 | `"20"` |
| goodsList[].totalItemAmount | string | = calculatePrice.totalTradePrice | `"20"` |
| goodsList[].totalItemDiscountedAmount | string | = totalGoodsItemPrice | `"15"` |
| goodsList[].spuId/skuId/skuName/spuType/skuImage | — | 商品标识（skuName 缺失回退 spuName，服务端 [99997] 校验非空） | 粉芭乐 |
| goodsList[].specList[] | array | **三键** `{specId, specOptionId, specOptionName}`（自造键序触发 [91010009]） | 大杯 |
| goodsList[].attributeList[] | array | 四键含名称（名称保留原始空格，如 `" 温度"`） | 标准冰/标准糖 |
| goodsList[].extraList[] | array/null | 必选加料组必须携带（缺→[9105050200005]） | `[]` |
| goodsList[].nutritionInfo | object | `{energyValue, energyUnit, energyLevel}` | 192 kcal/杯 B/C |
| goodsList[].（其余 14 键） | null | comboGroupId/isGift/ruleId/comboGroupType/tagList/comboItemList/discountList/tradeGoodsType/promotionDiscountId/realPrice/clientUniqKey/spuCategory/tagImageUrl/refunded — 显式置空占位 | `null` |
| discountList[]（顶层） | array | 选券行（见 2.3 折扣行）；③ 空 / ④ 选中券 | |

**折扣行**（settle 请求 ↔ createOrder 请求 ↔ settle 响应三处同构）：

| 字段 | 类型 | 约束/含义 | 示例 |
|---|---|---|---|
| deductionType | null | | |
| discountAmount | string | 抵扣金额；settle 请求可 null（服务端回填） | `"20"` |
| discountId | string | **= 券 couponCode**（三处统一锚点） | `1309465801635041280` |
| currentSelect | bool/null | createOrder 提交时须 `true` | `true` |
| discountName | string | = 券 templateName | `霸王茶姬20元代金券-DN` |
| discountSource | int | `1` | |
| discountType | int | `1` | |
| ruleId / scopeType | null/int | settle 响应回填 scopeType=2 | `null` / `2` |

**响应 data**：

| 字段 | 类型 | 业务含义 | 取值/示例 |
|---|---|---|---|
| confirmOrderKey | string | 27 位下单确认键，**一次性**，settle→create 的令牌 | `202609261410320010132193116` |
| totalBuyNum | int | 总件数 | `1` |
| orderGroupList[] | array | **订单组（须原样回传 createOrder）** | 见下 |
| orderGroupList[].orderId | string | 固定 `"100001"` | |
| orderGroupList[].goodsList[] | array | 商品行回显（服务端补 itemId=`200001_0`、tradeGoodsType=1、discountList 已回填金额） | |
| orderGroupList[].tradeFundInfo | object | 组级资金 18 字段（见资金字段族） | |
| discountList[] | array | 服务端已回填金额的折扣行 | |
| availableDiscountList | array | 空观测 | `[]` |
| assetInfo.userCouponInfo.availableCouponList[] | array | **下单可用券列表**（选券唯一入口；order-coupon-list 生产 404） | 见券条目 |
| tradeFundInfo | object | 顶层资金 18 字段 + packageFeeInfo | buyerRealPrice=`"0"` |
| channelMapping | object | 渠道映射回显 `{tradeChannelCode:"09", storeChannelCode:"android", storeBusinessType:1, goodsChannelType:2, goodsSaleType:1}` | |

**券条目**（availableCouponList[] 每项 15 字段）：

| 字段 | 类型 | 业务含义 | 示例/取值 |
|---|---|---|---|
| couponCode | string | 券码（= discountId = promotionId） | `1309465801635041280` |
| templateNo | string | 券模板号 | `994625568300003329` |
| templateName | string | 完整券名 | `霸王茶姬20元代金券-DN` |
| status | int | 券状态 | `10`（可用） |
| bizType | int | 业务类型 | `1` 代金券 / `21` 减配券 |
| benefitText | string | 面额文案 | `"20元"` |
| benefit2Text | string | 权益类型 | `"代金"` / `"配送费"` |
| useStartTime / useEndTime | int | 毫秒有效期窗口 | `1790092800000`–`1797868799000` |
| thresholdTips | string | 门槛，空串=无门槛 | `"满20元可用"` / `""` |
| currencyCode / currency | string | 币种 | `CNY` / `¥或元` |
| canDiscount | bool | 服务端可用性判定 | `true`/`false` |
| unavailableReason | string | 不可用原因（canDiscount=true 也可能携带互斥提示） | `"与已参与优惠不能同享"` |
| selected | bool | 服务端推荐/选中标记 | |

**资金字段族**（tradeFundInfo，18 金额 + packageFeeInfo；金额全字符串，`""` 与 `"0"` 并存）：

| 字段 | 含义 | 本单值 |
|---|---|---|
| totalOriginPrice | 原价合计 | `"20"` |
| totalSalePrice | 售价合计 | `"20"` |
| totalMarketPrice | 市场价合计 | `"0"` |
| totalTradePrice | **交易总额（门槛/选券基准）** | `"20"` |
| totalGoodsItemPrice | 折后商品合计 | `"0"` |
| totalFreightPrice / totalRealFreightPrice / totalFreightDiscountAmount | 运费三件套（自取恒 0） | `"0"` |
| totalPackagePrice / totalRealPackagePrice / totalPackageDiscountAmount | 包装三件套 | `"0"` |
| totalExtraGoodsPrice | 加料合计 | `"0"` |
| **buyerRealPrice** | **应付金额（支付场景判据：=="0"/"0.00" → 零元直通）** | `"0"` |
| totalDiscountAmount | 折扣总计（= ΣdiscountList[].discountAmount，一致性断言项） | `"20"` |
| totalGoodsItemDiscountAmount / totalGoodsPaymentDiscountAmount | 商品侧/支付侧折扣 | `"0"` / `"20"` |
| totalCouponDiscountAmount | 券折扣（组级 `""`、顶层 `"20"`，空串语义待定） | `"20"` |
| totalActivityDiscountAmount | 活动折扣 | `"0"` |
| packageFeeInfo | `{packageName:"保温袋", packageRemark, required:false, amount:"1"}` | 可选项 |

### ⑤ createOrder — POST /chagee-biz-trade-web/trade-web/order/createOrder

**请求体**（零元形态，wire：pay_zero_capture）：

| 字段 | 类型 | 约束 | 本单值 |
|---|---|---|---|
| tcode | string | `"CHAGEE"` | |
| orderInfo.buyerRemark | null/str | 备注 | `null` |
| orderInfo.orderBizInfo.businessType | int | `1` | |
| orderInfo.orderBizInfo.orderType | int | `0` | |
| orderInfo.storeInfo.storeNo / storeName | string | 运行时注入 | `CN08121` / `广东佛山顺德龙江沃达百货店` |
| orderInfo.userInfo.userId | string | whoami customerId | `1190018250` |
| orderInfo.userInfo.userType | int | `3` | |
| orderInfo.userInfo.mobile | string | **AES 密文**（whoami mobileEncrypt 直取透传） | `YUUjd1xuMwOdN/NgckaJ6Q==` |
| orderInfo.userInfo.areaCode | string | `"86"` | |
| orderInfo.paymentInfo.payerId | string | = userId | `1190018250` |
| orderInfo.paymentInfo.payType | int/null | **零元=null；差额=60（支付宝）** | `null` |
| orderInfo.paymentInfo.channelCode | string/null | **零元=null；差额="UnionPay"** | `null` |
| orderInfo.paymentInfo.currencyType | int | `156`（CNY） | |
| orderInfo.paymentInfo.payAmount | string | = settle buyerRealPrice | `"0"` |
| orderInfo.deliveryInfo.deliveryType | int | `1` | |
| orderInfo.deliveryInfo.packageFeeSelected | null | | |
| orderGroupList | array | **settle 响应原样回传**（深拷贝），行内 discountList 注入选中券行（currentSelect:true） | |
| discountList（顶层） | array | 选中折扣行（与组/行内三处同步） | 券行 ×1 |
| confirmOrderKey | string | settle 令牌 | |

**一致性断言（提交前，引擎 assert_settle_consistency）**：settle.tradeFundInfo.buyerRealPrice == orderGroupList[0].tradeFundInfo.buyerRealPrice；ΣdiscountAmount == totalDiscountAmount。

**响应 data**：

| 场景 | data 字段 | 类型 | 含义 |
|---|---|---|---|
| **零元（本单）** | orderNo | string | 27 位订单号；**仅此一键**（出现 payUrl/payNo 即违反应被断言拦截） |
| 差额 | orderNo | string | 同上 |
| 差额 | payNo | string | 支付流水 `CHP2026092610100109872701022` |
| 差额 | payUrl | string（**内嵌 JSON 字符串**） | `{requestJson:{orderStr:"alipay_sdk=…&biz_content=<urlencoded>&sign=RSA2…"}}`，orderStr 内 biz_content 含 out_trade_no/total_amount/time_expire/timeout_express="30m"/subject(含门店名) |

### ⑥⑧ getOrderDetail — POST /chagee-biz-trade-web/trade-web/order/getOrderDetail

**请求体**：`{userId: str, orderNo: str(27位), channelCode: "android"}`。

**响应 data**（订单域最全结构）：

| 字段 | 类型 | 业务含义 | 取值/示例 |
|---|---|---|---|
| orderNo | string | 订单号 | `202609260910110040362088250` |
| orderStatus | int | **状态机**：1 待支付 / 3 制作中 / 6 已完成 / 7 已取消（终态 6、7） | `6` |
| orderStatusText | string | 服务端状态文案（前端优先采用） | `"已完成"` |
| pickupNo | string | 取餐码；1/7 态空 | `TA0002` |
| uniquePosOrderNo | string | POS 唯一单号（getWaitingInfo 依赖） | `D00296329595152515072` |
| storeNo / storeName / cityName | string | 门店 | CN08121 |
| longitude / latitude | string | 门店坐标（字符串） | `117.020775` |
| phone / managerPhone / servicePhone | string | 门店电话族 | `400 878 8359` |
| businessType | int | `1` | |
| businessTypeText | string | `"自取"` | |
| orderTime | string | 下单时间 `yyyy-MM-dd HH:mm:ss` | `2026-09-26 22:28:11` |
| payTime | string | 支付时间；待支付态缺省 | 同 orderTime（零元即时） |
| totalAmount | string | 总额 | `"20"` |
| orderDiscountAmount | string | 订单折扣 | `"20"` |
| payAmount | string | **实付**（成单核验断言项） | `"0"` |
| fulfillAmount | string | 履约金额 | `"0"` |
| payType | int | `0`=免支付 / `60`=支付宝App支付 | `0` |
| payTypeText | string | | `"免支付"` |
| buyerRemark | string | 备注 | `""` |
| hasRefund / canRefund | bool | 退款标记 | `false`/`true` |
| paymentExpiryTimestamp | int(ms) | **仅待支付态**出现的支付截止（下单+10min） | null |
| paymentExpiryType | string | 仅待支付态 | `"autoCancel"` |
| orderItems[] | array | 商品行（结构同 settle goodsList 精简：spuId/spuType/skuId/skuName/skuImage/refundNum/buyNum/salePrice/totalItemAmount/totalItemDiscountedAmount/specList/attributeList/tagList/nutritionInfo） | 粉芭乐 |
| orderPromotions[] | array | **券核销记录**：`{promotionType:"coupon", promotionId(=券码), activityType:1, promotionName, discountAmount}` | DN 券 -20 |
| isQmHistoricOrder | string | 历史单标记（字符串） | `"0"` |
| point / exp | string | 积分/经验 | `"0"` |

### ⑦ getOrderList — POST /chagee-biz-trade-web/trade-web/order/getOrderList

**请求体**：

| 字段 | 类型 | 约束 | 示例 |
|---|---|---|---|
| userId | string | | `1190018250` |
| pageSize | int | 1–50 | `10` |
| pageNum | int | ≥1 | `1` |
| orderType | **string** | `"1"`（注意字符串型） | `"1"` |
| tabType | string | `today` / `history` | `today` |
| channelCode | string | `"android"` | |

**响应 data**：`{pageNum:int, pageSize:int, pageList:[订单行], qmPageNum:int}`。订单行 = getOrderDetail 字段子集 + 两个列表专属字段：

| 字段 | 类型 | 含义 | 出现条件 |
|---|---|---|---|
| canBuyAgain | bool | 可重新购买 | 终态单（6/7） |
| buyAgainText | string | `"重新购买"` | 同上 |
| payTime / pickupNo | — | 待支付/已取消行缺省或空 | |

### ⑨ getOrderStatus — POST /chagee-biz-trade-web/trade-web/order/getOrderStatus

**请求体**：`{userId: str, orderNo: str}`。
**响应 data**：**裸 int**（非对象）——`1/3/6/7`。最轻探针，用于待支付 5s / 制作中 3s 轮询。

### ⑩ getWaitingInfo — POST /chagee-biz-trade-web/trade-web/order/getWaitingInfo

**请求体**（**无 userId**，wire 定案）：

| 字段 | 类型 | 约束 | 示例 |
|---|---|---|---|
| storeNo | string | 门店编码（须与下单一致） | `CN08121` |
| orderNo | string | 27 位订单号 | `2026…250` |
| uniquePosOrderNo | string | **必须先从 getOrderList/getOrderDetail 行获取** | `D00296329595152515072` |

**响应 data**：

| 字段 | 类型 | 业务含义 | 取值范围 |
|---|---|---|---|
| waitingCups | int | 前方等待杯数 | ≥0 |
| waitingTime | int | 预计等待秒数 | ≥0（样本 300） |
| queueLimit | int | 队列上限 | ≥0（样本 61） |

### 附：差额分支 continuePay — POST /.../order/continuePay

**请求体**：`{tcode:"CHAGEE", userId, orderNo, channelCode:"UnionPay", payType:60}`。
**响应 data**：`{orderNo, payUrl(内嵌串，每次重铸 out_trade_no 均变), payNo}`——结构同 createOrder 差额分支。cancelOrder 请求体 `{userId, orderNo, channelCode:"android"}`（静态推断，无 wire 样本，实验性）。

---

## 四、字段映射关系（跨接口数据流）

### 4.1 身份与密文
- `whoami.data.customerId` → 全链 `userId`（②③④⑤⑥⑦⑧⑨）/ `orderInfo.paymentInfo.payerId`（⑤）
- `whoami.data.mobileEncrypt`（AES 密文）→ `createOrder.orderInfo.userInfo.mobile`（**原样透传**，服务端自己解密，客户端免二次加密）

### 4.2 价格链（calculatePrice → settlePrice）
- `resp.totalGoodsItemPrice` → settle 行 `salePrice`、`totalItemDiscountedAmount`
- `resp.totalTradePrice` → settle 行 `totalItemAmount`
- SKU 原价 → settle 行 `marketPrice`
- 占位 `"0.00"` 会被 [9105050200005] 拒绝 → 直发行价格必须来自 calculatePrice 真实值

### 4.3 券标识三处统一（本单 1309465801635041280）
```text
settle 响应 availableCouponList[].couponCode   （券码，选券入口）
  → settle④/createOrder 请求 discountList[].discountId     （currentSelect:true）
  → getOrderDetail 响应 orderPromotions[].promotionId      （核销凭证，verify() 断言）
  → 本地 CouponRecord.coupon_code / CouponUsageLog.coupon_code（券档案与使用日志锚点）
```

### 4.4 settle → create 回传链
- `settle.confirmOrderKey` → `create.confirmOrderKey`（一次性令牌）
- `settle.orderGroupList` → `create.orderGroupList`（**原样深拷贝回传**；组/行/顶层三处 discountList 同步注入选中券）
- `goodsList[].uniqueKey`（UUIDv4 客户端生成）→ 响应回显 → createOrder 回传（行关联键）

### 4.5 订单号依赖链
- `createOrder.data.orderNo`（零元单唯一输出）→ getOrderDetail / getOrderStatus / getOrderList 匹配 / continuePay / cancelOrder 的 `orderNo`
- `getOrderList|getOrderDetail.uniquePosOrderNo` + `storeNo` → `getWaitingInfo` 请求（缺一不可，本地 OrderRecord 缓存优先、缺失先补详情）

### 4.6 金额判据与状态机
- `tradeFundInfo.buyerRealPrice`：`Decimal==0` → 零元场景（paymentInfo 三 null + payAmount="0"，响应仅 orderNo）；`>0` → 差额场景（payType=60/UnionPay，响应带支付腿）
- `orderStatus` int → 我方映射 `{1:待支付, 3:制作中, 6:已完成, 7:已取消}`；`orderStatusText` 服务端文案优先
- 待支付专属：`paymentExpiryTimestamp`（下单+10min）+ `paymentExpiryType:"autoCancel"` → 我方 pay_deadline / 校准线程依据

### 4.7 我方系统消费映射（霸王响应 → 管理系统 UI/DB）
- getOrderList/Detail 行 → OrderRecord upsert（order_no 唯一键；空值不覆盖）→ `/ops/pickup` 页面列表与详情抽屉
- `orderItems[]` + `orderPromotions[]` → 详情抽屉"商品（N）/券核销（N）"表格（本单：粉芭乐 ×1 ¥20 / DN 券 -¥20）
- `payTypeText`/`payAmount`/`pickupNo` → 详情头（免支付 / ¥0 / TA0002）
- `waitingCups/waitingTime/queueLimit` → 制作中等待卡
- errcode≠"0" → 异常分级链（SessionExpired 409 置 expired / Trade 502 / "网络异常"追加打烊提示）

---

## 五、错误响应字段（errcode 体系实例）

| errcode | errmsg | 触发场景（实证） |
|---|---|---|
| 82041201 / 91010009 / 8202020200007 | `网络异常，请稍后重试` | trade 校验兜底文案（同文案不同码）；最常见根因=门店打烊（本单门店 CN08121 09:00–22:29，22:28:11 压线下单成功） |
| 99997（族） | `当前商品ID不能为空` / `商品类型不能为空` / `门店渠道no不存在` | currentGoodId 空 / 批量删除缺 spuType / 头版本号过旧或缺 storeChannelCode |
| 9105050200005 | （settle 拒绝） | 直发行价格占位 0.00 / 缺必选加料 |
| 8101000200010 | `购物车清空非法` | clearAll=true |
| 401 / 12320120400401 | `您的账号已退出登录` | token 失效（HTTP 200 + errcode="401" 也存在） |

---

## 六、遗漏项与不确定度（诚实标注）

1. `totalCouponDiscountAmount`/`totalActivityDiscountAmount` 在组级出现 `""`、顶层为数值字符串——空串语义（无该项 vs 未计算）未定案。
2. `getWaitingInfo` 样本仅 1 组（0/300/61），waitingTime 的上限语义（是否封顶）未验证。
3. `cancelOrder` 无 wire 样本，请求体按同族推断，响应标注 experimental。
4. getOrderStatus 响应仅有 1/3/6/7 四值实证，2/4/5 是否存在未观测（静态枚举未覆盖）。
5. `point`/`exp`/`isQmHistoricOrder`（字符串"0"）为透传字段，业务含义以字面推断。
