# 支付倒计时时间戳同步机制设计（2026-09-28）

> 状态：**全文定稿**。§1 接口契约为唯一事实源（壳页 pay_cashier.html 与管理端前端按此实现）；
> §2–§4 为流程 / 前端 / 异常分析，§5 为测试与验证记录（自动化离线套件 + 手动冒烟清单），
> §6 为兼容性说明。
>
> 背景：旧实现把「下单时刻 + remaining_seconds」交给前端自行倒数，存在三类时钟漂移——
> 手机本地时钟不准、网络往返吃掉 remaining、以及双进程（主 API 8000 / 收银台 8010）
> 各自 `datetime.now()` 取值不一致。改造后所有时间锚点由**服务器权威时间**统一下发，
> 前端只做偏移校正与绝对时间倒数。

---

## 1. 接口契约（最终形态）

### 1.1 时间戳字段（JSON 响应通用字段）

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `server_time` | `int`（epoch **毫秒**）或 `null` | 服务器权威时间。前端时钟偏移校正：`offset = server_time − Date.now()`（多帧样本取最小偏移更稳）。`null` 仅出现在无支付会话的场景（见 1.2 的 `pay/` 响应），前端遇 `null` 回退本地时钟 |
| `pay_deadline_ts` | `int`（epoch **毫秒**）或 `null` | 支付截止绝对锚点 = **钳制后** `PaySession.pay_deadline`（`min(支付宝 time_expire, 下单+10min)`，见 `services/pay_session.clamp_pay_deadline`，官方 autoCancel 窗）。前端倒数：`剩余秒 = (pay_deadline_ts − (Date.now() + offset)) / 1000`。`null` = 无截止时间（前端不显示倒计时） |
| `remaining_seconds` | `int` 或 `null` | 兼容保留的相对秒数快照（响应落地瞬间的值，不再作为倒计时主锚点）。已过为 0；无截止为 `null` |

携带 `server_time` / `pay_deadline_ts` 的响应（实现落点）：

| 端点 | 场景 | 说明 |
| --- | --- | --- |
| `GET /pay/{token}/info` | H5 收银台展示数据 | 两字段随每次 info 刷新 |
| `GET /pay/{token}/status` | H5 状态轮询（2s 节流探针） | 每次轮询刷新权威锚点 |
| `GET /pay/{token}/events`（SSE） | 首帧 `sync` 与所有事件 data | 见 1.3 |
| `POST /api/ops/.../orders`（create partial） | 管理端下单差额单响应 | `_pay_link_payload` |
| `POST /api/ops/.../orders/{order_no}/pay`（manual/auto） | 人工/自动支付响应 | 同上 |
| `POST /api/ops/.../orders/{order_no}/continue-pay` | 续付响应 | 同上（钳制窗不随续付重置，deadline 不变） |
| `POST /pay/{token}/switch-full-price` 与管理端同名义端点 | 原价重下响应 | **新单**的 `pay_deadline_ts` |

无支付会话场景（`_pay_link_payload` 未铸会话，token 为空）：两字段均为 `null`。

**`expire_at` 语义变更（2026-09-28）**：`GET /pay/{token}/info` 的 `expire_at` 由
「PayAttempt 的支付宝 time_expire 原文（下单+30min）」改为「**钳制后 pay_deadline** 的
`"%Y-%m-%d %H:%M:%S"` 格式化」（无 deadline 为 `null`），与 `remaining_seconds` /
`pay_deadline_ts` 同源，消除「倒计时未走完、茶姬侧已 autoCancel」的自相矛盾展示。

### 1.2 状态字段

`PaySession.status ∈ issued | paid | cancelled | expired`（不变），随上述响应原样下发。

### 1.3 SSE 事件协议（`GET /pay/{token}/events`）

- `Accept: text/event-stream`；响应头 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 无效 token → HTTP 404（建立失败）。
- 连接建立即推首帧：

```
event: sync
data: {"server_time":1759000000000,"pay_deadline_ts":1759000600000,"status":"issued","remaining_seconds":540}
```

- 事件类型（`data` 结构与 sync 完全同构）：

| event | 触发 | 之后 |
| --- | --- | --- |
| `sync` | 连接建立 / issued 态每 5s 心跳 / deadline 语义变化（续付重铸后） | 流继续 |
| `paid` | 会话跃迁 `paid` | **推送后服务器关闭流** |
| `cancelled` | 会话跃迁 `cancelled`（手动取消 / 超时校准 / watcher / 原价重下） | 同上 |
| `expired` | 会话跃迁 `expired`（watcher 过期收口） | 同上 |

- 连接时已是终态：先推一帧 `sync`，紧接推对应终态事件并闭流。
- 客户端断线重连策略由前端决定（`EventSource` 原生自动重连；重连首帧 sync 即重新对齐时钟）。

### 1.4 进程间通知协议（`POST /internal/broadcast`，仅收银台进程 8010 挂载）

- 调用方：主 API 进程内 `services.pay_broadcast.notify_session_change`（`mark_session`
  CAS 收口成功后自动 fire-and-forget，`paid`/`cancelled`/`expired` 均通知；timeout 0.3s，
  任何失败仅 debug 日志）。
- 请求：`POST http://127.0.0.1:8010/internal/broadcast`，头 `X-Internal-Token:
  <data/internal_broadcast.secret>`（`secrets.token_hex(16)`，双进程同机同文件共享，首读惰性创建），
  体 `{"order_no": str, "status": str}`。
- 响应：200 `{"ok": true, "order_no", "status", "event"}`；token 失配 401；会话不存在 404。
- 服务端以**库中最新状态**分派 SSE 事件（不信任请求体声称的 status，迟到旧通知不会把终态倒拨）。
- 兜底：收银台进程另有 1s 轮询比对 `(status, pay_deadline_ts)` 变化补推——通知只是加速器，
  丢失最终一致；主 API 重启窗口亦由该轮询覆盖。

### 1.5 环境开关

| 变量 | 默认 | 语义 |
| --- | --- | --- |
| `CHAGEE_PAY_BROADCAST_ENABLED` | `"1"` | `"0"` 时 `notify_session_change` 完全不发网络（**离线测试必置 0**；SSE 退化为纯兜底轮询模式，功能不丢） |

### 1.6 取消链路联动（漏洞修复，随本契约一并定稿）

手动取消（`POST /api/ops/.../orders/{order_no}/cancel`）与超时校准（`order_reconcile`
st==7 分支）此前只置 `OrderRecord=7` + 券回滚，**不联动 PaySession**。现两处均补
`mark_session(order_no, "cancelled")` + `order_cancelled` 事件（payload `source` 分别为
`manual-cancel` / `reconcile`）；事件只在 CAS 成功（真实状态迁移）时记录，与探针/watcher
口径一致。跨进程 SSE 通知由 `mark_session` 内部自动发出。

---

## 2. 状态同步流程（三条收口链路）

三条链路共用同一收口骨架：**协议层确认 → `mark_session` CAS 收口（主 API 进程）→
`pay_broadcast.notify_session_change` 通知收银台进程 → SSE 终态帧推完闭流 → 壳页停表**。
CAS 保证事件数与状态迁移严格一对一（watcher / 探针 / 管理端并发发现同一终态时只有一方赢）。

### 2.1 取消链路（管理端手动取消）

```
管理端                主 API(8000)                      收银台(8010)            壳页
  │ POST /orders/{no}/cancel  │                             │                    │
  ├──────────────────────────→│ api.cancel(order_no)        │                    │
  │                           │ OrderRecord→7 + 券回滚      │                    │
  │                           │ mark_session(CAS→cancelled) │                    │
  │                           │   ├─ order_cancelled 事件   │                    │
  │                           │   └─ notify ─ POST /internal/broadcast (0.3s)   │
  │                           ├────────────────────────────→│ 校验 X-Internal-Token│
  │                           │                             │ 按库内最新态分派    │
  │                           │                             │ _publish→SSE cancelled│
  │                           │                             ├───────────────────→│ 停表「已取消」
```

- 事件 payload `source: "manual-cancel"`（含操作者 `operator`）；CAS 失败（会话已终态）静默不记事件——重复取消幂等。
- 兜底①：通知失败/收银台进程未启动 → SSE **1s 兜底轮询**比对 `(status, pay_deadline_ts)` 变化补推。
- 兜底②：壳页自身 **2s `/status` 轮询**读库态（探针节流 2s），即使 SSE 全断也能秒级停表。

### 2.2 支付链路（用户在支付宝完成付款）

```
支付宝侧 1→3：三个发现方（并发，CAS 只有一方赢）
  a) 壳页 /status 探针（2s 节流 getOrderStatus）
  b) 主 API pay watcher 线程（CHAGEE_PAYWATCH_INTERVAL_SECONDS，默认 2s）
  c) 收银台进程自身的 /status 探针（H5 页面在收银台进程上轮询时）
      ↓ 任一方
  mark_session(CAS→paid, paid_at) → paid_detected 事件 → getOrderDetail 取 pickupNo
  回写会话/OrderRecord → notify → 收银台 _publish → SSE paid 帧推完闭流 → 壳页停表并展示取餐码
```

- 双兜底同 2.1：1s 兜底轮询（SSE 通道）+ 壳页 2s `/status` 轮询（HTTP 通道）——**SSE 与轮询并行**，任一通道可用即可收口。

### 2.3 超时链路（10 分钟 autoCancel）

```
到点茶姬侧自动转 7（paymentExpiryType=autoCancel，倒计时不随续付重置）
  a) order_reconcile 校准线程（CHAGEE_RECONCILE_INTERVAL_SECONDS，默认 60s）
     扫过宽限线（RECONCILE_GRACE_SECONDS=120s）的待支付单 → reconcile_order
     st==7 → OrderRecord→7 + 券回滚 + mark_session cancelled + order_cancelled{source:"reconcile"}
  b) 壳页 /status 探针发现 7 → mark_session cancelled + order_cancelled{source:"probe"} + 券回滚
  c) pay watcher 过期清扫 → mark_session expired + session_expired 收口
      ↓
  notify → SSE cancelled / expired 帧 → 壳页停表
```

- 前端不等服务端确认：壳页本地按 `pay_deadline_ts` 绝对锚点走到 0 即自行停表（§4 场景 5），
  服务端收口是随后的一致性保证——两侧最终一致，最多相差一个轮询周期。

### 2.4 续付重铸（不重置倒计时）

`remint` / `continue-pay` 换新支付串（token 不变），`pay_deadline` 重经 `clamp_pay_deadline`
钳制——**锚定 OrderRecord.created_at（下单时刻）**，续付不会把官方 10 分钟窗顺延；SSE 侧
`(status, pay_deadline_ts)` 指纹不变 → 不误发跃迁事件；工作台/壳页锚点不重置（§3）。

## 3. 前端时间同步（偏移法）

### 3.1 核心公式

```
offset        = server_time − Date.now()          # 每个带 server_time 的响应/帧都刷新
nowServer()   = Date.now() + offset               # 校准后的"服务器视角当前时间"
剩余秒        = (pay_deadline_ts − nowServer()) / 1000   # 绝对锚点倒数，每秒重算
```

- 每秒重算（而非计数器递减）：杜绝 `setInterval` 累计漂移与线程休眠丢失的秒数。
- `pay_deadline_ts` 为 `null` 的会话（无截止锚点）回退 `remaining_seconds` 计数器（兼容）。
- 当前实现为**最新帧覆盖式**校准（局域网 RTT 抖动小，简单够用）；如遇高抖动环境可升级为
  多帧样本取最小偏移（§1.1 建议），接口契约无需变更。

### 3.2 三处倒计时改造点

| 位置 | 实现 |
| --- | --- |
| **壳页** `server/templates/pay_cashier.html` | 进程内 `clockOffset`；`/info`、`/status`、SSE 每一帧都 `syncClock(server_time, pay_deadline_ts)`——`pay_deadline_ts` 仅在非 `null` 时覆盖锚点（防迟到帧把锚点倒拨）；EventSource 实时通道 + 2s 轮询并行；终态帧即停表并关闭 EventSource（防自动重连重复收终态） |
| **工作台** `web/src/views/OrderWorkbenchView.vue` | `payDeadlineTs` 绝对锚点（取响应 `pay_deadline_ts`，缺失回退 `nowMs() + pay_window_seconds`）；每秒 `payLeft = (payDeadlineTs − nowMs())/1000`；**续付（continue-pay）不重置锚点**（新响应同一钳制锚）；draft 倒计时同样走 `nowMs()` |
| **取餐页** `web/src/views/PickupView.vue` | 抽屉秒表 `nowTs = setInterval(() => nowMs(), 1000)`，待支付行倒计时由绝对锚点与 `nowTs` 推算 |

### 3.3 管理端全局校准（`web/src/utils/clock.js`）

- `calibrate(serverTimeMs)` 刷新模块级 `offset`（非法/空值忽略，保持上次校准）；
  `nowMs()` = `Date.now() + offset`（未校准过等价 `Date.now()`）。
- **axios 响应拦截器**（`web/src/api/index.js`）：任何响应体（一层或 `data` 包一层）携带
  `server_time` 即自动 `calibrate`——管理端所有页面共享同一全局偏移，无需各自实现。

## 4. 异常矩阵（现象 → 机制 → 恢复时间）

| # | 场景 | 现象 | 机制 | 恢复时间 |
| --- | --- | --- | --- | --- |
| 1 | **SSE 断线**（手机切后台/代理闲置断链） | 实时帧停更，页面不报错、倒计时不中断 | `EventSource` 原生自动重连，重连首帧 `sync` 重新对齐时钟；断线期间 2s `/status` 轮询持续兜底；5s 心跳兼探活让断链可被及时发现 | 浏览器重连间隔（约 3s）+1 帧；倒计时本地照走，零中断 |
| 2 | **主 API→收银台通知失败**（8010 未启动/超时/未装 requests） | SSE 终态事件晚到（或不来） | `notify_session_change` fire-and-forget：0.3s 超时、任何异常仅 debug 日志，绝不影响 `mark_session` 收口主流程 | SSE 1s 兜底轮询比对指纹变化补推（≤1s） |
| 3 | **收银台进程重启** | 订阅全部断开 | 客户端 EventSource 自动重连；重启后 `_sse_last_seen` 内存态清空，连接首帧 `sync` 即当前真实态（连入即终态则补推终态帧后闭流）；`_sse_reconciler` task 随首个连接重新拉起 | 重连间隔 + 1 帧（秒级） |
| 4 | **客户端时钟漂移/不准** | 本地 `Date.now()` 与服务器偏差 | 偏移法：`offset` 由每帧 `server_time` 持续校准，倒计时只依赖 `(pay_deadline_ts − nowServer())`，本地绝对时刻从不参与 | 每帧即校正（/status 2s、SSE 心跳 5s） |
| 5 | **网络持续失败**（壳页完全断网） | 轮询/SSE 均不可用 | 页面持有最后一次锚点（`pay_deadline_ts`+`offset`），倒计时继续走到 0 并**自行停表**（不依赖服务端确认）；网络恢复后 SSE 重连/轮询取回真实态 | 倒计时零中断；状态确认在恢复后 1 帧内 |
| 6 | **双进程时钟**（8000/8010 各自取 now） | 理论上两进程下发锚点可能不一致 | 同机部署同源时钟（偏差≪网络抖动）；且 `pay_deadline` 唯一存储于共享 SQLite，两端读同一列——不存在"各算各的"；`server_time` 仅用于偏移校正，毫秒级差异无感 | 设计上消除，无需恢复 |
| 7 | **迟到的旧通知**（通知在途时库态又变） | SSE 可能收到与库不符的事件声称 | `/internal/broadcast` 以**库内最新状态**分派事件（不信任请求体 status），旧通知不会把终态倒拨回 issued | 单帧内自愈 |

## 5. 测试与验证记录

### 5.1 自动化离线套件（FakeClient 回放，零真实网络）

新增 `server/test_timesync_offline.py`（本设计的行为锁，pytest 可单跑）：

| 用例 | 覆盖 |
| --- | --- |
| 01 info/status 时间戳字段 | `server_time`≈now±5s、`pay_deadline_ts` 与库内锚点±1s；`expire_in=1800` 被钳到 ≤now+10min（`expire_at` 为钳制后文本） |
| 02 order_cancel 联动 | 取消→`PaySession cancelled`+`order_cancelled{source:manual-cancel}`+`OrderRecord=7`；重复取消幂等（事件仅 1 条） |
| 03 reconcile st==7 联动 | 直调 `reconcile_order`→会话 cancelled+`order_cancelled{source:reconcile}` |
| 04 mark_session 触发通知 | CAS 成功→`notify(order_no,status)`；CAS 失败不重复；新跃迁再通知 |
| 05 SSE sync 首帧 | issued 会话（ASGI 进程内直驱）：`text/event-stream`+`Cache-Control: no-cache`+首帧 `sync` 四字段；cancelled 会话（标准 `client.stream` 全量）：`sync`+`cancelled` 帧后路由主动闭流 |
| 06 internal/broadcast 鉴权 | 无 header 401 / 错 token 401 / 对 token+无会话 404 / 对 token+issued 会话 200（分派 sync、不改库态）；secret 走临时文件隔离 |
| 07 remint 409 不回归 | cancelled 会话 `remint`→409，且不触协议层 |
| 08 零网络保障 | 全程仅触达回放覆盖的协议端点（未知 path 由 FakeClient AssertionError 拦截） |

> 环境注记：本仓 `.venv_verify` 的 starlette TestClient 传输层（httpx2）会把响应体**完整
> 缓冲后**再返回——无限 SSE 流用 `client.stream()` 必然挂死。因此 05 的无限流首帧改为
> ASGI 协议进程内直驱（真实路由+中间件+流式 generator，收到首帧即模拟断开，10s 看门狗）；
> 有限流（终态连入）仍走标准 `client.stream()`。这是测试基建限制，非产品缺陷。

全量回归结果（2026-09-27，逐套单独执行）：

| 套件 | 方式 | 结果 |
| --- | --- | --- |
| `server/test_timesync_offline.py`（新增） | python / pytest | **8/8 通过** |
| `server/test_payportal_offline.py` | python | 20/20 通过 |
| `server/test_orders_offline.py` | python | 12/12 通过 |
| `server/test_mint_provider_offline.py` | pytest（套件自身约定） | 5/5 通过 |
| `server/test_pay_window_offline.py` | pytest（套件自身约定） | 10/10 通过 |
| `server/test_reconcile_offline.py` | python | 11/11 通过 |
| `server/test_coupons_offline.py` | pytest（套件自身约定） | 5/5 通过 |
| `server/test_menu_spec_offline.py` | pytest（套件自身约定） | 11/11 通过 |
| `server/test_oplog_offline.py` | python | 13/13 通过 |
| `tests/test_msp_client_offline.py`（项目根） | python | 11/11 通过 |

### 5.2 手动冒烟清单（上线前逐项勾验）

| # | 步骤 | 预期 |
| --- | --- | --- |
| ① | 造一张待支付单，打开壳页后**修改本机系统时间**（±5 分钟） | 倒计时立即回到正确剩余秒（下一帧 `/status` 或 SSE `sync` 刷新 `offset`），到点时刻与下单+10min 一致——偏移法生效 |
| ② | 下单后不支付，在管理端**取消该订单** | 壳页 1~2s 内秒级停表并展示「已取消」（SSE `cancelled` 帧或 2s 轮询先到，二者其一）；`pay-events` 可见 `order_cancelled{source:manual-cancel}` |
| ③ | 壳页 DevTools → Network → **Offline**，等倒计时自然到点，再恢复网络 | 断网期间倒计时继续走到 0 并本地停表（不白屏不卡死）；恢复网络后 SSE 自动重连，首帧 `sync` 与库内终态一致（`cancelled`/`expired`），页面展示对应终态 |
| ④ | 工作台对临期单点**续付**（continue-pay） | 倒计时不重置、不回弹（同一钳制锚 `pay_deadline_ts`），仅支付串更新；壳页链接不变（token 不变） |

## 6. 兼容性说明

- **无 schema 变更**：不新增表/列。`PaySession.pay_deadline` 语义收紧为"钳制后截止"
  （`clamp_pay_deadline` 幂等，`min` 运算多处钳制结果一致），旧数据无需迁移；
  `data/internal_broadcast.secret` 为惰性创建的数据文件（`open("x")` 独占创建防双进程竞态），非 schema。
- **轮询通道完整保留**：`/status` 2s 节流轮询契约不变；SSE 是增强而非替代——
  前端 EventSource 与轮询并行，极老 WebView 无 SSE 时纯轮询模式功能不丢。
- **开关 `CHAGEE_PAY_BROADCAST_ENABLED`**（默认 `"1"`）：置 `"0"` 时 `notify_session_change`
  完全不发网络，SSE 退化为 1s 兜底轮询模式（终态最晚晚 1s，功能不丢）；离线测试必置 0。
- **响应字段兼容**：`remaining_seconds` 原样保留（降级锚点）；`expire_at` 语义由"支付宝
  time_expire 原文（下单+30min）"改为"钳制后 pay_deadline 文本"——前端已按新语义适配，
  旧消费者若比对原文会看到更早的截止时间（这正是修复目标）。无会话场景（`_pay_link_payload`
  未铸会话）`server_time`/`pay_deadline_ts` 为 `null`，前端回退本地时钟。
- **payportal 新增 asyncio task 的取舍**：`_sse_reconciler_loop` 兜底轮询 task 仅在**存在
  SSE 订阅**时存活（无订阅即退出并复位标志，空闲零开销，下个连接重新拉起）；轮询周期 1s、
  批量单事务查订阅单（`asyncio.to_thread`，不阻塞事件循环）——以每秒一次轻查询换
  "通知丢失最终一致"的保底，代价可接受；主 API 进程不新增任何线程/任务。
