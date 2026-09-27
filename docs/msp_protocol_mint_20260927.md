# 纯协议收银台铸造定案（msp/mcpay 通道复刻）· 2026-09-27

## 结论

**脱离云手机设备、纯 HTTP 协议铸造支付宝 H5 收银台链接已全链路打通并在线实证**：

```
orderStr（茶姬 createOrder/continuePay 纯协议已备）
   │  scripts/alipay_msp_client.py  ←→  https://mobilegw.alipay.com/mgw.htm
   │     Operation-Type: alipay.msp.cashier.dispatch.bytes
   ▼
session（服务端铸造，响应体顶层 session 字段直出）
   ▼
https://mclient.alipay.com/cashierRoutePay.htm?session=…&utdid=…&tid=…&cc=y
   ▼ 302 → h5pay/landing（probe 判活通过，浏览器可直接打开付款）
```

在线验证记录（2026-09-27，生产网关）：`success:true` + session 下发 + 链接判活
alive，全程 **1.1 秒**（frida 云手机路径预算 45s+30s）。过期单（time_expire 已过
24h）同样铸出 session——session 铸造只校验 orderStr 签名，业务时效在收银台层拦。

## 加密协议规格（逆向自 APK 内嵌支付宝 SDK 15.8.35，全 Java 无 native）

APK 内**不存在任何 alipay native so**（libmsp/libsgmain 均无），msp 通道加密是
纯 Java JCE——密码学上完全透明。来源文件：`decompiled/jadx/sources/com/alipay/sdk/m/`。

**报文**（`%05d` 十进制 ASCII 长度前缀分段，m/s/c.java:253）：

```
请求 = frame( envelope_JSON明文140B ‖ RSA块128B ‖ 3DES密文 )
响应 = frame( envelope_JSON明文112B ‖ 3DES密文 )        ← HTTP 层另套传输 gzip
```

1. **会话密钥**：客户端每请求自生成 24 位 [A-Za-z0-9]（m/s/c.java:13，
   m/x/o.java:579）。**无握手、无协商、无服务端私钥依赖**。
2. **密钥上行**：RSA-1024 `RSA/ECB/PKCS1Padding` 加密密钥串成 128B 块
   （m/p/d.java）。公钥硬编码 m/n/a.java:15；**服务端可经响应
   envelope.data.params.public_key 轮换公钥**（m/s/e.java:128-147，落 prefs
   "trideskey"）→ 客户端复刻为 data/msp_runtime_pubkey.txt 自动跟随。
3. **body 加密**：明文 body JSON → gzip（Java GZIPOutputStream，头 10B 恒定）→
   `DESede/CBC/PKCS5Padding`，**IV 恒 8×0x00**（m/p/c.java：bArr[0] 永不赋值 →
   bArr[8..15] 全零）。密钥即会话密钥的 ASCII 字节。
4. **响应解密**：响应用**请求方自选的同一把密钥**回加密 → 自发自解。
   （抓包自证：同一流请求/响应密文首 8B 相同 = 同密钥+零IV+同 gzip 头首块。）

**envelope（明文头）**：
`{"data":{"api_name":"com.alipay.mcpay","namespace":"com.alipay.mobilecashier",
"api_version":"4.9.0","device":"<Build.MODEL>","params":{}}}` — 与抓包 flow[50]
逐字节一致（140B）。

**body（加密体内，m/s/e.java:75-94 字段序）**：
```
action:{"type":"cashier","method":"main"}   external_info:<orderStr 原文>
tid / user_agent / has_alipay:false / has_msp_app:false / app_key:2014052600006128
utdid / new_client_key / pa:"{com.chagee.application.cn#103}"
```

**HTTP 头**（m/r/b.java + 抓包）：`User-Agent: msp`、`AppId: TAOBAO`、
`Version: 2.0`、`content-type: application/octet-stream`、`msp-gzip: true`、
`des-mode: CBC`、`Operation-Type: alipay.msp.cashier.dispatch.bytes`、
`Msp-Param: trade_no=<orderStr 的 biz_content urlencode 原值>`（m/s/a.java）。

**响应 body 形态**（在线实证）：顶层 `success:true` + `session` 直出 +
`control_type:"need_phonelogin"`（收银台登录墙模板 QUICKPAY@cashier-phone-
login-flex）。SDK 旧路径 `data.form.onload[]` JS 片段（openWeb('…') 携带链接、
tid('t','k') 下发设备凭据）同为实现分支，均已支持。

**user_agent**（m/o/b.java:132-197，22 段 `;` 分隔指纹串）：`Msp/15.8.35
(Android <rel>;Linux <kernel>;<locale>;https;<w>*<h>;<textSize>;<16 段>)`
可选 `);;;<AT>` 反欺诈后缀。已知段用真值（Android 10 / NXT-AL10 / zh_CN /
1080*1812），未知段（imei/imsi/client_key/vimsi/vimei）按 SDK 降级公式构造
（hex时间戳+4位随机，w/a.java:45-47）——**在线验证证明该构造被生产网关接受**。

## 设备凭据（设备级恒定，可长期复用）

| 凭据 | 值 | 来源 |
|---|---|---|
| utdid | ard6vj24ouoDAPwfC0RN0/hz | AC2，data.report 明文 |
| tid | 2e80dd74…3827aa | AC1，data.report 明文 |
| apdid/apdidToken/dynamicKey | eYOIkpU3…/7EKcSbYo…/y+7H2z… | flow[48] 请求体明文 |
| client_key | （服务端下发，可空） | tid('…','…') onload 操作 |

默认内嵌于 `scripts/alipay_msp_client.py:DEFAULT_DEVICE`，可用
`account_system/data/msp_device_profile.json` 覆盖（tid 操作自动落盘）。

## 模块与接入

- **scripts/alipay_msp_client.py**：纯协议客户端。`mint_cashier_link(order_str,
  probe=True)` → `{ok, url, session, control_type, body}`；公钥轮换自动跟随；
  CLI：`python scripts/alipay_msp_client.py --order-str "..." --probe`。
- **services/cashier_mint.py**：`CHAGEE_MINT_PROVIDER=protocol(默认)|frida|auto`。
  protocol 纯 HTTP 不占全局锁可并发；frida 走原云手机路径；auto=protocol 失败
  降级 frida。回填/事件（cashier_updated source=protocol-mint）/判活/防抖全部
  复用原契约，下游（壳页/GET cashier/事件流）零改动。
- 离线测试：`tests/test_msp_client_offline.py`（11 项，envelope 与抓包逐字节
  比对）、`account_system/server/test_mint_provider_offline.py`（5 项，单独跑）。

## 边界与风险（诚实记录）

1. **收银台内交互（登录/扣款）不在纯协议范围**：need_phonelogin 后是短信/滑块/
   密码/风控（cred_dev_to_explain_page），与 frida 铸的链接行为一致——用户在
   浏览器完成（这正是系统设计：链接交付、人工付款）。
2. **设备指纹段为构造值**已被网关接受（2026-09-27 实证），但支付宝风控策略可能
   变化；mint_failed 事件 + auto 降级是对冲。
3. 过期单 session 可铸但收银台查无交易（预期）。**支付窗以官方为准**：支付宝
   time_expire=下单+30min 且 continuePay 顺延，但茶姬官方待支付单 paymentExpiry
   Timestamp=下单+10min、autoCancel、不随续付重置——系统所有 pay_deadline 写入经
   `pay_session.clamp_pay_deadline` 钳制到 min(支付宝截止, 下单+10min)，杜绝
   「10-30 分钟间支付、茶姬已取消、支付宝仍扣款成功而支付无回传」的空窗
   （2026-09-27 定案，test_pay_window_offline.py 10 项覆盖）。
4. 公钥轮换若发生将自动落盘跟随；若服务端换出非 X509 结构会记 mint_failed。

## 相关文档

- docs/cashier_link_assembly_20260927.md（链接拼装公式，"离线拼造不可能"结论
  已被本篇修正为"离线加密复刻可行"）
- docs/cashier_h5_protocol_20260927.md（收银台 8 步协议链）
- output/mcpay_wire_extract.json（设备凭据与请求头样本提取）
- output/extract_mcpay_wire.py（flows 提取脚本，tnetstring 含 `^` float 类型）
