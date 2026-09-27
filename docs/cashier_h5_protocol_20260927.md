# 支付宝 H5 收银台协议链定案（茶姬 App 内嵌 · 云手机实付双样本）

> 日期 2026-09-27 | 依据：capture/chagee_native_phase0b_20260926.flows 两条完整实付链（20 个 API 调用点、
> 请求体全量明文 + 关键响应明文）+ 当日三次实时捕获（17:14/17:23/17:32 session 铸造观察）+ 一次现场实付
> （17:39-17:42，经 restore_webview_proxy 隧道完成）。提取存档：output/cashier_chain_full_20260927.json。

## 一、链路全景（8 步）

```
① cashierRoutePay.htm?route_pay_from=h5&init_from=SDKLite&session=…&utdid=…&tid=…&cc=y
      │ App 内支付宝 SDK 拉起支付时经 mobilegw 铸造（每次拉起都换新 session，utdid/tid 设备级恒定）
      ▼ 302
② h5pay/landing/index.html?h5_request_token=<同session>&cookieToken=<32hex>&serverParams=<b64>
      ▼ 页面 JS 就绪
③ POST /wapcashier/api/cashierMain.json      {h5_request_token, query_params(cc=y&session&utdid&tid…)}
      resp: controlType="unified_login"（登录墙）+ 订单金额 bizData.orderAmount
      ▼ 登录二选一（均带 server_param + contextId + cookieToken [+ captchaToken 滑块]）
④a 账号密码：unifiedLogin(account+captchaToken) → toAccountLogin → accountLogin(pwd=RSA密文, pageToken)
④b 短信验证：unifiedLogin(account+captchaToken) → smsValidateLogin(smsCode=明文4位, h5payClientId)
      ▼ 登录成功后 server_param 升级：zid=07/94;user_id=2088xxx…;identityInfo={QUICK_MSECURITY_PAY,trade2000…}
⑤ POST /wapcashier/api/operationQuery.json   （状态/操作查询，服务端逐腿下发 pageToken/ndpt）
⑥ POST /wapcashier/api/cashierSwitchChannel.json  渠道选择；cashierSwitchAccount(Sel).json 付款账号切换
      ▼ 渠道组合语法 combinationIndex："BANKCARD&EXPRESS_DC&ICBC&23111102853214713&3"（银行卡快捷）
      │                                                    "MONEYFUND&MONEY_FUND&INST_ALIPAY&60127861932819410"（余额宝）
⑦ POST /wapcashier/api/cashierPay.json       ← 扣款提交腿（本系统永不自动执行）
      REQ 关键字段：spwd=<收银台 rsaPubKey RSA 加密的支付密码多块密文> + envData(设备环境) +
                    combinationIndex + pageToken + contextId + cookieToken + server_param(user_id)
      resp 成功：{bizError:false, controlType:"need_pay_query", rsaPubKey:<每单下发的SPKI公钥>,
                  umidToken, userId, wnd:{act:{name:"/cashier/payResult",type:"submit"},time:5}}
      resp 风控拒：{bizError:true, controlType:"cashier_error_follow_action",
                    bizErrorCode:"cred_dev_to_explain_page", ruleId:20230327215458042,
                    → render.alipay.com/p/c/180020570000023463/index.html（"请更换常用设备"解释页）}
      ▼ 按 wnd 引导 5 秒后
⑧ POST /wapcashier/api/cashierPayResultQuery.json → 支付成功（returnUrl 携带签名
      alipay_trade_app_pay_response code=10000，2026-09-26 autopay 文档已证）→（可选）accountLogout
```

## 二、字段族谱

| 字段 | 来源 | 生命周期 | 说明 |
|---|---|---|---|
| `session` | mobilegw 铸造（SDK 拉起时） | **逐次拉起逐次更换**，短窗有效；过期 → 收银台 200 原地渲染"访问已超时"(mobileclientgw-42-92xx，非 302) | 三样本同 utdid/tid 不同 session 实证 |
| `utdid` / `tid` | 设备/隧道标识 | 设备级恒定 | 与 session 组成 cashierRoutePay 三元组 |
| `h5_request_token` | landing 302 下发 | = session 值 | cashierMain 入参 |
| `cookieToken` | landing 下发 32hex | 每收银台会话 | 全链随行 |
| `server_param` | 每腿响应回传演进 | **base64("zid=NN;[user_id=2088xxx;]ndpt=xxxx;cc=y;[identityInfo=…];tenantName=MZFBW3CN")**；登录前 zid=99 无 user_id，登录后 zid=07/94 带 user_id+identityInfo；ndpt 每腿递进 | 状态机的载体 |
| `contextId` | mobileclientgw 铸造 | 每收银台会话 | `RZ42…mobileclientgw99RZ44` 形态 |
| `pageToken` | 服务端逐腿下发 | 逐腿 | accountLogin/cashierPay 必带 |
| `spwd` | 客户端构造 | 逐单 | 用 **cashierPay 之前响应下发的 rsaPubKey**（每单不同，SPKI hex）RSA 加密支付密码，多块密文 |
| `captchaToken`/`captchaBizNo` | 滑块验证码组件 | 逐登录 | unifiedLogin 必带 |
| `h5payClientId` | 收银台设备指纹 | 会话级 | smsValidateLogin / payResultQuery 随行 |

## 三、环境依赖（云手机侧，2026-09-27 定案）

1. **WebView 固定代理 127.0.0.1:8080**（App 层固化，系统设置无关）：链路无监听即全 WebView 断网
   （ERR_PROXY_CONNECTION_FAILED）。修复/复刻：`scripts/restore_webview_proxy.py`（PC 转发代理 +
   `adb reverse tcp:8080 tcp:18080`）——**每次用云手机收银台前先跑**。
2. 链接实时提取：`scripts/extract_cashier_link.py --follow --feed`（flows 扫描）或 WebView DevTools
   通道（webview_devtools_remote socket + adb forward + /json，秒级、零依赖，三次实证）。
3. 诊断顺序沉淀：系统代理 5 键 → hosts → WifiConfigStore(apex 路径) → LinkProperties →
   webview-command-line → **App 层固化代理（本次真因）**。

## 四、纯协议系统复用边界（与 pay_scenarios_design_20260926 §二一致并细化）

| 能力 | 可否 | 依据 |
|---|---|---|
| 收银台 URL 构造/获取 | ✅ | build_cashier_url + 凭证实时捕获（DevTools/flows）；本系统已集成 cashier-url 回填端点 |
| 收银台状态只读探测 | ✅ | cashierMain.json 匿名可调（alipay_autopay.drive_cashier 已有） |
| 支付结果侧感知 | ✅ | 不依赖收银台——茶姬侧 getOrderStatus 轮询（watcher 已实现） |
| 自动登录收银台 | ❌ | 滑块 captchaToken + 短信/密码，人工环节 |
| **自动扣款（cashierPay）** | ❌ 永不 | spwd 需支付密码（rsaPubKey 每单下发）；设备风控 cred_dev_to_explain_page 实证拦新设备；结构上本系统禁调该端点（PaySubmitBlocked 不变） |
| 人工辅助半自动 | ✅ | 云手机拉起支付（隧道保活）→ 实时提取链接 → 人工输密码完成 → 系统侧 watcher 自动收口成单 |

## 五、当日实付验证（2026-09-27 17:39-17:42）

- 链路：选店(高德定位)→下单→拉起支付→cashierActivity→输密码→支付成功
- 全程经 restore_webview_proxy 隧道出网；收银台生态域名时间线见代理日志
  （mclient/collect/tscenter/micweb/render/mobileic/ynuf.aliapp.org）
- 本次为 App 原生下单（系统侧 token 已被云手机登录踢出，paid 收口链由茶姬 App 内完成）——
  系统侧 paid→取餐码→回传 链路验证仍待一笔系统内订单实付

## 六、样本与工具索引

- 全量链提取：`output/cashier_chain_full_20260927.json`（20 调用、请求体明文+server_param 解码）
- 原始 flows：`capture/chagee_native_phase0b_20260926.flows`（响应体多为 brotli 压缩存储）
- 实时捕获存档：`output/cashier_live_capture.json`（17:32 版含 landing 完整参数）
- 提取器：`scripts/extract_cashier_link.py`；隧道：`scripts/restore_webview_proxy.py`；
  DevTools 录制器（需多页跳转加固，本次仅录得入口两请求）：`scripts/capture_cashier_devtools.py`
