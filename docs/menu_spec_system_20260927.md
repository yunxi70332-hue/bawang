# 本地菜单规格库体系（2026-09-27 定案）

> 需求：客户代下单平台仅传文案规格（"大杯/少冰/半糖"），协议链需要数字 ID；
> 且需及时反映地区限定饮品与新品发售，特殊情况下仍能准确命中规格。

## 前置实证（体系根基）

1. **规格主数据是全局字典**：甜度组 `623882672850116609`（半糖=`623882672850116613`）、
   温度组 `745317722624679942`、杯型 specId `653599312273510400` 在杭州 CN00529 快照与
   佛山/衡阳/南京下单 wire（2026-09-26）完全一致 → 可本地缓存。
2. **"半糖"是官方选项**（此前"无精确对应"的说法系 wire 样本覆盖不足）；
   但**选项集是商品级配置**：青青糯山甜度 5 项含半糖，伯牙绝弦仅 4 项无半糖
   → 必须按门店×SPU 维度解析，不能只靠全局字典。
3. **同名选项存在双 ID**：少糖有全局版 `623882672850116612` 与商品专用版
   `1305862012887048203`（新品用新甜度组）→ 再次佐证 SPU 级解析。
4. 权威数据源 = 游客菜单接口（无登录态）：`storeGoodsMenu`（SPU 清单+价格）+
   `goods/detail`（specInfos/attributeInfos/attrOptions/extraInfos/skuInfos 全量）。
   注意 detail 字段形态：属性组选项数组键为 **attrOptions**、选项名为 **name**（组名带
   前导空格如 `" 温度"`，须归一化）；多分类展示致同一 SPU 在菜单中重复出现（82 行 62 唯一）。

## 架构

```text
权威源（游客菜单 API）
   │ refresh_store_menu（全店：清单→逐 SPU detail→落三表）
   ▼
menu_spec_options   全局规格主数据（kind×group×option 唯一；新增行=新品/新选项信号）
menu_goods_cache    门店×SPU 快照（sku_index: skuId→{price,stock,specs}；
                    spec/attribute/extra 拍平组；raw=原始 detail 供 /goods 回放）
menu_refresh_logs   刷新审计（trigger=manual|scheduled|ttl_miss + diff 摘要：
                    new_spus/new_spec_options/changed——地区限定与新品追踪入口）

读取链（三层兜底，宁拒不猜）：
  L1 本地库命中（TTL 内零线上调用）
  L2 陈旧/冷库 → 实时回源 storeGoodsMenu+detail 并 write-through
  L3 文案解析：归一化精确+别名 → 包含式模糊 → 歧义(0/多候选)抛 SpecResolveError 列候选
  必选组兜底 = App 官方语义（服务端要求每组必选；defaulted 优先，无默认自动选首项，
  auto_filled 透出供人工复核）；无法识别的文案必先报错，不被缺组错误掩盖
```

## 更新机制（明确周期）

| 机制 | 周期/触发 | 控制 |
|---|---|---|
| 单 SPU write-through | fetched_at 超 TTL（读取时惰性触发） | `CHAGEE_MENU_TTL_SECONDS`（默认 86400） |
| 每日定时全店刷新 | 活跃门店 = 有订单记录 ∪ 已缓存门店 | `CHAGEE_MENU_REFRESH_INTERVAL_SECONDS`（默认 86400；<=0 禁用，**离线测试必须置 0**） |
| 手动刷新 | `POST /api/ops/menu/spec/refresh`（单店或 all_active） | 审计 action=`menu.spec_refresh` |

## 端点（routers/ops.py，perm=feature:menu）

- `POST /api/ops/menu/spec/refresh` — body `{store_no}` 或 `{all_active:true}`，返回 diff 摘要
- `GET /api/ops/menu/spec/status` — 库规模/陈旧行/最近 10 次刷新（含 new_spus 明细）
- `GET /api/ops/menu/spec/resolve?store=&sku_id=&spec=大杯/少冰/半糖` — 兜底命中链入口：
  skuId（客户平台 linkId）或 spu_id 二选一；返回 `{spec_list, attribute_list, resolved,
  auto_filled}`；歧义 422 + 候选列表
- `GET /api/ops/goods` — **改造为本地优先**（响应结构不变，前端零改动）

## 别名表（保守收录，错温比缺温贵）

`无糖/0糖/不加糖/不要糖 → 不另外加糖`；`正常糖/全糖 → 标准糖`；`正常冰/标准冰度 → 标准冰`。
温度类近似词（如"温热"≠"热"）一律不收。

## 客户平台对接用法（一次调用）

`GET /resolve?store=<解析出的storeNo>&sku_id=<linkId>&spec=<spec文案>`
→ `spec_list/attribute_list` 直接构成 F5 `OrderSettleRequest.spec_list/attribute_list`；
`auto_filled` 非空时提示客户确认（App 同款默认值）。

## 验证（离线，CN00529 真实快照回放，零网络）

`test_menu_spec_offline.py` 11/11 绿：落库/幂等/本地优先/TTL 回源/端点结构/
skuId 直查 L1+L2/客户场景（青青糯山 skuId=客户 linkId，"大杯/少冰/半糖"→613）/
别名（无糖→610）/歧义拒猜/商品级选项集差异（伯牙绝弦无半糖→拒）。
回归：orders 12/12、coupons 5/5、reconcile 11/11（各自独立进程跑）。

首刷真实 diff 样例（追踪价值佐证）：62 唯一 SPU 中发现
`风荷映月⎡浙江限定⎦`、`橘柚荷龙井⎡浙江限定⎦`（地区限定）与 32 个新规格选项入库。
