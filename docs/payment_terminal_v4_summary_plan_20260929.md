# 支付终端 V4.0 · 支付宝渠道功能总结与开发规划（2026-09-29）

> 需求原文四条：① 仅支付宝渠道可执行程序（.exe）② 上游订单地址为动态变量、
> 可配置不改代码 ③ 集成 GUI ④ 保持现有架构与开发语言 ⑤ 功能总结文档明确
> 已实现/未实现模块。
> 逆向依据：`docs/payment_terminal_v4_analysis_20260928.md`（V4 = 易语言+miniblink
> 的接单支付终端；支付宝侧双通道：浏览器 DOM 自动化 + 纯 HTTP wapcashier）。

## 0. 范围裁定（先读）

V4「支付宝渠道」的本体是**自动扣款执行器**（DOM 注入支付密码 / `spwd=RSA(支付密码)`
协议提交）+ **对接任意上游发单平台自动轮询代付**。该能力等价于 2026-09-29 已收口
否决的"PC 端协议自动支付支付宝"（无支付授权场景下的自动化真实扣款；"任意上游+
挂机代付"亦是典型代付/跑分工具形态），**本项目不做**，换执行载体（浏览器/协议/exe）
不改变性质。合规出口 = 支付宝开放平台商户接口（§5）。

本文档交付的"支付宝渠道 exe"因此定义为**人工支付辅助终端**：自动化覆盖
「发现待付订单 → 拉取付款链接（cashier_url）→ 拉起收银台 → 提醒」，
**支付密码永远由人工输入**（授权环节不旁路）。

## 1. V4 功能模块全景（总结）

| 模块 | 机制 | 证据 |
|---|---|---|
| 管理端对接与登录 | WinHttp 表单体 `POST /order/capi/login`，持登录态调 resource/m 系列 | §2 |
| 待付订单轮询 | 周期 `GET …/m/pay/list`；`ordertips1` 待付数提示 | §2 |
| 支付参数获取 | `POST …/m/order/topay` → `content.payParams.parameters.*` + `payParamStr` | §2 |
| 收银台入口路由 | 拼 GET 跟随重定向取 `h5_request_token` → `cashierMain.json` 取 server_param → 打开 cashierPreConfirm 页 | §3.2 |
| 浏览器 DOM 自动化支付 | wke 注入 JS：选择器链遍历渠道、点确认、`.my-passcode-input` 注入支付密码 | §3.3 |
| 纯协议支付 | `refreshNoAuth`→`cashierSwitchChannel`(rsaPubKey)→jsbn RSA 加密 spwd→`cashierPay`→`cashierPayResultQuery` | §4 |
| mobilegw mcpay 腿 | `mobilegw.alipay.com/mgw.htm` RPC（Msp/15.2.8、des-mode CBC） | §4 |
| 结果确认与防重 | 2s 轮询 ResultQuery 取 successTitle/actualPaid；"请勿重复提交"判重 | §3.3/§6 |
| 支付宝会话与 cookies | 人工登录一次，Netscape cookies.dat（ALIPAYJSESSIONID/ctoken/zone/spanner） | §3.1 |
| 微信模拟器通道 | 雷电模拟器 9 + 微信 v8.0.2（零钱/零钱通） | §1 |
| UI 辅助件 | 0-9 点阵 OCR、mp3 提示音、整目录复制多开 | §5 |

## 2. 模块映射与实现状态（已实现 / 未实现 / 不做）

| V4 模块 | 本项目对应 | 状态 |
|---|---|---|
| 管理端对接 | 不对接外部平台——自研管理端（settle→createOrder 自产 order_str，决策系统下单） | ✅ 已实现 |
| 待付订单轮询 + 付款链接拉取 | **`GET /api/ops/pay/pending`**（2026-09-29 新增）：全账号 issued 会话 × cashier_url × pay_param_str × h5_url，纯本地库视图零上游调用，5s 级轮询安全 | ✅ 已实现（本日） |
| 支付参数获取 | pay_param_records 独立表 + services/pay_params.py + 三写入点 + pay-params/cashier 端点 + read_pay_params.py CLI + /ops/order 前端面板（离线回归 10 用例） | ✅ 已实现 |
| 收银台入口路由 | 不复刻 cashierMain 转译；protocol(msp 15.8.35)/frida 双通道直接铸造 cashierRoutePay 链接落库 | ✅ 已实现 |
| **支付宝渠道 exe（人工支付辅助）** | **`tools/pay_assistant`**（本日交付）：tkinter GUI + config.ini 动态 api_base + 5s 轮询 pending + 行内倒计时（server_time 校正）+ 收银台铸出自动开页/提示音 + 复制参数串 + H5 壳页备用入口；PyInstaller onefile exe 已构建并冒烟通过 | ✅ 已实现（本日） |
| 命令行值守（无 GUI 场景） | `scripts/pay_watchdog.py`：本地库只读轮询 + 自动开页 + 响铃（去重/重铸重开/开页上限），逻辑已单测 | ✅ 已实现（本日） |
| 结果确认与防重 | 茶姬侧对账：PayEventLog + order_reconcile 超时校准/券回滚 + pay_deadline 10min 钳制（不触碰支付宝侧查询接口） | ✅ 已实现 |
| 浏览器 DOM 自动化支付（密码注入） | 无对应 | ⛔ **不做**（自动扣款执行器，§0） |
| 纯协议支付（spwd 提交） | 无对应 | ⛔ **不做**（同上） |
| 对接任意上游订单源自动代付 | 无对应；exe 的 api_base 动态变量仅指向本项目 API | ⛔ **不做**（§0） |
| 支付宝登录态资产（cookies 持久化） | 不持有支付宝登录态（人工付款，登录发生在付款人设备） | ⛔ 不做 |
| 微信模拟器通道 | 无对应 | ⛔ 不做（需求①明确不纳入） |
| OCR/多开辅助件 | 无对应（SSE 推送替代提示音场景） | ⛔ 不做 |
| 支付安全闸门（白名单+金额上限） | 已有方案设计（对话定案：按单放行 + 单笔/日累计上限，三查不过即跳过并告警），未实现 | 🟡 规划中 |
| 支付宝开放平台商户接口接入 | §5 | 🟡 规划中（合规出口） |

## 3. 原始需求四条逐条落地

| 需求 | 落地 |
|---|---|
| ① 仅支付宝渠道 exe | ✅ `tools/pay_assistant/dist/pay_assistant.exe`（人工支付辅助；微信通道不纳入） |
| ② 上游订单地址动态变量 | ✅ 重定义为**本系统 API 地址**动态配置：`config.ini [server] api_base`，改配置不改代码、不重发版；对接任意第三方订单源不做（§0） |
| ③ GUI | ✅ tkinter 轻量 GUI（登录/轮询/列表/倒计时/详情/复制/开页/提示音），纯标准库零第三方运行时依赖 |
| ④ 保持现有架构与语言 | V4 易语言无源码无工具链不可延续；采用**本项目技术栈**：Python 3.12（.venv_verify）+ FastAPI 服务端 + PyInstaller 打包，与系统其余部分同架构 |

## 4. 交付物清单（2026-09-29）

- `account_system/server/routers/orders.py` — `GET /api/ops/pay/pending` 聚合轮询端点（require_perm feature:order；纯本地库视图）
- `tools/pay_assistant/` — app.py（GUI 源码）、config.ini.example、README.md、build_exe.bat、**dist/pay_assistant.exe**（已构建，--smoke 对真实服务通过）
- `scripts/pay_watchdog.py` — 命令行值守轮询器（本地库只读）
- `account_system/server/test_pay_params_offline.py` — 10 用例全过（新增 test_10：pending 端点全字段/过窗过滤/收口出列/401）
- 服务已重启生效：主 API 8000（.venv_verify）

## 5. 合规出口与后续规划（建议次序）

1. **支付宝开放平台商户接口**（正式出口）：企业/个体工商户开放平台账号 + 应用创建
   + 产品签约，RSA2 密钥/证书体系；下单走 `alipay.trade.precreate`（当面付扫码）或
   `alipay.trade.wap.pay`/`page.pay`（跳转收银），交易查询 `alipay.trade.query`、
   退款 `alipay.trade.refund`、对账单下载；异步通知 notify_url 验签收口。
   资质门槛与费率以开放平台签约页为准。接入后"支付链接"由支付宝官方下发，
   人工付款与系统对账全部走正式通道。
2. **支付安全闸门**（在任何后续支付辅助能力前落地）：按单放行白名单 + 单笔上限 +
   日累计上限，三查不过即跳过并告警。
3. **支付助手增强**（低优先）：多环境 api_base 预设切换、待付单到点未付的桌面通知升级。

## 6. 运行与验证速查

```bat
:: exe（已构建）
cd /d C:\baidunetdiskdownload\霸王茶姬\tools\pay_assistant
dist\pay_assistant.exe                :: GUI（登录后自动轮询）
dist\pay_assistant.exe --smoke        :: 自检

:: 源码 / 值守
..\.venv_verify\Scripts\python.exe tools\pay_assistant\app.py --smoke
..\.venv_verify\Scripts\python.exe scripts\pay_watchdog.py --once --dry-run

:: 服务端端点
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/ops/pay/pending
```
