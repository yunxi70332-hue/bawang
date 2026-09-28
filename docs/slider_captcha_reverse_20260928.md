# 茶姬登录滑块逆向（阿里云验证码 2.0 / FeiLin）— 2026-09-28

## 一、机制全貌（全部实证）

### 1. 滑块是什么
- **阿里云验证码 2.0（Aliyun Captcha）**，前端组件 **FeiLin 1.5.1**（`g.alicdn.com/captcha-frontend/FeiLin/1.5.1/feilin030.*.js`，入口 `o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js`）
- **不是数美滑块**。`smSessionId` 是茶姬服务端（其风控接了数美设备指纹 `fp-it.fengkongcloud.com`，代理日志实证）的挑战会话 ID，与滑块厂商无关
- CN 滑块页：`https://static.chagee.com/cdn-tools-transfer/chagee-cn-app/user/verify.html?sceneId=9nsud17h&language=zh`（prefix=`1w4yu5`，region=`cn`，mode=`popup`）
  - 该 URL 由 countryInfo 接口下发到 SP key `VerifySliderUrlApp`；SP 为空时兜底 SEA 版 `southeast-static.chagee.com/sg-c-cli/chagee-app/oversea/verify_sea_app.html?sceneId=hoipvzll`（region=sgp）
- App 内嵌滑块 = **InAppWebView** 加载该页（启动时预建 WebView，页面加载路径不经过 `android.webkit.WebView.loadUrl`——frida 基类钩零事件；chromium console 日志为铁证）
- 拼图交互：底图 296×200 自然宽（显示 300）+ 52px 拼片；**knob→piece 位移比 ≈ 0.677**（两端实测一致）；验证 = 拖拼片对准缺口

### 2. captchaVerifyParam（验证令牌）
- `success(captchaVerifyParam)` 回调产出，经 `ChageeCall.postMessage({funcID:'captchaVerify', param})` 桥回 App（App 注入的桥带 `if undefined` 保护，页面先定义者胜）
- **结构 = base64(JSON)**：`{"certifyId":"...","sceneId":"9nsud17h","isSign":true,"securityToken":"..."}`（securityToken 前缀跨会话相同——固定密钥加密特征）
- **一次性**：用户 00:08 手动过滑块的 token 重放 → 仍 challenge
- App 重发 = 原请求体 + `captchaVerifyParam` 字段 + 新 timestamp（重签），`blockParam` 保持"不验证"（verify_bloc.dart 0x8db5cc 实证）

### 3. 服务端判定
- 裸发（无 captchaVerifyParam）→ `errcode=0, sendFlag=false, dispose.action=challenge, smSessionId`
- `captchaVerifyParam=smSessionId` → 仍 challenge（错误）
- **PC 浏览器铸的真结构 token → 仍 challenge**（待验证的环境绑定假设：解滑块环境需与请求环境一致，或服务端校验维度更多）
- 滑块通过后（App 内）→ 短信真实投递（用户 00:08 实证）

### 4. 风控维度
- challenge 是**号码维度**：19241719504 被拦；同设备同 IP 19926070332 → pass+真实投递
- App 流量含数美指纹（fp-it.fengkongcloud.com）+ 网易易盾（da.dun.163.com）

## 二、已建成的工具链

| 工具 | 位置 | 用途 |
|---|---|---|
| repro_send_raw.py | scripts/ | 裸发抓 sendFlag/dispose 判定是否被拦 |
| repro_send_with_captcha.py | scripts/ | 带 captchaVerifyParam 重发实验 |
| serve_mitm.py | output/mitm/ | static.chagee.com MITM 服务器（改造版 verify.html：缺口分析+遥测+token 上报） |
| restore_webview_proxy.py（升级） | scripts/ | 转发代理 + `MITM_STATIC_PORT` 环境变量路由 static.chagee.com；`CLOUDPHONE_SERIAL` 可换设备 |
| 证书 | output/mitm/739f1c51.0 | 已装入手机系统证书库（static.chagee.com 专用，2038 过期） |
| spawn_probe.py 等 | frida/scripts/ | WebView 全事件探针 |

### PC 浏览器自动化铸 token（已跑通）
IAB 打开 verify.html → 注入 `window.ChageeCall` shim → domSnapshot/evaluate 取拼图 dataURL → 像素边缘对检测缺口（列梯度 top 边缘配对 42-60px 间隔）→ `cua.drag` 24 点 ease-out+jitter 拟人轨迹 → `window.__captured` 拿 token。**实测 1 次通过**（certifyId 1LEmM920tv）。

## 三、环境绑定假设（待最后一次验证）
PC 铸 token + PC 协议重发 = 被拒。两种解释：
1. **解滑块环境绑定**：token 校验绑定解題环境指纹/IP，须手机解+手机发
2. 服务端校验更多信息（如 smSessionId 与 token 配对、时间窗等）

**验证实验（下一步）**：用户在手机 App 里手动过滑块的瞬间，我通过 MITM/日志截获 token，**立即**用协议层重发。若通过→假设2排除，纯协议方案定型（手机铸+手机 curl 发：`adb shell su -c curl` 携带 PC 预构建的签名加密请求体，同 IP 出网）；若仍拒→假设1成立，需手机端闭环。

## 四、坑与备忘
- 云手机换 IP 重连后 **adb reverse 全丢**（含 8080→18080 代理隧道），App WebView 即断网——`CLOUDPHONE_SERIAL=新地址 python scripts/restore_webview_proxy.py` 重建
- 手机 Chrome **内置 DNS 绕过 /system/etc/hosts**（MITM 劫持 Chrome 失败的原因）；App WebView 走固化代理 127.0.0.1:8080，控制 PC 侧转发代理即可劫持其 HTTPS
- `input swipe` 的线性匀速轨迹被 FeiLin 行为检测识别（拼图对齐仍判失败）；多点 ease-out+抖动轨迹可通过
- kunlun CDN IP 每次解析漂移，iptables 锁 IP 不可行；劫持要走代理层
- pm clear App 会清 SP → 国家配置丢失 → 滑块页回退 SEA 版 URL
- 手机 tcpdump 有（/system/bin/tcpdump）；DNS 走 DoT(223.5.5.5:853) 看不到明文；App API TLS 到 gw.chagee.com 江苏边缘(58.221.40.8)
- App 登录页布局：手机号框 y≈400-465、获取验证码按钮 y≈640-715（中心 360,677）、协议勾选 y≈732（pm clear 后）、滑块轨道 y≈870-935
