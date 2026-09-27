# 霸王茶姬逆向项目知识库（2026-09-22 全会话沉淀）

> 范围：RK3588S 云手机新环境部署 → 三路线逆向 → 协议栈全破解 → 生产真实登录 → Python 独立复刻。
> 本文是"遇到的问题 + 产出结果"的结构化沉淀，证据细节见 `reverse_learning_log.md` 与 `field_genealogy_20260922.md`，算法实现在 `scripts/chagee_protocol.py`。

---

## 一、总览

**目标**：霸王茶姬 App（`com.chagee.application.cn` v638，Flutter AOT）的协议逆向与全流程复刻。

**设备演进**（三代战场）：

| 代 | 设备 | 关键能力 | 瓶颈 |
|---|---|---|---|
| 1 | 模拟器（x86_64+转译，Android 9） | 基线采集 | `/proc/maps` 被拒、无蜂窝、Frida 别扭 |
| 2 | TJ 云真机（Android 13，arm64，KSU） | root、真蜂窝 | **无 adb 通道**（仅 H5 WebSocket），frida 不可远控 |
| 3 | **RK3588S 云手机**（`125.109.27.7:56915`） | adb 直连 + root + remount | 公网明文 adb（只做学习） |

**最终成果一览**：

| 路线 | 成果 | 验证强度 |
|---|---|---|
| ① 内存完整性 | 运行期 libapp.so 与静态分析目标逐页一致 | **3125/3125 页 100%** |
| ② Dart 明文捕获 | hosts+reverse+SNI 抓包链路，全量业务流量解密 | 全部端点实测 |
| ③ 协议复刻 | 三套算法 + 头谱系 + 登录态 + Python 独立客户端 | airhub 8778/8778、主 API 4/4、端到端字节一致、**生产服务器接受 Python 自构请求** |
| 附加 | 生产真实登录闭环、test 短信通道定案 | 梯度 A/B 实验 |

---

## 二、核心成果（协议栈终态）

### 2.1 算法三件套

| 算法 | 公式 | 密钥 | 验证 |
|---|---|---|---|
| airhub sign | `base64(md5_hex(join(groupKeys)+appId+"json"+timestamp+secret))` | 四环境硬编码（`AIRHUB_ENVS`，secret 为 JWT） | 8778/8778 |
| 主 API sign | `base64(HMAC-SHA1(secretKey, sorted_kv(signFields))).trim()` | release=`9b83…f06e`；dev/test/uat=`686c…76bd`（`getSecretKey @0x8b35f0`） | 4/4 wire 样本 |
| 字段级 AES | `AES-128-ECB / PKCS7`，输出 base64 | `key = utf8(base64decode(sk))` = 16 字符 ASCII | 加解密双向 + 响应 `mobileEncrypt` 实测 |

### 2.2 请求链路

```text
getsk: GET {api}/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001   ← "enctrypt"为官方拼写
  → data = base64(16 位 hex) = sk（跨环境同值，SP 键 <env前缀>chagee_encrypt_sp_key 落盘）

请求 = 13 公共头（ua/user-agent/avc/apv/tcode/channel/os/aid/language/region/
        devicetimezoneregion/content-type + host）
      + [登录态] authorization(裸 JWT 无 Bearer，源 SP login_userLoginToken) / sk / uuid / cid
      + [按接口] body 字段 AES 加密（requestEncryptFields）+ sign（signFields）
响应 = {errcode, errmsg, data, thirdTraceId, globalTicket(32hex), timestamp}
      + [按接口] 加密字段（responseEncryptFields，同 key 可解）
```

### 2.3 关键机制结论

- **加密/签名按接口配置**：`extra["signFields"/"requestEncryptFields"/"responseEncryptFields"/"secretKey"/"sk"]` 由各业务模块经 `MeService::getEncryptExtra` 传入。登录验证码请求 signFields=`["sendObj","sid","timestamp"]`、requestEncryptFields=`["sendObj","mobile"]`；`auth/login/sms` 则 smsCode 明文且无 sign。
- **Authorization**：`NetHeaderService.headers @0xb49c84 → getLoginToken @0xb7f4c4 → SP`，裸值；合并优先级 dio < 公共头 < `extra["customHeaders"]`；**无 refresh**（401/`12320120400401` 踢回登录）；登出 `/user-client/auth/logout` + 清 SP 三键。
- **环境**：`initEnv` 兜底 test（`c_debug_env` 键缺省时）；release 需 SP 写入；`putStringEnv` 前缀 test-/uat-/dev-，**release 为空前缀**（环境探针技巧）。
- **短信通道**：test 环境受理（`sendFlag:true,action:"pass"`）但**不对真实号码投递**；生产真实送达。风控全程放行，与 root/抓包无关。
- **airhub**：test 环境 appId 被服务端停用（`9002010000001`）→ App 无限重试（~3 次/秒，13000+/小时样本量来源）。

---

## 三、问题与解决全记录

### A. 环境与工具链

| # | 问题 | 现象 | 解决 |
|---|---|---|---|
| A1 | Git Bash MSYS 路径转换 | `adb push /data/...` 被转成 `E:/Git/data/...`，`adb install` 把设备路径当本地路径 | 命令前加 `MSYS_NO_PATHCONV=1` |
| A2 | Git Bash `/tmp` 对 Windows 程序不可见 | push `/tmp/xx` 报 cannot stat | 用 Windows 可见路径（项目目录）中转 |
| A3 | GitHub 直连极慢 | frida-server 2 分钟仅 61KB | ghproxy.net 镜像 + `curl -C -` 断点续传（一次中断后续传成功）；xz 完整性校验 |
| A4 | mitmdump 不在 bash PATH | `command not found`（用户级安装） | 用全路径 `C:/Users/Administrator/AppData/Roaming/Python/Python314/Scripts/mitmdump.exe` |
| A5 | 原仓库路径失效 | `.mcp.json` 指向的 `E:\PythonCodeObject1` 本机不存在，3 个自建 MCP 不可用 | 改用本机全局 Python(3.14)+frida 16.6.6 client + adb 直接工作，不依赖 MCP |
| A6 | adb 公网断线 | 设备掉线后 reverse 丢失、宿主 mitmdump 全退 | 每次会话先 `adb connect` → 检查 `netstat` 监听 → 隧道 curl 探针验收，再干活 |
| A7 | frida 版本匹配 | client/server 必须一致 | 统一 16.6.6；server 用 `-l 127.0.0.1:27042` **仅 loopback**（公网设备必须），forward 用完精确删除 |

### B. 抓包链路（本项目最大坑群）

| # | 问题 | 现象 | 解决 |
|---|---|---|---|
| B1 | Dart 忽略系统代理 | 全局代理只覆盖原生 SDK（神策/极光/高德/易盾），业务 API 零捕获；`findProxyFromEnvironment` 只读环境变量，App 无 DEBUGGABLE 标志无法 wrap 注入 | **root 写 `/system/etc/hosts` 把业务域名指向 127.0.0.1 + `adb reverse tcp:443` + 宿主 mitmproxy reverse 模式 + SNI 路由**，无需改 APK/新二进制 |
| B2 | **SNI 路由隐性失效（最重要）** | `request` hook 里改 `flow.server_conn.address` 从未生效——reverse 模式自身 hook 覆盖目标，全部流量实发默认上游；"test-gw 大面积 404"是假象（getsk/cityList 恰好默认上游也服务） | 正解：**`tls_clienthello` hook + `data.context.server.address=(sni,443)` + `connection_strategy=lazy`**。教训：addon 日志打印≠路由生效，**必须用"上游独占端点"做路由探针**（airhub 只在 gw 存在，最灵敏） |
| B3 | mitmproxy 12 API 变更 | `data.server_conn` 属性不存在 → AttributeError | 改 `data.context.server` |
| B4 | 缺 lazy 策略 | "Cannot change server.address on open connection"（急切策略在建连后才触发 hello hook） | 必须加 `--set connection_strategy=lazy` |
| B5 | Flutter 首启弹窗链 | 隐私→通知→定位弹窗依次挡 UI，且隐私未同意时国内 SDK 全部延迟初始化（零 App 流量假象） | `uiautomator dump` 读 content-desc 定位控件 → `input tap` 逐个通过 |
| B6 | 无视觉环境下驱动 Flutter | 截图被工具转存 CDN 看不到 | uiautomator XML 拉到本地用 Python 解析（content-desc 承载语义，EditText 无标签需按坐标） |
| B7 | adb shell 内 grep 引号转义 | 嵌套引号被 bash 吃掉 | XML/文件一律 `adb pull` 到本地再解析 |
| B8 | `/proc/net/tcp` 是全 netns | 混入其他进程连接误判 | 用 `ss -tnp | grep pid=` 按进程过滤 |

### C. 静态与协议逆向

| # | 问题 | 现象 | 解决 |
|---|---|---|---|
| C1 | libapp.so "找不到映射" | `extractNativeLibs=false` 时 .so 不解压，**直接以 base.apk 切片映射**（maps 里只有 base.apk 行） | 本地解析 APK ZIP 本地头得数据偏移（如 libapp=0xbb0000，stored+4K 对齐），与 base.apk 映射区间求交集 |
| C2 | sign 算法候选空间大 | 直接暴力 md5/hmac×拼装组合未命中 | **回到反汇编拿精确拼装**：`generateSign` 只给框架（sorted-kv+Hmac+base64），密钥藏在 `extra["secretKey"]`→`getEncryptExtra`→`getSecretKey` 硬编码双值链里；signFields 子集过滤是关键（签名的不是全 body） |
| C3 | ECB vs CBC(IV=0) 单块无法区分 | 11 字节明文→16 字节密文两种模式等价 | 读 `AES::AES` 构造默认值：常量池 `AESMode@c13261`，objs.txt 标注 index3="ecb" → 定论 ECB（此 app 的 encrypt 包默认值被改为 ECB，上游默认 SIC） |
| C4 | 手抄样本引入假失配 | hex 两位转置（c8/8c）导致 1 条"失败" | 样本必须从 flows **程序化读取**，禁止手工转录 |
| C5 | `late static` 字段误导 | 硬编码初值函数不是真实入口（如 `ChageeEnv.selectEnv` 硬返回 uat） | 必须找 `StoreStaticField(offset)` 的赋值点（`initEnv` 才是真入口） |
| C6 | Dart 对象 hook 困难 | AOT 无符号，直接 hook Dart 函数成本高 | 改抓"wire 字节 + 反汇编"双证路线，Blutter RVA 只用于定位函数边界 |

### D. 业务与验证

| # | 问题 | 现象 | 解决 |
|---|---|---|---|
| D1 | 短信不达归因 | 真实号码收不到码，怀疑 root 检测/风控限投 | **梯度 A/B**：抓包全开→干净网络（留 CA）→全净（撤 CA），三级全部"受理成功但不达"→ 定案 test 通道不投递；生产反向验证（秒达） |
| D2 | 风控拦截误判 | 担心请求被拦 | 读响应语义：`action:"pass"`=放行；被拦应是 captcha/block/错误码；客户端根本不解析 `dispose` 字段（pp.txt 无此业务串），只看 errcode=="0" |
| D3 | 限流信息误读 | "60s内只能发送一次短信" | 这恰是**进入真实短信管道**的证据（限流器计数） |
| D4 | 环境切换验证 | 写了 c_debug_env 如何确认生效 | SP 键前缀探针：release 前缀为空 → 新增无前缀 `chagee_encrypt_sp_key`；生产 getsk 跨环境同 sk |
| D5 | 验证码请求重复触发 | UI 勾选协议的 tap 弹出协议确认框，send 未发出 | 每次 tap 后 dump UI 确认状态机位置，按实际界面重新定位按钮 |

---

## 四、可复用方法论

1. **Flutter Dart 流量抓取标准链路**（公网设备/无 adb 不可用时除外）：
   `hosts 重定向 + adb reverse tcp:443 + mitmproxy reverse + tls_clienthello SNI 路由 + lazy 策略 + 系统 CA（Flutter 只读系统库）`。原生 SDK 流量另走常规全局代理（两 mitmdump 并行，分文件落盘）。
2. **路由生效验证**：用"只存在于目标上游的端点"做探针；日志打印只证明 hook 执行，不证明结果。
3. **协议逆向最短闭环**：反汇编定框架 → 定位密钥/字段来源（extra 配置链）→ wire 单样本精验 → **端到端字节重组** → 独立客户端重放（服务端接受才算终验）。
4. **证据规则**：确定性纯算 ≥3 组任意输入对拍（本项目 airhub 8778、主 API 4 组）；样本程序化读取；终止性结论标注证据边界（哪些静态、哪些动态）。
5. **环境探针技巧**：带 env 前缀的 SP 键名直接暴露运行环境；`/proc/<pid>/maps` 实际连接 IP 与 DNS 对拍判域名归属（防沙箱 DNS 合成）。
6. **外向性动作分级**：切生产环境/真实短信/向生产发自构请求——逐级请示用户明确授权后执行；只读单次、不碰交易。
7. **敏感数据边界**：token/手机号明文只留 capture/ 原始文件与设备，docs 一律脱敏（`159****1290`、`<608B JWT>`）。
8. **会话韧性**：公网 adb 随时断——所有链路组件（connect/reverse/mitmdump/frida-server）的"检查-重建-探针验收"三步固化为会话开场动作。

---

## 五、脚本与资产索引

| 文件 | 说明 |
|---|---|
| `scripts/chagee_protocol.py` | 协议复刻主体：AIRHUB_ENVS 凭据表、airhub_sign、generate_sign(HMAC-SHA1 双密钥)、AES 字段加解密、build_login_sms_body（端到端字节级）、query_user_info（登录态直调）、verify-airhub 回归子命令 |
| `scripts/sni_route_addon.py` | mitmproxy SNI 路由 addon（tls_clienthello 版，含踩坑注释） |
| `scripts/dump_libapp_memory.py` | libapp.so 内存 dump 与逐页对拍（支持 extracted/apk-direct 双模式） |
| `scripts/verify_frida_env.py` | frida 链路连通性验收 |
| `capture/chagee_prod_dart.flows` | 生产登录态流量（userInfo/query 等，含 token——敏感） |
| `capture/chagee_dart_reverse*.flows` / `chagee_mitm*.flows` | test 环境 Dart 与原生 SDK 流量 |
| `capture/prod-login-success-20260922.png` | 生产登录成功证据截图 |
| `docs/reverse_learning_log.md` | 全程时间线日志（问题+证据最全） |
| `docs/field_genealogy_20260922.md` | 字段谱系与算法证据链 |
| `tools/frida/fs1666-arm64` + `c8750f0d.0` | frida-server 16.6.6 arm64 与 mitmproxy CA（设备侧部署源） |

## 六、设备在位状态与回滚（截至 2026-09-22 深夜）

**在位**：真实账号已登录；release 生产环境（SP 备份 `/data/local/tmp/FlutterSharedPreferences.xml.bak`）；抓包链路全开（CA `c8750f0d.0`、hosts 4 条生产域名、全局代理、reverse 8080/443、宿主双 mitmdump）；frida-server `/data/local/tmp/fs1666-arm64` loopback 27042。

**回滚**：
```bash
adb shell settings delete global http_proxy
adb shell cp /data/local/tmp/hosts.bak /system/etc/hosts
adb shell rm /system/etc/security/cacerts/c8750f0d.0
adb reverse --remove-all          # 回 test 环境：停 App 后删 SP c_debug_env 键
```

---

## 七、2026-09-23 增补：跨会话断线事故与验证（工具实锤）

新会话（新云手机 IP 39.174.221.6:56915，同端口）遇 App"网络异常"。逐层排查实锤：

- **根因**：上一会话结束 → `adb reverse` 全部丢失，但 hosts 劫持/系统 CA/mitmdump(8443) 均残留 → 业务域名解析 127.0.0.1:443 无监听 → 业务 API 全灭。**hosts 劫持与 reverse 隧道是"链路必需组件"而非"可独立存在的修改"**——隧道一断，劫持立刻变毒药。
- **本文档交叉验证结果**（2026-09-23）：B1（Dart 无视系统代理）、B2（SNI 路由）、方法论 #1（Flutter 只读系统 CA）、第六节在位状态——**全部与实测一致**，可信度高。airhub 重试风暴在 release 环境同样出现（c_debug_env=release 实测），但主业务（gw navigation 200）不受影响，仍属背景噪音。
- **新增误区记录**：网络异常时先怀疑了 SSL Pinning（Flutter App），注入 httptoolkit 反 Pinning 组合脚本——其 Flutter 模块特征扫描在此 App 的引擎上卡死 JS 线程（"Scripts completed" 标记永不出现），且**完全不需要**：系统 CA 已足够。判定 Dart 是否绕代理的快速手段：设备 `netstat -tnp | grep <pid>` 看有无直连外网 443（无 = 走 hosts 劫持路径）。
- **会话开场动作固化**（方法论 #8 的具体化）：`adb connect` → netstat 查 8443/9000 监听 → 重建 reverse 443+9000 → flows 增长探针。完整命令见 `frida/README.md` 的"会话断线恢复清单"。
