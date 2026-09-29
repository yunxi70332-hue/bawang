# 异步订单中枢（Order Intake MQ System）

> 2026-09-29 · 分支 `feature/order-intake-mq` · 需求：MQ 异步订单处理 / 高并发接收接口 /
> 订单登记专用表 / 复用取餐查询 / 监控告警 / 备份策略

## 1. 架构总览

```
外部客户平台 ─POST /api/intake/v1/orders (X-Api-Key, KFC系格式+适配层)─┐
内部工作台  ─POST /api/intake/orders     (JWT, 标准格式)──────────────┤
                                                                    ▼
                          接收层 routers/intake.py + services/intake_registry.py
                    Pydantic 校验 + 菜单库预检（歧义拒猜422） + 幂等（customer_order_no唯一）
                                                                    ▼ 同一事务原子提交
                    customer_orders（登记表） + order_messages（SQLite 持久化队列）
                                                                    ▼
              order-worker 线程池（CHAGEE_ORDER_WORKERS，默认2；claim 原子认领）
                全自动链：decide_core → settle_core → create_core（auto_fallback）
              零元单→completed+取餐码；差额单→awaiting_payment+H5链接→按单回调
                                                                    ▼
              现有 pay-watcher（2s）支付收口 → intake_notify 挂钩推进 completed
              + 取餐码缓存回填 + SSE 推送（topic "intake"）+ 按单回调
```

### MQ 选型：SQLite 持久化队列（用户已确认）

单机部署（主 API 单进程）+ 现役栈 SQLite/WAL/daemon 线程。队列即 `order_messages` 表：
- **登记与入队同一事务原子提交**（外部 broker 做不到）；
- 重试退避/死信/宕机恢复全在应用层可控，零外部服务依赖；
- WAL + busy_timeout=5000 + synchronous=NORMAL（2026-09-29 调优）；
- 未来如需迁移 Redis Streams，`services/order_queue.py` 是唯一替换点，接口不变。

### 可靠性语义（at-least-once + 消费幂等）

| 机制 | 实现 |
|---|---|
| 认领原子性 | 单条 `UPDATE…RETURNING`（SQLite 单写者无 check-then-act 竞态）；attempts 认领时+1 |
| 重试 | `fail()`：attempts<max（默认3）按 30/120/300s 阶梯回 pending（next_visible_at 延迟可见） |
| 死信 | attempts 耗尽 → dead；`kill()`：致命错误（决策 blocked/OrderHang）直达死信不耗退避 |
| 崩溃恢复 | `reclaim_orphans()`：processing 超 600s（CHAGEE_ORDER_MSG_VISIBILITY_TIMEOUT）复位 pending，worker 池每 30 轮兜底 |
| 消费幂等 | worker 认领后先查登记单：终态或已成单（chagee_order_no 非空）直接 ack；「单次一单」检查为最后防线 |

### 失败分类（HTTPException 语义与 F5 路由完全一致）

- **可重试**（fail→退避）：账号占用 409 / 凭证失效 409 / 协议 502 / 券耗尽 400 —— 重跑 decide 自动换号换券；
- **致命**（kill→死信）：决策 blocked（利润/库存确定性拒绝）、createOrder 结果不确定（OrderHang，严禁自动重试防重复下单）、一致性校验中止。

## 2. 数据表（models.py，create_all 自动建表）

- **customer_orders**：customer_order_no(唯一,幂等键)、source、payload(归一化)/raw_payload(原始)、
  status（registered→enqueued→processing→awaiting_payment→completed/failed/cancelled）、step/progress、
  account_id/chagee_order_no/pickup_no/pay_url/pay_amount/coupon_code、callback_url、error、attempts、
  trace_id、时间戳组。索引：(status,created_at)、chagee_order_no、source。
- **order_messages**：topic、customer_order_id、status(pending/processing/done/dead)、attempts/max_attempts、
  next_visible_at、locked_by/locked_at、last_error。索引：(status,next_visible_at)。
- **intake_api_keys**：key_hash(sha256,唯一)、label、source、active、last_used_at（明文仅创建时返回一次）。

## 3. API 一览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | /api/intake/orders | JWT feature:order | 内部登记（202；幂等 duplicate=true） |
| GET | /api/intake/orders | JWT feature:order | 列表（keyword/status/source/分页+分组统计） |
| GET | /api/intake/orders/{no} | JWT feature:order | 详情（状态/进度/取餐码/支付链接/trace） |
| POST | /api/intake/orders/{no}/cancel | intake:manage | 消费前取消（未消费消息一并作废） |
| POST | /api/intake/orders/{no}/requeue | intake:manage | failed/cancelled 重放入队（completed 严禁） |
| GET | /api/intake/queue/stats | intake:manage | 队列深度/死信/worker 存活 |
| GET | /api/intake/queue/messages?status=dead | intake:manage | 死信（默认）/消息分页 |
| POST | /api/intake/queue/messages/{id}/requeue | intake:manage | 死信重放（额度重置） |
| GET/POST | /api/intake/keys（+/{id}/disable） | intake:manage | 接入密钥管理 |
| POST | /api/intake/v1/orders | X-Api-Key | **外部 KFC 系**：orderNo/linkId/count/specs/storeNo/payAmount（缺省回落 intake_config.default_store_no） |
| GET | /api/intake/v1/orders/{orderNo} | X-Api-Key | 外部状态查询（KFC 系字段命名） |

外部限流：每密钥滑窗 60s / `CHAGEE_INTAKE_RATE_LIMIT`（默认120）次，超限 429。
回调（按单 callback_url）：事件 order_created/completed/pickup/cancelled，签名
`X-CHAGEE-Signature = hex(hmac_sha256(secret, raw_body))`（secret 同 pay_callback_config.json），
1/2/4s 退避共 4 次，独立线程投递。

## 4. 复用而非重复开发（对应需求§4）

| 能力 | 复用方式 |
|---|---|
| decide 决策引擎 | `routers/decision.py decide_core()` 提取为可编程调用（套餐/成本规则/方案/阈值全口径） |
| settle/create 下单链 | `routers/orders.py settle_core()/create_core()` 同一实现，HTTP 路由变薄适配层（test_orders_offline 全量回归零漂移） |
| 取餐码获取四通道 | 零元即时 / pay-watcher 2s / H5 探针 / 全量扫描——原样保留；中枢只做 customer_orders.pickup_no 缓存列同步（intake_notify 挂钩） |
| 取餐查询（F6/pickup search） | 完全不动；登记单 chagee_order_no ↔ order_records.order_no 弱关联 |
| 支付收口 | 现有 pay-watcher 的 `_handle_paid/_handle_cancelled` 各加一处惰性挂钩（intake_notify.on_chagee_order_paid/cancelled） |
| SSE 基建 | events.py `_TOPIC_PERMS` 注册 `"intake"` topic；worker 每次状态迁移 publish |
| 告警管线 | log_monitor 新增 queue_dead_backlog / queue_backlog_high 两规则，复用 op_alert+webhook/SMTP |

## 5. 监控与运维

- **SSE**：`GET /api/events?topics=intake&token=<JWT>`（perm feature:order），事件 `order_status` 逐迁移推送；前端「订单中枢」页（/ops/intake）实时刷新。
- **log_monitor 六规则**：原四条 + queue_dead_backlog（dead>0 → ERROR）+ queue_backlog_high（最老 pending>5min → WARN）；order-worker-0 纳入 thread_dead/heartbeat_stale 监控。
- **oplog**：intake.order_received / queue.retry / queue.dead / queue.orphan_reclaim / intake.order_completed / intake.order_awaiting_payment / intake.order_failed / queue.dead_requeue；worker 线程独立 trace_id，`GET /api/ops/logs/trace/{id}` 整链查询。
- **备份**（此前全仓无自动备份）：`services/backup.py` 每日 sqlite3 backup API 一致性快照
  app.db + oplog.db → `data/backups/`，保留 `CHAGEE_BACKUP_RETENTION_DAYS`（默认14天）自动清理。

## 6. 环境变量（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| CHAGEE_ORDER_WORKERS | 2 | worker 线程数（<=0 不启动；离线测试置 0） |
| CHAGEE_ORDER_MSG_VISIBILITY_TIMEOUT | 600 | 认领超时回收阈值（秒） |
| CHAGEE_INTAKE_ENABLED | 1 | 预留总开关 |
| CHAGEE_INTAKE_RATE_LIMIT | 120 | 每密钥每分钟接收上限 |
| CHAGEE_BACKUP_INTERVAL_SECONDS | 86400 | 备份间隔（<=0 不启动） |
| CHAGEE_BACKUP_RETENTION_DAYS | 14 | 备份保留期 |

离线测试约定：conftest.py 统一置 `CHAGEE_ORDER_WORKERS=0`、`CHAGEE_BACKUP_INTERVAL_SECONDS=0`。

## 7. 测试与压测结论

| 套件 | 结果 | 覆盖 |
|---|---|---|
| test_queue_offline.py | 8/8 | 入队原子性/并发认领不重不漏/退避/死信/孤儿回收/重放/统计 |
| test_intake_offline.py | 10/10 | 202/幂等/422矩阵/外部鉴权+适配+限流429/取消/重放/零元全自动链/决策blocked→死信/差额链+收口挂钩/HMAC 回调实收验签 |
| test_intake_stress.py | 2/2 | 8线程320单：零丢失零重复全202；消费管线排空320条 |
| 存量 12 套件 | 全过 | orders 17 / decision 27 / reconcile 11 / payportal 20 / oplog 13 / …（提取重构零漂移） |

**压测口径**（TestClient 壳内测量，含每请求 ~180ms anyio portal 开销）：
接收 16.4 单/秒（8线程）；消费管线 6.6 条/秒/worker（含状态机+SSE+oplog 三次打标）。
真实 uvicorn 部署下显著更高；按业务量（每日百级单）余量充足。全链真实瓶颈为茶姬远程接口时延。

## 8. 已知边界与后续

- 单饮品口径（与 decide 引擎一致）：多商品购物车为未来扩展（payload 已按 dict 预留）。
- 接收幂等返回 202 + `duplicate:true` 字段（路由级统一状态码；不重复入队）。
- 外部对接「半糖」类歧义文案：422 拒猜（带全部候选），绝不静默下错规格——客户平台侧需规范文案或
  扩充 SPEC_ALIASES 别名表。
- 备份对象为 app.db + oplog.db；云手机凭证（data/accounts/）未纳入，必要时手工复制。
