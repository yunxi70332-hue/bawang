# 折扣率券（"7折"/"7.8折"）面额误解析——全链路修复定案

日期：2026-09-28 · 分支：`fix/rate-coupon-face` · 事故账号：茶壶#8

## 一、事故与根因

### 现象
带折扣率券下单被一致性断言拦截（**提交前拦截，未成单未扣券，安全**）：

```
409 下单一致性校验失败，已中止提交: 抵扣合计不符: ΣdiscountList=7 totalDiscountAmount=6.6
```

oplog 四次失败（oplog.db id 4078/4093/4095/4098），数值全部精确吻合折率口径：

| 商品 | 总价 | 券 | 本地错算 | 服务端实扣 |
|---|---|---|---|---|
| 桂馥秋梨冰奶中杯 | 21 | 7折 | 7 | **6.30** = 21×0.3 |
| 桂馥兰香冰酿大杯 | 22 | 7折 | 7 | **6.60** = 22×0.3 |
| 桂馥兰香冰酿大杯 | 22 | 7.8折 | 7.8 | **4.84** = 22×0.22 |

### 根因（四层传播）
1. **解析层**：`ChageeTradeApi.coupon_face()`（scripts/chagee_trade_api.py）用首数字正则解析 benefitText，`"7折"`→¥7。`orders.py:_parse_face()` 是同款正则的独立复制品。
2. **交易层**：`order_create` 复跑 `settle_direct` 后**丢弃**服务端回填的真实抵扣行（`SettleResult.discount_list` 此前无任何生产消费方），用 `pick_coupon` 本地重猜构造下单折扣行 → `assert_settle_consistency` Σ≠totalDiscountAmount 拦截。
3. **数据层**：`_upsert_coupon` 把错值写进 `coupon_records.amount`（7折券存 "7"），档案库展示「7元」。
4. **展示层**：下单工作台 `faceOf/deductionOf/est` 只认「N元」，折扣率券显示「¥—」、无预估；试算预览 `estimated_pay`/推荐抵扣同样错。

### 修复总原则
**服务端回填值为唯一权威**：settlePrice 响应的 `discountList[].discountAmount` 是茶姬按券型算好的真实抵扣（wire 证据：output/trade_samples_20260926.json、output/cart_capture_settle_20260926.json；docs/order_wire_field_analysis_20260927.md 折扣行三处同构表）。下单行直接取它；本地折率换算仅做无回填时兜底。

## 二、改动清单

### 后端引擎（scripts/chagee_trade_api.py）
- 新增 `coupon_kind(entry)`：`rate`（"N折"）/ `fixed`（"N元"）/ `other`（免次卡等）。
- 新增 `expected_deduction(entry, total)`：rate → `total×(10−n)/10`（2位小数）；fixed → `min(n,total)`；other → 0。
- `pick_coupon` 重写：覆盖判断限定 fixed 券（rate 恒不覆盖总价）；非覆盖分支按预估抵扣统一排序（fixed 券行为与旧版完全一致——非覆盖时 deduction==face，排序不变）。
- `SettleResult.discount_rows_for(coupon_code)`：从服务端回填行按 discountId 匹配，拷贝并强制 `currentSelect=True`。

### 后端路由（account_system/server/routers/orders.py）
- `order_create`：`rows = settle.discount_rows_for(code) or [build_discount_row(entry, expected_deduction(...))]`；使用日志 deduction 改记服务端 `totalDiscountAmount`。
- `order_settle` 预览：优先服务端回填（discountList 非空且 totalDiscountAmount>0 时直接取服务端事实与对应券条目），无回填回退折率感知 pick_coupon。
- `_parse_face`：rate 券返回空串（删除重复正则，改走 coupon_kind）。
- `GET /coupons/search`：items 新增 `coupon_kind` + `amount_display`（"7折"/"20元"/原文），`amount` 照旧透传。

### 实弹脚本（scripts/test_voucher_order_flow.py）
- 下单行改 `discount_rows_for` 兜底链；应付预估/券抵展示/落库 amount 全部券型感知。

### 前端（web/src，已构建 dist）
- `CouponQueryView.vue` 档案库面额列：渲染 `amount_display`（回退 amount+'元'）。
- `OrderWorkbenchView.vue`：`faceLabel(c)`（rate→"7折"、fixed→"¥20"）；`deductionOf` 折率计算；`est` 本地估算支持「N折」；提示文案更新。

### 数据修复
- `app.db` 备份至 `app.db.bak_rate_coupon_20260928`；11 条折率券存量行 `amount` 置空（7折×3、7.5折、7.8折、8折×6）。展示正确性由查询时派生的 amount_display 保证，后续 upsert 自然保持。

## 三、测试

- `tests/test_trade_api.py` +6：coupon_kind 分类、expected_deduction 事故数值精确复现（6.30/6.60/4.84）、rate 永不覆盖、混合券池按抵扣比较、服务端回填行过断言+本地 7 元行必被拦截。**30 passed**。
- `account_system/server/test_orders_offline.py` +1：`test_create_with_rate_coupon` 全链路回归（FakeClient 折率 wire 回放：预览 6.60/15.40/partial → create partial 成单 → OrderRecord pay_amount 15.4 → 使用日志 deduction 6.60 → 档案 amount 空串）。**13 passed**。
- 回归基线：test_coupons_offline 5 passed、test_reconcile_offline 11 passed、test_coupon_api 9 passed（offline 套件须逐个单独跑，组合跑会因共享 database 模块状态冲突）。

## 四、在线验证状态

- ✅ 8000 服务已重启（统一切 `.venv_verify`，原进程误用 TeleAgent python）。
- ✅ `GET /coupons/search` 实测：11 张折率券全部 `kind=rate, amount='', display='7折/7.5折/7.8折/8折'`。
- ⏳ 在线试算预览（茶壶#8 事故商品复刻）：22:42 门店打烊时段被 91010009 拒（环境因素，请求形态与当日 21:49 成功单一致）——**待营业时段重试**。
- ⏳ 真实下单终验（茶壶#8 + 7折券 + ¥22 大杯 → 抵扣 6.6/应付 15.4）：会产生真实待支付单，**待用户确认后执行**。

## 五、关联
- 事故诊断记忆：rate-coupon-consistency-bug-20260928
- wire 依据：docs/order_wire_field_analysis_20260927.md（券三处统一 / 折扣行三处同构）
- 券同步落库链路：F4 券查询 + F5 试算 upsert（无后台线程）
