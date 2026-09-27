# 路线 ② 产出：test 环境字段谱系（2026-09-22，RK3588S 设备）

> 证据来源：mitmproxy 反向模式 + SNI 路由解密的 Dart 业务流量（capture/chagee_dart_reverse2_20260922.flows）+ 设备 SP 只读交叉验证。
> 敏感值处理：sk/签名样本为 test 环境短时效值，仅作学习对拍；不含真实账号 token。

## 抓取链路（本轮核心工程成果）

```text
Dart(Dio) --DNS(/system/etc/hosts 指向 127.0.0.1)--> 127.0.0.1:443
  --adb reverse tcp:443--> 宿主 mitmdump :8443
  --mode reverse + scripts/sni_route_addon.py（按 client SNI 改写 upstream）--> 真实服务器
CA：mitmproxy CA 已写入 /system/etc/security/cacerts/c8750f0d.0（备份在 /data/local/tmp/）
```

关键认知：Dart `findProxyFromEnvironment` 不读系统代理（9/10 已结案），系统代理只覆盖原生 SDK（极光/易盾/高德/神策，走 :8080 常规代理模式）。Dart 直连流量的正解 = hosts 重定向 + reverse 模式 + SNI 路由，全程无需改 APK、无需新二进制。

## 环境判定（运行时实证）

- `initEnv` 兜底 test（`c_debug_env` 键缺省）→ 实际请求全部落在 `test-gw.chagee.com` / `test-gj-api.bwcj.com`。
- SP 落盘键名带 `test-` 前缀（`flutter.test-chagee_encrypt_sp_key`），与静态分析的 `putStringEnv` 行为一致。

## 请求头谱系（Dio 层，全量）

```text
ua: Dart/2.12 (dart:io)          ← 固定旧版本串（兼容头）
user-agent: Dart/3.6 (dart:io)   ← 真实引擎版本
avc: 638                          ← versionCode
apv: 1.0.0                        ← versionName
tcode: CHAGEE
channel: APP
os: android
aid: 100001
language: zh_CN
region: CN
devicetimezoneregion: Asia/Shanghai
content-type: application/json
```

注意：未登录启动期的 `/api/*` 请求**没有 sign 头**（skin/info、countryInfo、multi-lang、navigation 均无）——sign 的触发条件比静态预期窄，需路线 ③ 结合 `_handleRequestPostBody` 反汇编确认触发面。

## getsk 接口（AES/HMAC 密钥下发）

```text
GET test-gj-api.bwcj.com/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001
→ 200 {"errcode":"0","errmsg":"处理成功","data":"<base64>","traceId":""}
data(base64) = 16 字符小写 hex 字符串 = sk（本轮样本：f7346022c2d57c81）
```

- 路径 `enctrypt` 为服务端原始拼写（非笔误修正项）。
- `code=CHAGEE_C_001`：App×渠道×平台的密钥码。
- **落盘交叉验证**：`FlutterSharedPreferences.xml` 中 `flutter.test-chagee_encrypt_sp_key` 的值 = getsk 返回的 base64 原串，一字不差；键名 `test-` 前缀同时证实运行环境。
- sk 生命周期：请求时读 SP（`_getEncryptKeySP`），与静态链路 `setEncryptKeySP` 闭环。

## airhub 配置 SDK 的独立签名体系（与主 API 不同）

```text
POST /chagee-airhub-config-server/chagee-airhub-config-server/config/queryList
body: {"sign":"<base64(32hex)>","appId":"HVRk4cIj7puaOPAB","returnType":"json","timestamp":"<ms>","groupKeys":[]}
```

- sign = base64(32 位小写 hex)，5 组 ts/sign 样本已存（见 flows）。
- 快速候选空间（md5/hmac-md5/hmac-sha × {sk, sk_b64, appId, 空} × 8 种消息拼装）**未命中** → airhub 使用自身嵌入密钥或更复杂的拼装，需路线 ③ 反汇编其 Dart 生成函数。
- test-gw 的 airhub 接口 404（重试 ~3 次/秒，107 次/分钟级），test 环境该服务已迁移/下线；logcat 的 `Airhub Failed` 与此互证。

## test-gw 存活状况

`/api/*` 系列在 test-gw 全部 404（skin/info、countryInfo、multi-lang、navigation 装修接口）——test 网关大部分业务路由已不在。**未登录期能拿到的明文样本已拿全**；后续要么切 `c_debug_env`（SP 写 `release` 打生产），要么直接进路线 ③ 纯算复刻。

## 样本资产

- `capture/chagee_dart_reverse2_20260922.flows`：Dart 业务流量（反向+SNI 模式）
- `capture/chagee_mitm_20260922.flows`：原生 SDK 流量（常规代理模式，含神策/极光/高德/易盾）
- 设备回滚：`cp /data/local/tmp/hosts.bak /system/etc/hosts`；`settings delete global http_proxy`；`adb reverse --remove tcp:443`

## 路线 ③ 第一阶段成果（2026-09-22 下午追加）

### airhub sign 算法（已确定性复刻，8778/8778 全通过）

```text
AirHubSDK::_generateSign @0x5900b4（chagee_devkit/airhub/chagee_airhub.dart）
parts = ["".join(groupKeys), appId, "json", timestamp, secret]
sign  = base64( md5_hexdigest( utf8(concat(parts)) ) )      ← 纯 MD5，无 HMAC
timestamp = DateTime.now().microsecondsSinceEpoch ~/ 1000 的十进制字符串
```

- 凭据按环境硬编码于 `chagee_global_app/init.dart @0x806644-0x806710`（dev/test/uat/prod 四套 appId+secret+groupKey，完整表在 `scripts/chagee_protocol.py: AIRHUB_ENVS`；secret 是 JWT，payload 自述 `{"appSecret":"[\"gk25\"]",...}`）。
- 验证：`python scripts/chagee_protocol.py verify-airhub capture/chagee_dart_reverse2_20260922.flows` → **8778 ok / 0 mismatch**（含 groupKeys=['gk6'] 非空前缀样本）。

### getsk 宿主直连复现（协议级复刻首个里程碑）

用还原的 13 个固定头从宿主 Python 直接 `GET test-gj-api.bwcj.com/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001` → 200，data 与设备侧一致（`f7346022c2d57c81`）——**首次在 App 之外独立完成一次真实业务请求**。

### 响应包络（所有业务响应统一）

```json
{"errcode":"0","errmsg":"处理成功","data":...,"thirdTraceId":"","globalTicket":"<32hex>","timestamp":...}
```

`globalTicket` 为每响应一枚的 32 位 hex 票据（风控关联，`docs/不走流量与验证码诊断.md` 的 code=="0" 判定即对应 errcode 字段体系）。

### test 环境端点存活图谱（截至 2026-09-22）

| 端点 | 状态 |
|---|---|
| `test-gj-api /encrypt-server/enctrypt/api/getsk` | ✅ 200 |
| `test-gw /chagee-navigation-web/.../store/cityList` | ✅ 200（真实城市数据） |
| `test-gw /chagee-navigation-web/.../decoration/pageInfo` | ✅ 偶发 200（531 试 1 成） |
| `test-gw /.../store/list`、`store/reverseGeo`、`skin/info`、`countryInfo`、`multi-lang`、`adv/page-show`、`im-user-device-info/report`、`getDecorationData`、airhub `queryList` | ❌ 404 |

store/list 请求体已捕获（`{latitude,longitude,pageNum,pageSize,cityCode,businessType,channelCode:"android"}`），待路由恢复即可复刻。

## 登录态协议链 + Python 独立复刻终验（2026-09-22 晚，生产环境）

### Authorization 机制（静态+动态双证）
- **`Authorization` 头 = 608B 裸 JWT，无 Bearer 前缀**。静态链：`NetHeaderService.headers @0xb49c84` → `MYLoginService::getLoginToken @0xb7f4c4` → `SpUtil.getString("login_userLoginToken")` 原样放入。
- 头合并优先级：dio options.headers < NetHeaderService 公共头（覆盖同名）< `extra["customHeaders"]`（最高）。
- 无 refresh 机制：401 或业务码 `12320120400401` → `ChageeNetNotifier` 通知 UI 踢回登录。
- 登出：`/user-client/auth/logout` + `cleanToken` 清 SP 三键（login_userLoginToken/login_type/login_isNewUser）。

### 生产登录态流量（capture/chagee_prod_dart.flows）
- `GET gw.chagee.com/user-client/customer/userInfo/query` **200**：authorization(608B JWT)/sk/uuid/cid 头实测，响应含完整会员资料。
- 生产网关无 test 专属路由（countryInfo/skin/info 等直连亦 404）；登录态核心路由在 `/user-client/*` 与 `chagee-*` 服务。
- **响应加密字段实测（RT7 动态闭环）**：响应 `mobileEncrypt` = `YUUjd1xuMwOdN/NgckaJ6Q==`（与登录请求密文相同），用 sk 派生 key 的 AES-ECB 解密成功=完整手机号。

### Python 独立复刻终验（协议化最终闭环，用户批准）
- 从设备 SP 读 token（608B JWT，全程不落档）→ `ChageeProtocol.query_user_info()` 以自构 13+3 头（含裸 JWT Authorization + sk）直调生产 `userInfo/query`。
- **结果：errcode=0 处理成功，customerId/昵称/等级与 App 抓包响应完全一致**——服务端接受了完全独立于 App 构造的请求，协议复刻全链路闭环。

### 全链路协议栈终态
```text
getsk(code=CHAGEE_C_001) → sk(跨环境同值) → AES-128-ECB 字段加密(PKCS7, key=sk ASCII)
  + HMAC-SHA1 sign(secretKey 环境双值, signFields 子集)   [按接口触发]
  + 13 公共头 + uuid/cid/sk + Authorization(裸 JWT)
  → 生产服务器接受（App 与 Python 独立客户端双重验证）
  ← 响应包络 {errcode,errmsg,data,globalTicket}，含加密字段可用同 key 解密
```

### 设备在位状态（用户选择：全部保留）
- App：真实账号已登录，release 生产环境（SP `c_debug_env=release`，备份在 `/data/local/tmp/FlutterSharedPreferences.xml.bak`）
- 抓包链路在线：CA `c8750f0d.0` 在系统库；hosts 含 4 条生产域名→127.0.0.1；全局代理 127.0.0.1:8080；adb reverse 8080→8080、443→8443；宿主 mitmdump 双实例（8080 常规/8443 reverse+SNI `tls_clienthello` 版）
- 回滚：`settings delete global http_proxy` + `cp /data/local/tmp/hosts.bak /system/etc/hosts` + `rm /system/etc/security/cacerts/c8750f0d.0` + `adb reverse --remove-all`；回 test 环境=停 App 后删 SP `c_debug_env` 键
- 敏感数据边界：token/手机号明文只存在于 capture/ 原始 flows 与设备 SP；docs 一律脱敏

#### 断线重连实录（2026-09-22 深夜追加）
- 设备公网 IP 漂移：`125.109.27.7:56915` → **`39.174.221.6:56915`**（同端口同指纹，RK3588S 云手机重新拨号）。`adb connect` 即恢复。
- **断线后设备侧全部保留、宿主侧全部失效**：SP（登录 token 608B/sk/环境键）、系统 CA、hosts 重定向、App 进程均在；`adb reverse --list` 空、宿主 mitmdump 全退、全局代理被清（`:0`）。hosts 仍指 127.0.0.1 而隧道已死 ⇒ **App 业务流量黑洞**（此窗口内 App 网络不可用）。
- 重建顺序（实测可复用）：宿主起 mitmdump 双实例 → `adb reverse tcp:443 tcp:8443` + `tcp:8080 tcp:8080` → `settings put global http_proxy 127.0.0.1:8080` → 探针 `curl -X POST https://gw.chagee.com/.../cityList` 应 200 → `am force-stop` + monkey 冷重启 App。
- 重建后验证：cityList 探针 200；App 回到登录态首页（"Hi，茶友"+3 优惠券，UI dump 证实）；8443 实例收到 navigation/multi-lang/skin/messaging/getsk 全套业务流，8080 实例收到 Sentry/神策/易盾原生流。
- 已知现象：`api-cn.chagee.com`（airhub）经 mitmdump 链 TLS 握手被服务端关闭（宿主 curl 直连同端点正常）；App 侧 airhub 无限重试但核心业务不受影响，与 test 环境 airhub 404 的良性降级同构。

## 路线 ③ 第二阶段：主 API sign + AES 全破解（2026-09-22 傍晚，登录流程触发）

### 触发方式
登录页输入占位手机号 19900000000（test 环境不投递真实短信，9/10 已证）→ 勾协议 → 获取验证码 → `POST /user-client/message/send` 全量捕获（响应 404 无碍，客户端构造已完成）。

### 新增请求头（登录态/敏感接口）
`uuid`、`cid`（同一安装标识）、`sk`（base64 形态，与 SP 一致）。

### 主 API sign（wire 精确命中 + 反汇编全链证据）

```text
ChageeInterceptor::generateSign @0xb48da0
msg  = signFields 子集按 key 排序拼 "k1=v1&k2=v2"（值为加密后的最终值）
sign = base64( HMAC-SHA1( utf8(secretKey), utf8(msg) ) ).trim()
secretKey = EncryptUtils::getSecretKey @0x8b35f0 硬编码双值：
  env == "release" → 9b83336464f74e148d8bd0bdcaa5f06e
  其他（dev/test/uat）→ 686c9567b5b9e0a5ff6e0e4df88076bd
登录验证码请求 signFields = ["sendObj","sid","timestamp"]（phone_login_bloc.dart @0x8da140）
```

验证样本：`HMAC-SHA1(686c…76bd, "sendObj=Eb4a8EnsqITU1KDkQ4YFCQ==&sid=e55a9fa2-…&timestamp=1790066157107")` → base64 = `GJczOhmMRhbL6dDmUTqIruzg5GE=`（与 wire 一字不差）。

### 字段级 AES（密文解密验证）

```text
buildEncryptedDataFromOriginal @0xb49234
key = base64decode(sk) → 16 字节 ASCII（f7346022c2d57c81）→ AES-128
模式 = ECB（无 IV 前缀；或 CBC/IV=0，单块样本两者等价，RT6）
padding = PKCS7；输出 base64
requestEncryptFields（登录）= ["sendObj","mobile"]
验证：Eb4a8EnsqITU1KDkQ4YFCQ== →解密→ "19900000000"（padding 合法）
```

### 端到端重组（决定性验收）
从纯输入（手机号/sid/timestamp/sk）重建 message/send 完整请求体（加密+签名+字段序），**与 wire 字节级一致**。`chagee_protocol.py` 的 `build_login_sms_body` 即该实现，类级自检通过；airhub 回归同步增至 13285/0。

### 剩余（RT6-RT8）— 已于同日傍晚全部结案

- **RT6 ✅ ECB 定论（静态铁证）**：`AES::AES @0xb2237c` 默认 mode 取常量池实例 `Obj!AESMode@c13261`，objs.txt 标注 `off_8=3, "ecb"` → 变换串 `"AES/ECB/PKCS7"`。key 构造 `Key.fromUtf8(utf8Decode(base64Decode(sk)))` 同步静态确认。**此 app 内置的 encrypt 包默认值即 ECB**（上游 encrypt 包默认 SIC）。
- **RT7 ✅ 响应解密静态确认**：`_decryptResponseIfNeeded` 侧构造完全相同（同 key 同 ECB/PKCS7）。实测响应（message/send、login/sms）data 均为明文——responseEncryptFields 在这些接口未触发。
- **RT8 ✅ 3/3 组独立 wire 样本**：ts=1790066157107 / 1790066158157 / 1790073693699 全部命中同一公式。

### 无 token 浏览链实测（2026-09-22 深夜追加，生产 gw.chagee.com）

**结论：门店 + 商品 SKU 菜单全链路无 token 可用。** 只需 13 公共头 + uuid/cid（无 authorization/sk/sign），Python 直连生产网关：

| 接口 | 请求 | 结果 |
|---|---|---|
| `POST /chagee-navigation-web/api/navigation/store/cityList` | 空 body | 200，22 城市分组 |
| `POST /chagee-navigation-web/api/navigation/store/list` | `{latitude,longitude,pageNum,pageSize,cityCode:"3301",businessType:"1",channelCode:"android"}` | 200，杭州 total=198，data={total,pageList}，首店 storeNo=CN00529 |
| `POST /chagee-navigation-web/api/navigation/goods/storeGoodsMenu` | `{storeNo:"CN00529",saleType:"1",saleChannel:"2"}`（userId 未登录可省） | 200，14 分类 / 82 SPU（含 showPriceStart/stock/imageUrlList/skuIdList） |
| `POST /chagee-navigation-web/api/navigation/goods/detail` | `{spuId,storeNo,saleType:"1",saleChannel:"2"}` | 200，specInfos[].specOptions[]（杯型大/中杯 specOptionId）+ skuInfos[]（skuId/itemSkuId/barCode/salePrice/stock/specOptionInfos 规格组合） |

- 参数依据：`ChageeMenuPageCubit::queryMenuData @0x5d91a4`（storeGoodsMenu body 字面量表：storeNo/saleType="1"/saleChannel="2"/userId=getUserInfo()?.field_7，**未登录时 userId=NULL**）与 `product_detail_cubit.dart @0x7d7fd4`（goods/detail：spuId/storeNo/saleChannel/saleType）。
- 这两个请求均未配置 extra（signFields/requestEncryptFields），与"未登录期 /api/* 无 sign"一致；navigation-web 系全部为明文 JSON 浏览接口。
- 城市码为 4 位地级市码（3301=杭州）；latitude/longitude 可传数值。

### 菜单浏览链接口化（scripts/chagee_menu_api.py）

接口已封装为 `ChageeMenuApi`（无 token 客户端，M1-M7 全端点 + 翻页迭代器 + 商品/SKU 拍平 + 快照保存），参数静态依据与实测记录见脚本头注释。离线单测 `tests/test_menu_api.py` 6 项（头谱系/guest body 语义/拍平/错误码）。全量快照实测（生产，CN00529）：

```text
python scripts/chagee_menu_api.py --city 3301 --store CN00529 --with-sku --out output/menu-CN00529
→ 分类 14 / 商品 82 / SKU 94 / 城市门店 198
产物：01_cityList / 02_store_list / 03_storeGoodsMenu / 04_goods_details (raw JSON)
     + goods_flat.csv（商品行）+ sku_flat.csv（SKU 行，spec 拼规格组合，UTF-8 BOM）
```

补充参数表（AOT 还原）：store/detail body={latitude,longitude,storeNo,businessType:"1",userId,channelCode:"android"}（`StoreDetailCubit @0x98a1ec`）；getNearestStoreDetail body 含 {longitude,latitude}（`ChageeMenuPageCubit @0x5da304`）；reverseGeo 经纬度为字符串（flows 实测）。

### 重大修正：SNI 路由缺陷与 test-gw 真实状态（傍晚会话）

1. **下午的 SNI 路由 addon 有隐性缺陷**：在 `request` hook 里改 `flow.server_conn.address` 从未生效（reverse 模式自身 hook 会覆盖目标），**所有流量实际都发给了 reverse 默认上游 test-gj-api**。getsk/cityList 恰好 test-gj-api 也服务所以"看起来正常"；airhub/store-list 等只存在于 test-gw → 假 404 → 错误得出"test-gw 大面积 404"结论。
2. **正解**：`tls_clienthello` hook + `data.context.server.address = (sni, 443)` + `connection_strategy=lazy`（缺 lazy 会报 "Cannot change server.address on open connection"；旧版属性 `data.server_conn` 在 mitmproxy 12 已不存在）。修复后 curl 经隧道访问 countryInfo = **200**。
3. **修正后的 test-gw 端点图谱：全部 200**。airhub queryList 返回真实业务错误 `9002010000001 "APPID错误或已停用"`（test 环境 appId 被停用，App 因此无限重试）。
4. **`POST /user-client/auth/login/sms`（登录校验）**：body `{"phoneCode":"86","smsCode":"888888","mobile":"<AES>","storeNo":""}`——**smsCode 明文、无 sign**（各接口 extra 配置不同）；假码获真实风控响应 `errcode=1232020200004 "验证码错误"`。
5. **message/send 在真实路由下 200**：`{sendFlag:true,dispose:{action:"pass"}}`（test 环境受理但不投递，与 9/10 结论一致）。
