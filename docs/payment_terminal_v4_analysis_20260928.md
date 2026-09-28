# 支付终端 V4.0 自动支付逆向分析（2026-09-28）

> 样本：`C:\Users\Administrator\Desktop\支付终端V4.0(1)\支付终端[解压到桌面]`
> 方法：易语言 PE 字符串提取（GBK）+ 附属文件（cookies/LocalStorage/配置）形态分析，未脱壳未调试。
> 结论可信度：字符串证据充分，个别拼接顺序为推断（已标注）。

## 1. 技术栈与总体架构

| 组件 | 证据 | 职责 |
|---|---|---|
| 易语言 GUI | krnln.fnr + *.fne（commobj/e2ee/eAPI/eCompress/EThread/shell/spec/wke） | 主程序、多线程、加密压缩、HTTP |
| miniblink 内核 | node.dll + wke.fne + LocalStorage/缓存目录 | 内嵌浏览器（截图中央的支付宝页面） |
| WinHttp.WinHttpRequest 5.1 | 字符串 | 管理端 API 调用（无浏览器） |
| ScriptControl(JScript) | 字符串 | 运行内嵌 JS：URI 编解码 + jsbn RSA |
| 雷电模拟器 9 | 微信App支付使用前请看.txt | 微信支付通道（另一条腿，模拟器跑微信 v8.0.2） |

它是**上游发单平台（"管理端"）的接单支付终端**：登录平台拉待付订单 → 取支付参数 → 在支付宝侧完成真实扣款 → 回平台确认。支付宝侧有**两条并行腿**：① 内嵌浏览器 DOM 自动化（主通道，即截图界面）② 纯 HTTP wapcashier API 链（协议通道，免浏览器）。

## 2. 管理端（上游平台）协议

均为 `WinHttp` + `Content-Type: application/x-www-form-urlencoded`：

- `POST /order/capi/login`，体 `{"number":"<管理帐号>","password":"<管理密码>"}` → 登录态
- `GET /order/capi/resource/m/pay/list` → `content.payList[i].payType` 待付订单列表
- `POST /order/capi/resource/m/order/topay`，体 `{"payType":"126","orderId":"…","successUrl":"…"}` → **content.payParams.parameters.***（method/app_id/charset/biz_content/sign/sign_type/version/timestamp/return_url/notify_url + action）与 **content.payParamStr**（整串支付参数）；`content.orderInfo.orderId/desc`
- `GET /order/capi/resource/m/user/baseinfo/chaxun`（`{"phone":…}`）→ `content.usablebalance` 界面"余额:"
- `GET /order/capi/resource/m/sys/home/ordertips1` → 订单数提示
- `GET /order/order/detail/<id>` → H5 订单详情页

**要点**：`content.payParamStr` 与我们系统的 `pay_param_str`（pay_param_records，2026-09-28）是同一概念——平台下发支付参数串、终端只负责支付。我们的系统等于把"上游发单平台"也自研了（settle→createOrder 自产 order_str）。

## 3. 支付宝通道 A：内嵌浏览器 DOM 自动化（主通道）

### 3.1 登录态模型（关键资产）
- 首次**人工**在内嵌浏览器登录支付宝（"支付宝登录超时/需要手动登录！才可继续挂机！"——截图的 Hi,你好 页即登录态检查页）。
- 会话持久化为 Netscape 格式 `cookies.dat`（miniblink 导出）：**ALIPAYJSESSIONID / ctoken / zone / spanner / jsh_t_c_e / j_s_***；代码内拼装键名集合 `ALIPAY_WAP_CASHIER_COOKIE`（ALIPAYJSESSIONID、ctoken、zone、awid、spanner、JSESSIONID）。
- LocalStorage 存 mclient.alipay.com 侧 j_s_* 会话标记与 yuyan 监控配置（miniblink 自有 `key--mb-sep--\nvalue` 文本格式，非 SQLite）。
- "清除登录"按钮 = 删 cookies.dat + LocalStorage。

### 3.2 取收银台入口（半协议）
topay 的 payParams.parameters.* 拼成表单串 → 以 Safari 5.1 UA GET 跟随 `Location:` → 从最终 URL/响应提取 `h5_request_token=`、`query_params=`、`referer=`、`app_name=`、`targetDispatchSystem=`、`serverParams=`（失败分级文案：取支付宝链接失败1~6）→
`POST https://mclient.alipay.com/wapcashier/api/cashierMain.json`（体含 `h5payClientId`、`device:{ua,1600×900,4核}`、`targetDispatchSystem:"unitradeprod"`、`serverParams`）→ 取 `params.contextId` / `params['server_param']` 与 `data.bizData.h5RouteToken`（必要时 `h5ContinuePay.json` 续路由）→
**内嵌浏览器打开 `https://mclient.alipay.com/h5pay/cashierPreConfirm/index.html?server_param=<…>`**。

### 3.3 页面自动化（wke 注入 JS，选择器实录）
```
金额/状态读取      .h5RouteAppSenior__h5pay (textContent)
展开支付渠道列表   .c-pay-channel__channel → .c-pay-channel__val--click
渠道条目遍历       function Getshuliang(){return document.getElementsByClassName('channelMain__name').length}
                   .channelMain__name（按名称匹配「账户余额/余额宝」= 界面"支付方式"下拉）
确认付款按钮       .cashierPreConfirm__btn（disabled / .adm-plain-anchor 检测）
密码弹层判定       .adm-center-popup-wrap + .pwdValidate__title == "输入支付密码"
支付密码注入       .my-passcode-input / .my-passcode-input-native-input (value)
```
- 渠道下拉全集：支付通道1/2/3、账户余额、余额宝、零钱、零钱通（后两个属微信通道）。
- 结果判读：`cashierPay.json` → `data.wnd.msg`（"订单已付款成功，请勿重复提交。"）/ `data.bizData.placeholder`/`errorMsg`（"过期了"）；轮询 `cashierPayResultQuery.json` → `data.bizData.modules[0].successTitle/actualPaid`；"余额不足"→支付异常；`支付间隔 2 秒`=轮询节拍；"链接打乱"=打乱管理端链接次序（防平台侧顺序风控，推断）。

## 4. 支付宝通道 B：纯协议（免浏览器，Python 化最佳参照）

```
POST /wapcashier/api/refreshNoAuth.json      → data.userId / orderAmount / originalCost /
                                               payTool / payments[]
POST /wapcashier/api/cashierSwitchChannel.json (fromCashier:true)
                                             → data.bizData.payments[i].name/.disable/
                                               .params.combinationIndex（按名称选渠道）
                                               data.rsaPubKey   ← 支付密码加密公钥
JScript 内嵌 jsbn RSA（window={};navigator={} 垫片）→ spwd = RSA(rsaPubKey, 支付密码)
POST /wapcashier/api/cashierPay.json         → 体含 t/ua/combinationIndex/spwd
POST /wapcashier/api/cashierPayResultQuery.json → successTitle/actualPaid 确认到账
```
另有 mobilegw mcpay RPC 腿：`http://mobilegw.alipay.com/mgw.htm` + 头 `Operation-Type: alipay.msp.cashier.dispatch.bytes / AppId: TAOBAO / des-mode: CBC`，体 `api_name: com.alipay.mcpay, namespace: com.alipay.mobilecashier, device: vivo y51a`，UA `Msp/15.2.8 (Android 5.1.1…)`——与本项目 `scripts/alipay_msp_client.py`（SDK 15.8.35）同族，版本更旧；响应 `Decrypt('…')` 解密取 `form.content`，回调形态 `js://wappay('…')` + `resultStatus`。

**结论**：支付宝 H5 收银台的完整支付可以不依赖浏览器——登录 cookies + ctoken(CSRF) + rsaPubKey 加密支付密码即可纯 HTTP 完成；该工具仍以浏览器为主通道，推断为风控兼容兜底。

## 5. 辅助件

- `缓存/数字识别库.txt`：0-9 点阵字模（OCR 数字），用于页面数字识别（验证码/金额，具体挂载点未深挖）。
- `mp3.run`：提示音；`配置.ini`：仅 `[登录配置]是否清除1`；多开需整目录复制（LocalStorage/cookies 按目录隔离）。
- `Wq0V7EhkX5cDALGHoSfXyDQQ`：h5payClientId 缺省值候选（未验证）。

## 6. 对本项目（霸王茶姬系统）的映射与启示

1. **浏览器支付 Python 脚本的形态选择**：优先纯 HTTP 版（§4 链路）——我们已有 `pay_param_records`（cashier_url + session/utdid/tid），缺的资产只有**支付宝 H5 登录态 cookies**（ALIPAYJSESSIONID/ctoken/zone/spanner）。可复用该工具思路：内嵌 WebView 人工登录一次 → 持久化 cookies → 之后脚本轮询 pay_param_records 逐单支付。
2. **浏览器兜底通道**：若纯协议触发风控，退化为真浏览器 + §3.3 的选择器链（cashierPreConfirm 页 + 支付密码 DOM 注入）；我们已捕获的真实样本 URL（cashierRoutePay.htm?session=…）即该页链族入口。
3. **ctoken**：随 ALIPAYJSESSIONID 同发，wapcashier POST 的 CSRF 依据，必须与 cookies 同源保存。
4. **轮询与防重**：`data.wnd.msg` 的"请勿重复提交"与 successTitle 判定可直接借用；支付截止（我们 pay_deadline=10min 钳制）内 2s 节拍轮询足够。
5. **合规提醒**：该链路执行真实扣款（账户余额/余额宝），脚本化前须限定白名单订单与金额上限；支付密码 RSA 加密仅是传输保护，密码本体切勿落库/落日志。
