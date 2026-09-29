# 订单中枢 API 参考手册（Order Intake API Reference）

> 版本 1.0 · 2026-09-30 · 对应代码：`server/routers/intake.py` + `server/services/{intake_registry, order_queue, order_worker, intake_notify}.py`
> 架构与设计决策见 [intake_system.md](intake_system.md)；本文是**接口级**完整参考：请求/响应报文、状态码、错误语义、内部编程接口与回调协议，均附使用示例。

## 0. 总览

订单中枢的全部 API 分四层：

| 层 | 内容 | 章节 |
|---|---|---|
| ① 对外 HTTP | 内部标准接口（JWT）+ 外部适配接口（X-Api-Key） | §1、§2 |
| ② 管理 HTTP | 登记单/队列/死信/密钥管理（intake:manage） | §3 |
| ③ 内部编程接口 | 登记/队列/执行/通知四模块的函数级 API（worker 与路由共用核心） | §4 |
| ④ 出站协议 | 按单 HMAC 回调 + SSE 实时推送（消费方视角） | §5、§6 |

鉴权方式速查：

| 接口组 | 鉴权 | 说明 |
|---|---|---|
| `/api/intake/orders*`（登记/列表/详情） | `Authorization: Bearer <JWT>` | 权限点 `feature:order` |
| `/api/intake/orders/*/cancel`、`/requeue`、`/queue/*`、`/keys*` | `Authorization: Bearer <JWT>` | 权限点 `intake:manage` |
| `/api/intake/v1/*` | `X-Api-Key: ck-xxxx` | 密钥经 §3.4 创建，sha256 落库 |

公共约定：

- 金额字段一律为**字符串**（`"15.70"`），非负小数；
- 幂等键为 `customer_order_no`（内部）/ `orderNo`（外部），重复提交返回现状、不重复入队；
- 时间戳格式 `YYYY-MM-DD HH:MM:SS`（本地时区）；
- 错误响应统一 FastAPI 结构 `{"detail": ...}`，`detail` 可为字符串或对象（预检 422 带候选清单）。

---

## 1. 内部标准接口（JWT，feature:order）

### 1.1 登记客户订单 `POST /api/intake/orders`

接收层只做三件事：Pydantic 校验 → 菜单库预检（本地，必要时回源）→ 毫秒级 SQLite 短事务（登记+入队原子提交），即返 **202**。重活（决策/试算/下单/支付收口）全部由 worker 异步执行。

**请求体**（`IntakeOrderRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| customer_order_no | str(≤64) | ✅ | 幂等键，客户侧唯一单号 |
| store_no | str(≤32) | ✅ | 茶姬门店号（如 `CN07078`） |
| store_name | str(≤128) | | 门店名（快照展示用） |
| sku_id | str(≤64) | ✅ | 茶姬 skuId（客户平台 linkId） |
| quantity | int 1..99 | | 默认 1 |
| spec_texts | list[str] | | 文案规格（如 `["半糖"]`），空=默认组合 |
| customer_price | str(≤16) | ✅ | 客户支付价（决策 revenue，利润阈值判定依据），非负金额字符串 |
| allow_full_price | bool | | 无券时是否允许原价单，默认 false |
| plan_id | int≥0 | | 下单方案（0=自动） |
| packet_id | int≥0 | | 套餐（0=自动匹配） |
| drink_info | str(≤200) | | 饮品信息（选填，带入订单快照） |
| phone | str(≤16) | | 联系电话 |
| remark | str(≤255) | | 订单备注 |
| callback_url | str(≤512) | | 按单回调地址（http/https，空=不回调） |

**使用示例**：

```bash
curl -X POST http://127.0.0.1:8000/api/intake/orders \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{
    "customer_order_no": "C20260930-0001",
    "store_no": "CN07078", "store_name": "霸王茶姬（示范店）",
    "sku_id": "102233", "quantity": 1,
    "spec_texts": ["少冰", "五分甜"],
    "customer_price": "11.70",
    "plan_id": 0, "packet_id": 0,
    "drink_info": "伯牙绝弦", "phone": "13800000000",
    "remark": "不要奶盖", "callback_url": "https://cb.customer.example/intake"
  }'
```

**响应 202**（新单已入队）：

```json
{
  "duplicate": false,
  "customer_order_no": "C20260930-0001",
  "source": "internal", "status": "enqueued", "status_label": "已入队",
  "step": "queued", "progress": "已入队，等待消费",
  "sku_id": "102233", "store_no": "CN07078", "quantity": 1,
  "customer_price": "11.70", "goods_snapshot": {"spu_id": "...", "spu_name": "伯牙绝弦", "price": 15.0},
  "account_id": null, "chagee_order_no": null, "pickup_no": null,
  "pay_url": null, "pay_amount": null, "coupon_code": null,
  "attempts": 0, "error": "", "trace_id": "",
  "callback_url": "https://cb.customer.example/intake",
  "created_at": "2026-09-30 10:00:00", "updated_at": "2026-09-30 10:00:00",
  "started_at": null, "finished_at": null,
  "query": "/api/intake/orders/C20260930-0001"
}
```

**响应 200**（幂等命中，单号已存在）：`duplicate: true` + 该单现状字段 + `note`。不重复入队、不报错。

**响应 422**（预检失败，坏报文不进队列）：

```json
{ "detail": { "message": "规格文案「半糖」存在歧义：可选 五分甜/七分甜", "candidates": ["五分甜", "七分甜"] } }
```

预检内容：`sku_id` 经菜单规格库 `resolve_by_sku` 命中（新店/新品自动全店回源一次）；`spec_texts` 经 `resolve_spec_texts` 三级匹配（精确/别名 → 模糊 → 歧义拒猜带候选）。**歧义绝不静默下错规格**。

### 1.2 登记单列表 `GET /api/intake/orders`

Query：`keyword`（单号/茶姬单号/取餐码/来源模糊）、`status`（七态枚举）、`source`、`page`(≥1)、`page_size`(1..100，默认 20)。

**响应**：`{"total": n, "stats": {"by_status": {"enqueued": 2, "completed": 5, ...}}, "items": [...]}`（items 元素同 1.1 的登记单字段）。

```bash
curl "http://127.0.0.1:8000/api/intake/orders?status=failed&page=1&page_size=20" -H "Authorization: Bearer $JWT"
```

### 1.3 登记单详情 `GET /api/intake/orders/{customer_order_no}`

返回登记单全量字段（状态/进度 step/progress、账号、茶姬单号、取餐码、支付链接、trace_id 等）。404 = 单号不存在。

```bash
curl http://127.0.0.1:8000/api/intake/orders/C20260930-0001 -H "Authorization: Bearer $JWT"
```

**状态机**（`status` 七态，迁移见 §4.3）：

```
registered → enqueued → processing(step: decide→settle→create)
    → completed（零元单/支付收口）
    → awaiting_payment（差额单，等支付；收口后 → completed；超时取消 → failed）
    → failed / cancelled（终态，可人工 requeue 回 enqueued）
```

---

## 2. 外部适配接口（X-Api-Key，KFC 系报文）

### 2.1 外部登记 `POST /api/intake/v1/orders`

字段映射（2026-09-27 客户平台抓包实锤）：`linkId` = 茶姬 skuId。

| 字段 | 类型 | 必填 | 映射到内部 |
|---|---|---|---|
| orderNo | str(≤64) | ✅ | customer_order_no（幂等键） |
| linkId | str(≤64) | ✅ | sku_id |
| count | int 1..99 | | quantity |
| specs | list[str] | | spec_texts（歧义 422 拒猜） |
| storeNo | str(≤32) | | store_no（缺省回落 `data/intake_config.json` 的 `default_store_no`，仍缺 → 422） |
| payAmount | str(≤16) | ✅ | customer_price（非负金额字符串） |
| phone / remark / callbackUrl | str | | 同名内部字段 |

```bash
curl -X POST http://<host>:8000/api/intake/v1/orders \
  -H "X-Api-Key: ck-xxxxxxxxxxxxxxxx" -H "Content-Type: application/json" \
  -d '{"orderNo":"KF998877","linkId":"102233","count":1,"specs":["五分甜"],"payAmount":"11.70","callbackUrl":"https://cb.customer.example/kf"}'
```

**响应 202**：

```json
{
  "duplicate": false,
  "orderNo": "KF998877", "status": "enqueued",
  "query": "/api/intake/v1/orders/KF998877",
  "note": "已入队，异步执行；轮询查询接口或等待回调（order_created/completed/cancelled）"
}
```

**响应 200**（幂等）：`duplicate: true` + `orderNo/status/pickupNo/chageeOrderNo` 现状。

**错误**：`401` 缺/无效/已吊用密钥；`429` 超限（每密钥滑窗 60s，默认 120 单/分钟，`CHAGEE_INTAKE_RATE_LIMIT` 可调）；`422` 预检失败（message + candidates）。

### 2.2 外部查询 `GET /api/intake/v1/orders/{orderNo}`

KFC 系字段命名返回进度/取餐码/支付链接。**单号归属密钥隔离**：只能查本密钥登记的单，他人单号一律 404。

```json
{
  "orderNo": "KF998877", "status": "awaiting_payment", "statusLabel": "待支付",
  "step": "awaiting_payment", "progress": "已成差额单，等待支付（收口自动完成）",
  "chageeOrderNo": "BWC202609301234", "pickupNo": "",
  "payUrl": "http://<host>:8010/pay/<token>", "payAmount": "4.30",
  "error": "", "attempts": 1,
  "createdAt": "2026-09-30 10:00:01", "finishedAt": ""
}
```

---

## 3. 管理接口（JWT，intake:manage）

### 3.1 消费前取消 `POST /api/intake/orders/{no}/cancel`

仅 `registered/enqueued` 可取消（`processing` 及之后 **409**——执行中/已成单的取消走茶姬订单管理）。同时将未消费消息一并置 dead（防 worker 随后拾起执行）。成功返回取消后登记单字段。

```bash
curl -X POST http://127.0.0.1:8000/api/intake/orders/C20260930-0001/cancel -H "Authorization: Bearer $JWT"
```

### 3.2 失败/取消单重放 `POST /api/intake/orders/{no}/requeue`

仅 `failed/cancelled` 可重放（`completed` **409** 严禁重下——茶姬单已成，重下=重复下单）。发**全新消息**，重试额度重置，`error` 清空。

### 3.3 队列监控

- `GET /api/intake/queue/stats` — 队列深度（pending/processing/done/dead）、最老 pending 时间、worker 配置数（`CHAGEE_ORDER_WORKERS`）与存活线程名。
- `GET /api/intake/queue/messages?status=dead&page=1&page_size=20` — 消息分页（`status` ∈ pending/processing/done/dead，默认死信），带关联登记单号。
- `POST /api/intake/queue/messages/{msg_id}/requeue` — 死信重放：消息回 pending、额度重置；关联登记单若处 failed/cancelled 同步回 enqueued。404 = 非死信态。

```bash
curl http://127.0.0.1:8000/api/intake/queue/stats -H "Authorization: Bearer $JWT"
# → {"pending": 0, "processing": 0, "done": 320, "dead": 1,
#    "oldest_pending_created_at": "", "oldest_pending_visible_at": "",
#    "workers_configured": 2, "workers_alive": ["order-worker-0", "order-worker-1"]}
```

### 3.4 接入密钥管理

- `GET /api/intake/keys` — 密钥列表（指纹展示 `key_hash[:12]…`，明文不可再现）。
- `POST /api/intake/keys` — 创建：`{"label": "KFC客户平台A", "source": "kfc-a"}`，**明文 api_key 仅本次响应返回一次**。
- `POST /api/intake/keys/{key_id}/disable` — 吊销（立即生效：后续请求 401）。

```bash
curl -X POST http://127.0.0.1:8000/api/intake/keys -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" -d '{"label":"KFC客户平台A","source":"kfc-a"}'
# → {"id": 3, "label": "KFC客户平台A", "source": "kfc-a",
#    "api_key": "ck-J8x...（仅此一次返回，请立即交付对接方）", "note": "..."}
```

---

## 4. 内部编程接口（函数级，worker 与路由共用核心）

### 4.1 `services/intake_registry.py` — 登记核心

| 函数 | 签名要点 | 说明 |
|---|---|---|
| `validate_and_normalize(db, body)` | 内部报文 → 归一化 payload | 预检失败抛 `HTTPException(422, {message, candidates})`；返回 worker 执行链输入契约（含 `sku_resolved` 预检快照） |
| `external_to_internal(body)` | KFC 系 → `IntakeOrderRequest` | storeNo 缺省回落 `intake_config.default_store_no` |
| `register_order(db, *, source, api_key_id, payload, raw_payload, callback_url)` | → `(CustomerOrder, created)` | **登记+入队同一事务原子提交**；幂等：单号已存在返回 `(既有单, False)` |
| `create_api_key(db, label, source)` | → `(row, plaintext)` | 明文 `ck-<token_urlsafe(32)>` 仅返回一次，库内 sha256 |
| `check_rate_limit(api_key_id)` | 超限抛 429 | 每密钥 60s 滑窗，进程内存态 |
| `load_intake_config()` | → dict | `data/intake_config.json`：`default_store_no` / `allow_full_price` |

```python
# 登记一单（任意调用方，如脚本/其他服务）：
payload = intake_registry.validate_and_normalize(db, body)          # 预检(422 fail-fast)
payload["customer_order_no"] = body.customer_order_no
co, created = intake_registry.register_order(
    db, source="internal", api_key_id=0, payload=payload,
    raw_payload=body.model_dump(), callback_url=body.callback_url)
if not created:            # 幂等命中，返回现状
    ...
```

### 4.2 `services/order_queue.py` — SQLite 持久化队列

| 函数 | 说明 |
|---|---|
| `enqueue(db, customer_order_id, payload=None, topic="order.create", max_attempts=3)` | 入队走**调用方事务**（与登记同一 commit 原子生效） |
| `claim_one(worker_id)` | 原子认领：单条 `UPDATE…RETURNING`，pending→processing、attempts+1、锁标记；无可领返回 None |
| `ack(msg_id)` | 置 done（幂等，非 processing 态静默不动） |
| `fail(msg_id, error, attempts, max_attempts)` | 退避重试：30/120/300s 阶梯回 pending；额度耗尽置 dead。返回 `"pending"`/`"dead"` |
| `kill(msg_id, error)` | 致命失败直达死信（决策 blocked/OrderHang，不耗退避） |
| `requeue_dead(msg_id)` | 死信重放：dead→pending，attempts/last_error 归零 |
| `reclaim_orphans(timeout=None)` | 崩溃恢复：processing 超 `CHAGEE_ORDER_MSG_VISIBILITY_TIMEOUT`(600s) 复位 pending |
| `queue_stats()` | 队列深度统计（fail-soft，监控端点与 log_monitor 数据源） |

```python
# worker 消费骨架（services/order_worker.process_once 即此流程）：
msg = order_queue.claim_one("order-worker-0")     # 原子认领，多 worker 不重不漏
if msg is None: ...
try:
    ...                                            # 执行链（绝不与远程调用混在同一事务）
    order_queue.ack(msg["id"])
except RetryableError as e:
    order_queue.fail(msg["id"], str(e), msg["attempts"], msg["max_attempts"])
except FatalError as e:
    order_queue.kill(msg["id"], str(e))
```

### 4.3 `services/intake_notify.py` — 状态机/推送/回调

- `transition(db, co, new_status, *, step, progress, error, **fields)` — 状态 CAS 推进：非法迁移拒绝返回 False（WARN 留痕），空值字段不覆盖，进终态记 `finished_at`；每次成功推进向 SSE topic `intake` 推一帧。合法迁移表：
  `registered→{enqueued,processing,cancelled}`；`enqueued→{processing,cancelled,failed}`；`processing→{awaiting_payment,completed,failed,enqueued}`；`awaiting_payment→{completed,failed}`；`failed/cancelled→{enqueued}`（人工重放）；`completed` 终态不可逆。
- `publish_status(co)` — 状态帧 → SSE（事件名 `order_status`）。
- `on_chagee_order_paid(db, order_no, pickup_no)` / `on_chagee_order_cancelled(db, order_no)` — pay-watcher 收口挂钩：推进 completed/failed + 取餐码回填 + 回调；幂等（已完成单只补迟到取餐码）。
- `dispatch_intake_callback(db, co, event, extra=None)` — 按单回调（见 §5），callback_url 为空即 no-op，独立 daemon 线程投递。

### 4.4 `services/order_worker.py` — 消费执行器

- `process_once(worker_id="order-worker-0")` — 认领并处理一条消息，返回 `idle/done/retry/dead/crash`（离线测试直接调用做确定性验证）。
- `start_order_workers()` — 幂等启动线程池（`CHAGEE_ORDER_WORKERS`，默认 2；≤0 不启动；1s 轮询 + 每 30 轮兜底孤儿回收）。
- 执行链（复用 F5 手动下单同一套 core，零重复开发）：`decide_core`（选号选券）→ `settle_core`（试算 draft）→ `create_core`（auto_fallback 下单）→ 零元单 `completed`+取餐码 / 差额单 `awaiting_payment`+H5 链接 → pay-watcher 收口自动推进。
- 消费幂等闸门：认领后登记单处终态或 `chagee_order_no` 非空 → 直接 ack，绝不重复下单。
- 失败分类：HTTPException detail 含「结果不确定/一致性校验失败」→ kill（防重复下单）；其余（409 账号占用/凭证失效、502 协议、400 券耗尽等）→ fail 重试（重跑 decide 自动换号换券）。

### 4.5 依赖的兄弟模块接口

| 模块 | 中枢用到的接口 | 用途 |
|---|---|---|
| `routers.decision` | `decide_core(db, body: DecideRequest)` | 选号选券（套餐/成本规则/方案/阈值全口径），返回 `verdict/account_id/coupon/decision_log_id/settle_prefill` |
| `routers.orders` | `settle_core(db, account, body: OrderSettleRequest)`；`create_core(db, account, body: OrderCreateRequest)` | 试算 draft / 下单（auto_fallback 券自动切换） |
| `services.menu_spec` | `resolve_by_sku(db, store_no, sku_id)`；`resolve_spec_texts(db, store_no, spu_id, texts)`；异常 `SpecResolveError(.candidates)` | 接收层预检（三级匹配，歧义拒猜） |
| `services.events_bus` | `publish(topic, event, data)` / `subscribe/unsubscribe` | SSE 帧广播（topic `intake`） |
| `services.payment_events` | pay-watcher 在 `_handle_paid/_handle_cancelled` 惰性调用 §4.3 收口挂钩 | 支付状态收口推进 |

---

## 5. 出站回调协议（中枢 → 客户平台）

事件（`event` 字段）：`order_created`（差额单链接下发）/ `completed`（零元单完成或支付收口）/ `pickup`（迟到取餐码补发）/ `cancelled`（支付取消/超时）。

- 投递条件：登记时 `callback_url` 非空；`POST` JSON，独立 daemon 线程，1/2/4s 退避共 4 次，失败仅 oplog 留痕（`intake.callback_failed`）。
- **验签**：`X-CHAGEE-Signature = hex(hmac_sha256(secret, raw_body))`，secret 与支付回调同源（`data/pay_callback_config.json` 的 `secret`，缺省 `data/secret.key`）。接收方须按**原始 body 字节**验签。

```json
{
  "event": "completed",
  "customer_order_no": "KF998877",
  "chagee_order_no": "BWC202609301234",
  "status": "completed",
  "pickup_no": "A87",
  "pay_url": "", "pay_amount": "",
  "dispatched_at": "2026-09-30 10:03:22"
}
```

接收方验签示例（Python）：

```python
sig = hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
hmac.compare_digest(sig, request.headers["X-CHAGEE-Signature"])   # True 才受理
```

## 6. SSE 实时推送（消费端订阅）

`GET /api/events?topics=intake&token=<JWT>`（EventSource 无法带 Authorization 头，JWT 走 query；权限 `feature:order`）。每次状态迁移推一帧 `event: order_status`：

```
event: order_status
data: {"customer_order_no":"KF998877","status":"processing","step":"settle","progress":"试算中",
       "chagee_order_no":"","pickup_no":"","pay_url":"","error":"","updated_at":"2026-09-30 10:00:05"}
```

前端订单中枢页（`/ops/intake`）即此订阅。15s 无事件发 `: ping` 心跳保连。

## 7. 环境变量与配置文件

| 变量 | 默认 | 说明 |
|---|---|---|
| CHAGEE_ORDER_WORKERS | 2 | worker 线程数（≤0 不启动；离线测试置 0） |
| CHAGEE_ORDER_MSG_VISIBILITY_TIMEOUT | 600 | 认领超时回收阈值（秒） |
| CHAGEE_INTAKE_RATE_LIMIT | 120 | 每密钥每分钟接收上限 |
| CHAGEE_INTAKE_ENABLED | 1 | 预留总开关 |

`data/intake_config.json`：`{"default_store_no": "CN00529", "allow_full_price": false}`（外部单缺 storeNo 的回落默认门店 / 外部单是否允许原价）。

## 8. 监控与告警挂钩

- **log_monitor 六规则**含 `queue_dead_backlog`（dead>0 → ERROR）与 `queue_backlog_high`（最老 pending>5min → WARN）；`order-worker-N` 纳入 `thread_dead/heartbeat_stale` 线程监控。
- **oplog 动作**：`intake.order_received / queue.retry / queue.dead / queue.orphan_reclaim / intake.order_completed / intake.order_awaiting_payment / intake.order_failed / intake.order_cancelled / queue.dead_requeue / intake.transition_denied / intake.callback_failed`。worker 每消息独立 trace_id，`GET /api/ops/logs/trace/{id}` 整链查询。

## 9. 边界与注意事项

- **单饮品口径**：一次一 SKU（与 decide 引擎一致），多商品购物车为未来扩展（payload 已按 dict 预留）。
- **「半糖」类歧义文案 422 拒猜**：客户平台须规范文案或扩充 `SPEC_ALIASES` 别名表。
- **completed 严禁重放**；processing 及之后不可取消——对应处置路径见 §3。
- 中枢只跑主 API 进程（8000）；pay_portal（8010）进程不启动 worker（双进程职责约定）。
