# 支付宝 H5 收银台链接拼装公式定案（本地实证）

> 日期 2026-09-27 | 依据：capture/chagee_native_phase0b_20260926.flows 逐字节取证（session×39 处、utdid/tid×64 处、
> mobilegw×36 处、安全 SDK 设备上报原文）+ 当日 4 个不同 session 的实时铸造观察。
> 配套文档：docs/cashier_h5_protocol_20260927.md（完整协议链）

## 一、链接形态

```
https://mclient.alipay.com/cashierRoutePay.htm
    ?route_pay_from=h5        ← SDK 常量：H5 收银台路由来源
    &init_from=SDKLite        ← SDK 常量：SDKLite 集成形态
    &session=RZZFB00…RZZFB00  ← mobilecashier 会话（服务端铸造，逐次更换）
    &utdid=ard6vj24ouoDAPwfC0RN0/hz   ← 安全 SDK 设备标识（AC2，设备级恒定）
    &tid=2e80dd74…3827aa      ← 安全 SDK 设备指纹（AC1，设备级恒定，64-hex）
    &cc=y                     ← SDK 常量
```

## 二、各段来源与生命周期

| 段 | 来源 | 生命周期 | 实证 |
|---|---|---|---|
| `route_pay_from` / `init_from` / `cc` | 支付宝 SDK 内部常量 | 永久 | 全部样本同值 |
| `utdid`（=deviceData.AC2） | **APPSecuritySDK-ALIPAYSDK**（v3.4.0.202506100708）注册的设备标识，存 App 私有存储，随 `alipay.security.device.data.report` 上报 | 设备级恒定 | flows@548355 设备上报原文；两天所有链接同值 |
| `tid`（=deviceData.AC1） | 同上（安全 SDK 设备指纹） | 设备级恒定 | flows@548270；同上 |
| `session` | **拉起支付瞬间**，SDK 经 mobilegw（mcpay 应用层加密 RPC）向支付宝服务端铸造 mobilecashier 会话 | **逐次拉起逐次更换，短窗有效**；过期后收银台 200 原地渲染"你的访问已超时"(mobileclientgw-42-92xx，非 302) | 昨日 3 个 + 今日 4 个不同 session；session 在 flows 中首次可见即 302 响应头（h5_request_token） |

## 三、安全上报原文（flows@548270，节选）

```
operationType=alipay.security.device.data.report
requestData=[{"bizData":{"apdid":"eYOIkpU3…","apdidToken":"7EKcSbYo…","dynamicKey":"y+7H2…"},
  "deviceData":{
    "AA1":"com.chagee.application.cn",        ← 宿主 App
    "AA2":"1.0.3",                            ← 宿主版本
    "AA3":"APPSecuritySDK-ALIPAYSDK",         ← 安全 SDK
    "AA4":"3.4.0.202506100708",               ← SDK 版本
    "AC1":"2e80dd74…3827aa",                  ← 链接里的 tid
    "AC2":"ard6vj24ouoDAPwfC0RN0/hz",         ← 链接里的 utdid
    "AC4":"8cbfb4b7…"}}]
```

## 四、关键结论

1. **链接与 orderStr 无关**：orderStr 只决定"付哪单"；链接的 session 是拉起支付时服务端现铸的。
2. **离线拼造不可能**：utdid/tid 可长期复用（设备凭据），但 session 只能从"App 拉起支付"这个动作实时产生。
3. **mobilegw 铸造通道不可读**：36 处 mobilegw 流量经 mcpay 应用层加密，session 在其中流转但无明文；
   反编译 smali 亦无 cashierRoutePay 明文（URL 组装在 SDK 内部）。
4. **工程正解 = 实时捕获**：
   - WebView DevTools 通道（webview_devtools_remote socket → adb forward → /json）秒级拿到当次链接；
   - flows 扫描（scripts/extract_cashier_link.py，tnetstring 长度帧切片）；
   - 捕获后回填：POST /api/ops/accounts/{id}/orders/{order_no}/cashier-url（短窗内浏览器可直开）。
5. **判活标准**：GET 该链接 → 302 落到 `h5pay/landing` = 会话有效；200 原地渲染出错页 = 已过期。

## 五、环境依赖（云手机侧）

- WebView 固定代理 127.0.0.1:8080（App 层固化）：需 `scripts/restore_webview_proxy.py`（PC 转发代理 +
  `adb reverse tcp:8080 tcp:18080`）保持运行，否则收银台 ERR_PROXY_CONNECTION_FAILED。
- 详见 [[cloud-phone-network-fix-20260927]]（记忆）与 docs/cashier_h5_protocol_20260927.md §三。
