# 官方收银台支付参数串 · 字段与表结构契约（2026-09-28）

> 需求：在 /ops/order 支付卡集成官方收银台支付参数串；系统库新增独立字段存储；
> 结构便于支付宝浏览器支付 Python 脚本调用解析；生成/存储/读取三功能齐备。

## 1. 数据表：`pay_param_records`（独立存储域）

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| order_no | VARCHAR(64) UNIQUE + INDEX | 业务关联键（order_records / pay_sessions 同键） |
| account_id | INTEGER INDEX | 账号快照（无外键，同 PayEventLog 口径） |
| pay_token_prefix | VARCHAR(16) | 支付会话 token 前 8 位 |
| **param_str** | TEXT | ★ 支付参数串（JSON v1 紧凑单行原文，本契约主体） |
| source | VARCHAR(32) | protocol-mint / frida-mint / manual / static-config |
| created_at / updated_at | DATETIME | |

- **一单一活跃记录**：`order_no` 唯一，重铸/回填覆盖更新（与 `pay_sessions.alipay_cashier_url`
  的「最新短窗」语义一致），续付不改 token、参数串整串替换。
- **独立性与关联性**：不设外键，支付会话取消/重铸后参数串仍完整可查（生命周期独立）；
  需要业务语境时按 `order_no` join、按 `account_id` 筛选。
- 建表：`Base.metadata.create_all`（seed.init_db），服务重启自动创建，无需手写迁移。

## 2. param_str 契约（JSON v1）

```json
{
  "v": 1,                          // 契约版本，解析端强校验
  "order_no": "20260928...",
  "pay_no": "CHP20260928...",
  "out_trade_no": "...",
  "pay_amount": "16.00",           // 实付差额（支付宝侧金额）
  "total_amount": "19.00",         // 订单总额
  "cashier_url": "https://mclient.alipay.com/cashierRoutePay.htm?...",
  "base_url": "https://mclient.alipay.com/cashierRoutePay.htm",
  "params": {                      // URL query 全量参数（URL 解码后）
    "route_pay_from": "h5", "init_from": "SDKLite",
    "session": "...", "utdid": "...", "tid": "...", "cc": "y"
  },
  "source": "protocol-mint",
  "generated_at": "2026-09-28 18:05:17",
  "expires_at": "2026-09-28 18:10:11"
}
```

- **必需参数 `session` / `utdid` / `tid`**（mobilegw 设备级三元组）缺失即生成失败（不落库），
  解析端同样拒收——半截参数对支付脚本毫无价值。
- 存储/接口下发恒为**紧凑单行** JSON（`ensure_ascii=False`），前端展示才 pretty。

## 3. 生成 / 存储 / 读取链路

| 功能 | 入口 |
|---|---|
| 生成+存储 | `services/pay_params.py::save_pay_params`（解析 URL → build → upsert，fail-soft） |
| 写入点（与 alipay_cashier_url 同步） | ① `pay_session.ensure_pay_session`（静态构造，source=static-config）② `cashier_mint._fill_back`（自动铸造回填，source=protocol-mint / frida-mint）③ `POST /orders/{order_no}/cashier-url`（人工回填，source=manual） |
| 随单下发 | `pay_link_payload_with_session`（下单/续付/switch 响应带 `pay_params` + `pay_param_str`） |
| 轮询透出 | `GET /orders/{order_no}/cashier`（mint_status 增加 pay_params / pay_param_str） |
| 显式读取端点 | `GET /orders/{order_no}/pay-params`（generated + 解析对象 + 原文） |
| Python 脚本消费 | `scripts/read_pay_params.py`（零依赖 CLI，只读打开 app.db） |

## 4. Python 支付脚本用法

```bash
python scripts/read_pay_params.py <order_no>                 # 紧凑 JSON 原文（与库内逐字一致）
python scripts/read_pay_params.py <order_no> --field session # 取单参数
python scripts/read_pay_params.py <order_no> --url           # 只取原始收银台 URL
python scripts/read_pay_params.py --url-parse "<cashier_url>"
```

```python
from read_pay_params import load_param_str, parse_cashier_url, fetch_param_str
doc = load_param_str(fetch_param_str("202609280910110026101743116"))
sess, utdid, tid = (doc["params"][k] for k in ("session", "utdid", "tid"))
```

服务端同契约实现：`account_system/server/services/pay_params.py`（`parse_cashier_url` /
`build_param_str` / `save_pay_params` / `load_pay_param_str` / `payload_fields`）。
两处契约常量（v=1、必需三元组、域/路径）勿漂移。

## 5. 前端（/ops/order 支付卡，Element 1）

人工模式面板在 order_str 折叠面板下方新增「官方收银台支付参数串（pay_params）」折叠面板：
- 下单/续付响应带 `pay_param_str` 即时填充；否则 3s 轮询 `orderCashier`（约 60s 上限，
  到手即停）——官方收银台链接为异步铸造（protocol/frida，约 10-30s）。
- 面板 title 显示字符数与状态 tag（已捕获 / 铸造中…）；textarea 展示 pretty JSON；
  「复制支付参数串」复制紧凑原文（与库内存储一致，可直接投喂支付脚本）。
- 支付窗口过期 / 再下一单 / 组件卸载均停止轮询。

## 6. 验证

- 离线回归：`account_system/server/test_pay_params_offline.py`（9 用例：建表/解析/往返/
  upsert/静态构造联动/_fill_back 联动/回填端点/pay-params 端点/下发与坏数据兜底）
- 相邻套件回归：`test_payportal_offline.py` 20/20 通过（未破坏既有链路）
- 生效条件：重启主 API（8000）与收银台进程（8010）——`create_all` 自动建表。
