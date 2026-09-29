# 代理出口管理（系统设置 · 代理配置）

> 2026-09-29 交付。目标：生产部署在香港，所有茶姬账号域操作（登录 / 账号信息 / 优惠券 /
> 下单 / 取餐码）优先经大陆 IP 代理出口；代理不可用时自动无缝回退服务器直连，恢复后自动切回。

## 1. 功能总览

| 能力 | 说明 |
| --- | --- |
| 配置界面 | 系统管理 → 系统设置（权限 `settings:manage`，admin 内置角色自动拥有） |
| 代理来源 | ① 提取 API（服务商 API 取 ip:port，TTL 到期/失败自动轮换）② 固定代理（host:port） |
| 协议 | HTTP/HTTPS 代理（CONNECT 隧道，账密认证预置 `Proxy-authorization`）；SOCKS5（PySocks） |
| 数据格式 | TXT（`ip:port[:user:pass]` 多分隔符）/ JSON（海量IP 实测形态 + 通用字段名变体） |
| 路由范围 | 命中 `route_domains`（默认 `chagee.com` / `bwcj.com` 后缀）的出站请求才接管，其余直连 |
| 无缝切换 | 请求期代理网络级异常 → 本次请求立即直连重试（调用方无感）；连续 2 次失败降级直连并唤醒监控轮换 |
| 状态检测 | 后台线程周期检测（默认 30s）+ SSE 实时推送（topic=proxy）+ 立即检测按钮；显示当前出口 IP/归属地/大陆标签 |
| 操作日志 | oplog：`proxy.config_save` / `proxy.fetch` / `proxy.switch`（切换/轮换/回退，WARN 级回退）/ `proxy.egress`（关键账号请求出口留痕，限频 90s） |

## 2. 实现要点

- **接管点**：`urllib.request.urlopen` monkeypatch（`server/services/net_proxy.py`）。
  `scripts/chagee_client.py`、`chagee_protocol.py`、`chagee_menu_api.py` 等全部茶姬域出站
  都经过它；支付（alipay/mobilegw）、webhook 等非茶姬域不受影响。
- **健康判定**：经代理真实访问 `https://gw.chagee.com/`（任何 HTTP 应答即活）。
  **不用** echo 站点判定——海量IP 隧道对 `myip.ipip.net` 回 `614 domain is black`。
- **出口身份**：优先采信服务商元数据（海量IP 提取响应的 `realIp` + `area`），
  无元数据时回落 echo 探测（ipip / ip.sb / ipinfo 依次尝试）。
- **大陆校验**：`require_mainland=true`（默认）时出口非大陆（含港澳台）视为无效候选。
- **状态机**：`proxy_active`（代理生效）/ `fallback_direct`（回退直连，WARN）/ `disabled` /
  `not_configured`；切换、轮换、回退全部落 oplog 并 SSE 推送。
- **配置存储**：`account_system/data/proxy_config.json`（双进程共享，主 API 保存后收银台
  进程下个检测周期自动重读）。测试重定向：`CHAGEE_PROXY_CONFIG`。

## 3. API（均需 `settings:manage`）

| 端点 | 说明 |
| --- | --- |
| `GET /api/ops/settings/proxy` | 配置（密码掩码 `***`）+ 状态快照 |
| `PUT /api/ops/settings/proxy` | 保存并立即检测生效（密码传 `***` = 不改） |
| `POST /api/ops/settings/proxy/test` | 干跑诊断：提取→CONNECT 隧道→经代理访问茶姬→大陆校验→直连基线（不落状态） |
| `POST /api/ops/settings/proxy/refresh` | 立即执行一个检测周期并返回最新状态 |
| `GET /api/ops/settings/proxy/logs` | 最近 `proxy.*` 操作日志 |

## 4. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CHAGEE_PROXY_CHECK_INTERVAL_SECONDS` | 30 | 0=不启动监控线程（conftest 离线测试约定；urlopen 补丁仍安装） |
| `CHAGEE_PROXY_CONFIG` | `data/proxy_config.json` | 配置文件路径（测试指向 `data/test_*.json`） |
| `CHAGEE_PROXY_HEALTHCHECK_URL` | `https://gw.chagee.com/` | 代理健康判定目标 |

## 5. 海量IP（hailiangip）接入实测结论（2026-09-29）

提取 API：`getIpEncrypt?dataType=0&encryptParam=...`（URL 已预置为系统默认值）

1. 白名单：提取 API 与代理使用均要求白名单（未加白时响应「对不起，您使用的IP地址不在白名单内」）。
   本机 180.153.160.45 已加白；**香港服务器上线前须把其出口 IP 也加入海量IP后台白名单**。
2. 响应（302 → ecs.hailiangip.com:8422，跟随即可）：
   `{"code":0,"data":[{"realIp":"116.16.17.15","area":"广东-梅州","ip":"36.156.10.7","port":13651}]}`
   —— `ip:port` 为代理入口，`realIp/area` 即出口身份，无需 echo 探测。
3. **域名黑名单**：隧道对 `myip.ipip.net` 回 `614 domain is black，请联系客服处理`；
   对未授权业务域名（含 gw.chagee.com）部分会话静默丢弃（CONNECT 无应答/超时）。
   ⚠️ **待办：联系海量IP客服把 `chagee.com`、`bwcj.com` 加入授权域名**，授权前部分会话
   无法承载茶姬流量（系统会自动轮换到可用会话或回退直连，不影响可用性，但代理命中率打折）。
4. 会话寿命约 60~90s（到期 `702 No BindIP`），系统按 TTL/失败自动轮换；每次提取消耗套餐
   配额，若套餐按量计费可在设置中调大「检测间隔」与「代理有效期」。

## 6. 测试

`server/test_proxy_offline.py`（10 用例，零网络）：提取解析 / 大陆判定 / 配置校验 / 保存掩码 /
补丁路由（代理成功、异常直连重试、HTTPError 不回退）/ 降级阈值 / egress 限频 / 状态机 /
settings 端点（RBAC 403/401、422、掩码往返）/ SSE topic 注册。

单独跑：`cd account_system/server && ../../.venv_verify/Scripts/python.exe -m pytest test_proxy_offline.py -q`

## 7. 运维备忘

- 依赖：PySocks（.venv_verify 与 TeleAgent runtime python 均已安装）。
- 主 API(8000) 与收银台(8010) 进程各自安装补丁与监控线程，共享同一配置文件。
- 收银台 8010 需重启一次以加载 net_proxy（本轮只重启了 8000）。
- 快速自检 CLI：`cd account_system/server && ../../.venv_verify/Scripts/python.exe -m services.net_proxy`
  （输出当前配置的干跑诊断 JSON）。
