# 霸王茶姬六功能纯协议实现 — 设计与进度文档

> 日期：2026-09-23 ｜ 状态：**方案已批准，Phase 0/1 已完成，应用户要求暂停推进待确认**
> 依据：知识库 `knowledge_base_20260922.md`（已工具交叉验证）+ 三路探索（协议库/抓包流量/静态资产）

---

## 一、需求（用户原始六步）

1. **设备注册流程**：完成账号登录，身份验证成功并建立有效会话
2. **路径选择**：明确选择「门店自取」服务路径
3. **数据获取**：游客模式调用接口获取城市信息及菜品 SKU 配置
4. **账号功能**：登录态执行优惠券分类查询
5. **订单操作**：已登录账号发起门店自取订单
6. **订单查询**：自取订单取餐信息查询（取餐时间/地点/取餐码）

要求：步骤间数据传递准确、流程连贯、正确处理异常。

**用户已拍板的三个决策**（2026-09-23）：
| 决策点 | 选择 |
|---|---|
| 下单深度 | 用账号内**饮品兑换券，0 元支付**完整闭环 |
| 登录验证 | **两者都做**：先复用设备 token 跑通全流程，最后真实短信登录闭环 |
| 补抓配合 | 用户在云手机走一遍下单流程供抓包 |

---

## 二、探索结论（方案事实基础）

### 已有可复用资产
| 资产 | 内容 | 验证强度 |
|---|---|---|
| `scripts/chagee_protocol.py` | HMAC-SHA1 签名（generateSign @0xb48da0）、AES-128-ECB/PKCS7 字段加密、getsk、airhub MD5 签名、登录短信体构造 | 签名 wire 3/3；短信体**字节级一致**；airhub 13285/0 |
| `scripts/chagee_menu_api.py` | 游客模式 M1-M7：cityList/store list/storeGoodsMenu/goods/detail 等 | 生产实测：22 城市、杭州 198 店、CN00529 = 14 分类/82 SPU/94 SKU |
| `scripts/session_seed.json` | 今日导出：设备真实 uuid + 608B 裸 JWT token（release） | whoami errcode=0（见下） |

### 六功能覆盖度与映射
| 功能 | 事实 | 协议层方案 |
|---|---|---|
| 1 设备注册 | **App 无独立设备注册端点**（静态 pp.txt + 抓包双证）。设备身份 = 本地生成 uuid/cid（同值，36 字符）+ 请求头携带；`im-user-device-info/report` 仅极光推送上报（可选） | uuid 生成/复用 + 13 公共头 + getsk 握手 ≈ 协议层「设备注册」；登录 = message/send + auth/login/sms |
| 2 自取路径 | 非端点而是参数族：`saleType:"1"`（自取菜单）、createOrder `deliveryType`、`businessType:"1"` | 贯穿功能 3/5 的参数，值以补抓样本定案。**勘误 2026-09-26**（wire）：createOrder `orderBizInfo.businessType=1`（int，settlePrice `settleBizInfo.businessType` 同值），非旧记"下单域 2" |
| 3 游客城市+SKU | **已实现**（menu_api，生产实测） | 薄封装复用：cityList→store/list→storeGoodsMenu→goods/detail |
| 4 优惠券分类 | **此版本无 classify/category 端点**（静态零命中）；实际三列表：`effective-list`(可用)/`historical-list`(历史)/`order-coupon-list`(下单可用) | 三列表分类汇总输出；识别「饮品兑换券」供功能 5 联动 |
| 5 下单 | trade 全家族静态已知（shoppingCart/change,get → settlePrice → createOrder → commitPay → cancelOrder），**抓包零样本** | 补抓后按样本复刻；settlePrice 断言 0 元后才允许 createOrder |
| 6 取餐查询 | getOrderList 有 wire 样本（含 pickupNo=T0133、待取餐状态）；getOrderDetail/getWaitingInfo/getOrderStatus 静态已知零样本 | getOrderList 定位 → getOrderDetail（pickupNo）→ getWaitingInfo（waitingCups/waitingTime/queueLimit）→ getOrderStatus 轮询 |

### 关键协议事实（继承知识库，已复核）
- 生产域名：业务 `gw.chagee.com`，getsk `gj-api.bwcj.com`；test 弃用（短信不投递 + test-gj 网关 404，1.8 万次重试风暴为证）
- 签名密钥双值：release=`9b83…f06e`，其余=`686c…76bd`（getSecretKey @0x8b35f0）
- `authorization` = 608B 裸 JWT 无 Bearer；失效码 401/`12320120400401`，**无 refresh**
- 登录短信：signFields=`[sendObj,sid,timestamp]`，requestEncryptFields=`[sendObj,mobile]`；login/sms 的 smsCode 明文无 sign
- 13 公共头（`chagee_protocol.BASE_HEADERS` 缺 `apv:1.0.0`，客户端已补）

---

## 三、模块设计（已批准方案）

```
scripts/
├── chagee_protocol.py      # 已有，密码学原语（不动）
├── chagee_menu_api.py      # 已有，游客菜单（复用）
├── chagee_client.py        # ✅ Phase 1 已完成：设备身份/公共头/getsk/请求管线/异常分级
├── chagee_login.py         # Phase 2：send_sms + login_sms + logout + 会话持久化
├── chagee_coupon_api.py    # Phase 4：三列表分类查询 + 兑换券识别
├── chagee_trade_api.py     # Phase 5：加购→试算→createOrder→0元 commitPay→cancelOrder 兜底
├── chagee_pickup_api.py    # Phase 6：getOrderList/Detail/WaitingInfo/Status → 取餐报表
└── run_pipeline.py         # Phase 7：六步流水线（断点续跑 + 确认门 + 验证）
```

请求管线（chagee_client.post 统一实现）：字段加密(requestEncryptFields) → 子集签名(signFields, 值=加密后终值) → POST → 包络校验(errcode≠"0" 分级抛异常) → 响应解密(responseEncryptFields)。

### 异常处理矩阵
| 场景 | 处理 |
|---|---|
| token 失效（401/12320120400401） | SessionExpiredError → 停止提示重登（无 refresh） |
| 短信 60s 限流 | RateLimitError → 等待后重试 |
| 加购售罄/库存变更（validSkuList/saleOutSkuList/stockChangeSkuList 分类响应） | 报告差异，人工换 SKU |
| settlePrice 非 0 元 | **中止下单**并报券未生效原因 |
| 下单/支付失败 | 不自动重试，回查订单状态防悬挂 |
| getsk 失败 | 重试 1 次后报错 |

### 安全边界
- 生产外向动作（发短信/真实下单/支付）逐项运行时确认开关；下单前打印订单预览（门店/商品/规格/券/金额）
- token/手机号全程脱敏；session 文件不进 docs；单次一单
- 0 元单会真实制作，门店由用户选择，不取自然作废（已告知）

---

## 四、当前进度

| Phase | 内容 | 状态 |
|---|---|---|
| 0 前置自检 | mitmdump(8443)✅ reverse 443/9000✅ App release✅ token/uuid 导出→`session_seed.json`✅ | **完成** |
| 1 客户端骨架 | `chagee_client.py`：设备身份装载(seed uuid)/13头/getsk/whoami 自检 | **完成，冒烟通过** |
| 2 登录模块 | `chagee_login.py`：send-sms/login/logout/status | **完成，生产闭环通过**（见下） |
| 0b 用户补抓 | 用户云手机走查：自取选品加购→结算选兑换券(0元)→提交支付→取餐页；券列表/订单详情/订单列表 | **待用户执行**（操作指引见第六节；UI 坐标侦察已完成存档 output/ui_*.xml） |
| 3 游客城市+SKU | `run_features_234.py` [F3]：cityList→store/list→storeGoodsMenu(自取)→goods/detail→OrderTarget | **完成（2026-09-23 生产实测）** |
| 4 券查询 | `chagee_coupon_api.py`：effective/historical 双列表分类 + 兑换券识别 | **完成（生产实测）**；order-coupon-list 生产 404 未部署（gw/gj-api × 两前缀 × 空/实 storeNo 全验），下单券入口待 0b 补抓 |
| 5 下单 | 核心新开发，依赖补抓样本；自取参数族定案入 `run_features_234.PickupContext`（deliveryType 自取=1 为推断值） | 未开始 |
| 6 取餐查询 | getOrderList 有 wire 样本（含 pickupNo=T0133、待取餐状态）；getOrderDetail/getWaitingInfo/getOrderStatus 静态已知零样本 | 未开始 |
| 7 流水线+验证 | 六步串联 + 文档；[F2/F3/F4 段已通]（`run_features_234.py`，2026-09-23） | 部分完成 |

**Phase 1 冒烟证据**（2026-09-23 实测）：
```
device uuid: 029a1add-…-108d3 (source: seed)
sk: ZjczNDYw…len=24 plain_len:16      ← getsk 生产握手成功
whoami errcode: 0 | customerId: 1190…len=10 | nickName: Hi，茶友
```

**Phase 2 生产闭环证据**（2026-09-23 实测，功能1 完成）：
1. 账号手机号恢复：whoami `mobileEncrypt` → sk 解密 → 159****1290（与 KB 记录一致）
2. send_sms 请求管线对 test wire 样本**字节级回归通过**（键序/加密/签名全一致）
3. 生产 `/user-client/message/send` 纯 Python 构造被接受：`errcode=0, sendFlag=true, action=pass`
4. `/user-client/auth/login/sms` 成功，**响应结构首次定案**：`data={customerId, accessToken(608B 裸 JWT), refreshToken, firstLogin, dispose}`——修正 KB"无 refresh"表述（存在 refreshToken 字段，客户端是否使用待查）
5. token 提取→session.json 持久化→**冷启动新进程** whoami `errcode=0` 同账号 ✅
6. 单会话语义实锤：协议登录使 seed 旧 token 立即失效（whoami 401）
7. 耗时 2 轮短信（第一轮验证了服务端接受但提取器候选键缺 accessToken，已修正）

**Phase 3/4 实测记录**（2026-09-23，`run_features_234.py --city 杭州`）：
```
F2 自取参数族断言通过（saleType=1/saleChannel=2/businessType 门店域"1"/下单域 2/orderType 0/deliveryType 1*推断）
F3 游客链: 359 城→杭州 198 店→CN11109 首店→15 分类/52 SPU
   →选中 浓抹香草籽 SKU 1305903142662811649 [杯型:中杯] ¥22.00 库存 9999 → OrderTarget 就绪
F4 登录态: 可用 3 / 历史 3（均 20 元代金券 bizType=1）；饮品兑换券 0 张
```
**Phase 3/4 新增事实**（2026-09-23）：
- 券/SPU 响应字段以实测为准：SPU 名为 `name`（非 spuName）、SKU 规格在 `specOptionInfos[].specOptionName`；菜单首位是套餐占位（0 元无规格），选品逻辑已过滤
- historical-list 的 `status` 入参生产未生效（0/1/2 同结果），分类以列表归属为准
- 账号无饮品兑换券 → Phase 5「0 元」需先 `coupon-exchange`（/api/promotion-web 前缀，体 {exchangeCode}）或改代金券差额方案，待用户决策
- 设备公网 IP 轮换（125.109.27.7→39.174.221.6），serial 以 `adb devices` 实时为准

**Phase 2 静态交叉验证**（blutter 汇编级证据，与上述实测互相印证）：
- token 键名 `accessToken`：`LoginToken.fromJson @0x8d89c0`（token_entity.dart:444-473）→ `SpUtil::putString("login_userLoginToken", accessToken)`（phone_login_bloc.dart:3207-3222）——**汇编级+生产实测双重定案**
- 客户端解析模型仅 3 键（newUser:int/token:String/accessToken:String），生产响应多出的 customerId/refreshToken/firstLogin/dispose 客户端直接忽略——两侧无矛盾
- 登录响应**无 responseEncryptFields**（getEncryptExtra 未传 → null），token 明文下发，无需 sk 解密（实测一致：直接提取成功）
- logout 请求体为空 `{}`（SettingBloc::_logOut @0x9cabd8 无 .data() 调用），身份全靠 Authorization 头——与 chagee_login.py 实现一致
- oneKey 登录体：`{accessToken:<SDK令牌>, token, loginType:"onekey", storeNo}` 全明文（login_service.dart:4600-4655，备选登录方式备查）
- userInfo/query 的 responseEncryptFields=["mobile","email"]（pp.txt:1998-2001）——解释了手机号密文下发与本项目解密恢复手机号的可行性

---

## 五、风险与未知（诚实标注）

1. createOrder 请求体字段族庞大（下单页 73 项状态字段族），一次样本+静态核对若有缺漏（防重放 nonce 等）按错误码迭代
2. 0 元支付通道行为未知（可能免 commitPay 直接成单，以样本为准）
3. ~~成功 login/sms 的响应 token 字段名待补抓样本确认~~ **已定案（2026-09-23 生产实测）：`accessToken`**；refreshToken 字段存在但客户端使用方式待查
4. ~~重登会换 token，可能踢云手机 App 会话~~ **已实锤：单会话语义，协议登录使旧 token 立即失效**
5. 云手机 App 当前登录态已被协议登录踢掉（App 内需重新登录才能继续操作 UI；纯协议链路不受影响）

---

## 六、补抓操作指引（Phase 0b，待用户执行）

**前提**：抓包链路已就绪（勿断 adb）；App 已登录、release 环境。
**在云手机上按序操作**（每步之间稍微停顿 2-3 秡便于切分流量）：

1. 首页 → 定位/选择一家门店（**自取**）→ 进入门店菜单
2. 选一款商品 → 选好规格 → 加入购物车
3. 购物车 → 去结算 → 结算页选择**饮品兑换券** → 确认金额变为 **0 元**
4. 提交订单 → 完成 0 元支付 → 停在**取餐页**（有取餐码的页面）
5. 返回点开：我的 → 优惠券（各 tab 都点一遍）；订单列表 → 该订单的**订单详情**（取餐进度页）
6. 【最后做，会退出当前登录】我的 → 设置 → 退出登录 → 用短信验证码重新登录一遍（收到验证码正常输入）

完成后告知，我从新流量中提取六域 wire 样本（含 trade 接口的 sign/加密 extra 配置反推），继续 Phase 2-7。

---

## 勘误 2026-09-26（wire 实测）

- **下单域 businessType 定案为 1（int），非旧记 2**：两笔 createOrder（差额单与零元单）wire 实测 `orderBizInfo.businessType = 1`，settlePrice 的 `settleBizInfo.businessType` 同值亦为 1。覆盖下列旧表述：
  - 第四节「Phase 3/4 实测记录」F2 行中「businessType 门店域"1"/下单域 2」——「下单域 2」为当时静态推断值，以本勘误为准；
  - `scripts/run_features_234.py` `PickupContext.BIZ_TYPE_ORDER` 已同步改为 1（TRADE_PARAMS.businessType 随之 =1）。
- 不受影响项：门店列表/详情域 businessType 仍为 String `"1"`（menu 域）；下单券域 order-coupon-list 静态体 `businessType:2` 为另一端点（生产 404 未部署），与 createOrder 无关，勿混用。
