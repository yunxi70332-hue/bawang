# 模拟器抓包 · CA 隐匿方案

> 目标：在雷电模拟器里抓 `com.chagee.application.cn` 的流量，同时**不留下可被识别的中间人痕迹**。
> 约束：不改 APK/DEX/SO/签名；不采集真实令牌、手机号、Cookie、密钥或完整请求体。

---

## 0. 一句话结论

**你担心的事不是假设，它已经发生了。**

当前这台雷电实例（`emulator-5554` / Android 9 / x86_64）上：

| 项目 | 实测现状 | 判定 |
|---|---|---|
| 用户证书库内容 | `/data/misc/user/0/cacerts-added/87bc3517.0` | ❌ 非空 |
| 该证书身份 | `OU=HttpCanary, O=HttpCanary, CN=HttpCanary Root CA` | ❌ 字面暴露 |
| 已安装抓包工具 | `com.reqable.android`(Reqable)、`com.guoshi.httpcanary`(HttpCanary) | ❌ 包名即指纹 |
| 系统证书库 | 137 张，全部原厂，可疑项 0 | ✅ 干净 |
| 系统代理 | `null`（测试后已还原） | ✅ 干净 |

**"用户证书库非空"是 Android 上最响亮的中间人信号**——市售设备的这个目录通常是空的。而且那张证书的 CN 直接写着 `HttpCanary`，任何做字符串匹配的检测都会一眼命中。

好消息在下面第 2 节：**这个 App 本身没有任何检测证书库的代码**。坏消息是：客户端不检测 ≠ 服务端不风控。

---

## 1. 先回答你的原问题：LSPosed 插件能不能解决

**不能，而且这里根本不需要。**

| 方案 | 能否解决 | 原因 |
|---|---|---|
| LSPosed + TrustMeAlready / JustTrustMe | ❌ | 它们只 hook Java 层 `X509TrustManager` / `HostnameVerifier`。**本 App 的 Dart 层 TLS 走 BoringSSL，根本不经过 Java 栈**，hook 不到 |
| LSPosed 本身 | ❌ 装不上 | 当前实例只有 `/system/bin/su`，**没有 Magisk**。LSPosed 依赖 Magisk 的 Zygisk/Riru |
| 由此产生的反效果 | ⚠️ | 装了 LSPosed 反而**增加**新指纹（`/data/adb/`、注入痕迹），与你的目标相反 |

你真正需要绕的东西不是一个"防护"，而是**Flutter 读取 CA 的位置**：

- 实证：`libflutter.so` 里存在 `/system/etc/security/cacerts` 字符串，且**没有内置根证书 PEM**（`BEGIN CERTIFICATE` 命中 0 次）→ 该引擎从**系统证书目录**加载根证书，**不读用户证书库**。
- 推论：把 CA 装成"用户证书"对 **Dart 业务流量无效**（这是绝大多数人卡住的点）；必须进系统目录。
- 而用户证书库那一张 HttpCanary CA，**当前很可能只对 Java 侧 SDK 生效，对 Dart 层并不生效**——所以它既没帮上抓包的忙，又白白留下了指纹。这是双重损失。

---

## 2. 这个 App 有没有 CA 检测能力（静态证据）

| 检测面 | 结论 | 依据 |
|---|---|---|
| 枚举用户证书库 | ❌ 无 | 全量反编译源码中 `cacerts-added` **零命中** |
| 使用 `AndroidCAStore` | ⚠️ 仅 1 处，功能性的 | 仅在 `com.huawei.secure.android.common.ssl.SecureX509TrustManager`，用于**构建系统 CA 信任管理器**（验证用），非枚举检测 |
| 匹配抓包工具字样 | ❌ 无 | 4 个 dex 中 `mitmproxy/portswigger/charles/fiddler/burp` **全部 0 命中** |
| 反 Frida / Xposed | ❌ 无 | 原生库中 `Frida` 的唯一命中是 **`Friday` 的子串误报**（已核对字节上下文） |
| 反调试 / 完整性自校验 | ❌ 无 | 见 `docs/静态分析报告.md` |
| 证书锁定 | ❌ 应用级无 | Java 层无 `sha256/<pin>` 常量；Dart 层无 `badCertificateCallback` |

补充一个有价值的发现：`cn.jiguang.net.SSLTrustManager` 是**库级 pinning**——它只信任构造时传入的那一张 CA（`KeyStore.setEntry("ca_root", …)`）。这属于极光推送自己的通道，**与业务流量无关**，也**不会**因为你装了 mitmproxy CA 而改变。反过来说，这也是唯一一个"LSPosed 插件理论上能碰"的地方，但收益为零。

---

## 3. 推荐方案：临时挂载，用完即卸

设计原则：**不修改任何持久状态，所有动作可一条命令回滚。**

```text
① 把用户证书库整体挪走（备份到 /data/local/tmp，可还原）
        ↓
② 生成 subject 不暴露的 CA（不要叫 mitmproxy）
        ↓
③ 拷贝系统证书目录副本 → 加入自有 CA → bind-mount 覆盖原目录
        ↓
④ adb reverse + 系统代理，指向宿主 mitmproxy
        ↓
⑤ 抓完：umount + 还原用户库 + 清代理
```

为什么用 bind-mount 而不是直接写 `/system`：

- 实测 `/` 挂载为 `ro`（`/dev/root on / type ext4 (ro,…)`），`mount -o remount,rw /` **失败**（`'/dev/root' is read-only`）；雷电的系统盘默认不可写。
- 但 **`mount --bind` 可以覆盖只读文件系统里的目录**——已实测通过（覆盖后目录内容变为 1 条，`umount` 后恢复 137 条）。
- **SELinux 为 Permissive**，免去 `chcon` 上下文折腾。
- 全程**不需要重启**，不需要改镜像，`umount` 即还原。

---

## 4. 风险与残余风险（诚实说明）

| 措施 | 能挡住什么 | 挡不住什么 |
|---|---|---|
| 清空用户证书库 | 枚举 `cacerts-added` 的检测 | — |
| CA subject 改名 | 字符串匹配 `mitmproxy`/`HttpCanary` | 证书指纹、签发者链异常、自签根 |
| 卸载 Reqable/HttpCanary | `pm list packages` 的包名指纹 | 历史安装残留、其他工具 |
| 卸载后 umount | 事后取证 | 抓包期间的实时上报 |

**必须讲清楚的残余风险：**

1. **改名只解决字符串匹配，解决不了真指纹。** 任何持有可信根列表、或对证书链做结构校验的检测，都能认出"这张叶证书签发者不在公共根库里"。
2. **服务端风控不可见。** 客户端没有检测代码，不代表服务端不基于行为特征（请求时序、设备指纹一致性、安装包名单上报）打分。
3. **模拟器 + root 这个信号本身已经在上报。** 设备里存在极光/神策/易盾/移动认证 SDK，它们会采集并上报设备环境，**这与装不装 CA 无关**。所以"让 CA 不被发现"并不能让这台设备变得不可识别。
4. 建议：**只用自有账号，只看自己的流量**，抓包产物走 `scripts/sanitize_log.py` 脱敏，不落全量正文。

---

## 5. 实测证据汇总（本次会话）

| 验证项 | 方法 | 结果 |
|---|---|---|
| `/system` 可写性 | `mount -o remount,rw /` | ❌ 失败，`'/dev/root' is read-only` |
| bind-mount 可行性 | 覆盖 `/system/etc/security/cacerts` | ✅ 成功，`umount` 后恢复 137 条 |
| SELinux | `getenforce` | `Permissive` |
| iptables NAT | `iptables -t nat -L` | ✅ 可用 |
| root | `su -c id` | ✅ `/system/bin/su`，`ro.debuggable=1` |
| 系统代理对 **Java 侧**是否生效 | 浏览器 + 目标 App 经 `adb reverse` 打到探测端口 | ✅ **Java 侧 SDK 全部命中** |
| 系统代理对 **Dart 侧**是否生效 | 5 分钟持续操作 App + 探测端口 + `/proc/net/tcp` 内核连接表 | ❌ **不生效，Dart 直连绕开代理**（见第 7 节） |
| Android CA 哈希命名算法 | openssl `-subject_hash_old` vs 自实现 vs 设备实际文件名 | ✅ 三者一致（`87bc3517`） |

抓到的 Java 侧端点（说明代理链路是通的）：

```text
upload-tracking-sea.chagee.com:443   ×5   埋点
test-sentry.chagee.com:443                 Sentry
onekey2.cmpassport.com:443                 移动一键登录
log2.cmpassport.com:9443                   移动认证日志
status-ipv6.jpush.cn:443                   极光推送
da.dun.163.com:443                         网易易盾
```

**待办项已结案（见第 7 节）：Dart 业务流量不认 Android 系统代理。** 上面命中的全是 Java SDK 域名；5 分钟持续操作 App 期间探测器零 Dart 域名，而内核连接表显示 App 在多条 `:443` 上直连。初版方案"配好系统代理即可"的假设由此被推翻，传输层改用 WireGuard 模式。

---

## 6. 命令速查

```bash
# —— 审计设备当前暴露面（只读，随时可跑）
python scripts/audit_device_ca.py <系统库目录> <用户库证书文件>

# —— 判断某 App 到底走没走代理（决定性手段，不是看它"还能不能用"）
python scripts/dump_app_sockets.py --package com.chagee.application.cn --samples 10
python scripts/proxy_probe.py --port 8080 --seconds 180

# —— 生成一张 subject 不暴露的 CA（示例 DN 用泛化的企业 CA 命名）
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
  -keyout myca.key -out myca.pem \
  -subj "/C=CN/O=Enterprise Network Services/CN=Enterprise Root CA"

# —— 用完把 mitmproxy 的 CA 换成自己的
cat myca.pem myca.key > ~/.mitmproxy/mitmproxy-ca.pem   # confdir 里那个文件需 cert+key 合并
rm -f ~/.mitmproxy/mitmproxy-ca-cert.pem                # 触发重新生成

# —— 安/卸证书环境（不要再用 -SetProxy，理由见第 7 节）
pwsh -File scripts/setup_capture_env.ps1 -Action status
pwsh -File scripts/setup_capture_env.ps1 -Action apply -CaFile ./myca.pem
pwsh -File scripts/setup_capture_env.ps1 -Action revert

# —— 传输层：WireGuard 模式让 Dart 无法绕开
mitmdump --mode wireguard@51820
```

> 注意：本方案只处理"设备侧伪装"。宿主侧 mitmproxy 的 CA **当前 subject 是 `CN=mitmproxy, O=mitmproxy`**（落盘名 `c8750f0d.0`），
> 直接装进去等于自报家门——务必先替换成上面生成的 CA。

---

## 7. 传输层修正：系统代理拿不到 Dart 流量（重要）

第 5 节里那条待办已结案，结论是**否**。这条修正推翻了初版"配好系统代理即可"的假设，
是本次最有价值的发现。

### 三条独立证据

| # | 证据类型 | 内容 |
|---|---|---|
| 1 | **静态** | `libapp.so` 中含 `findProxyFromEnvironment`、`http_proxy`、`no_proxy` —— Dart `dart:io` 的代理解析走**环境变量**，不读 Android 的 `http_proxy` 全局设置。而 `libflutter.so` 中**没有** `proxyHost` / `ProxySelector` / `getDefaultProxy` 这类 Android 代理桥 |
| 2 | **动态** | 代理已设为 `127.0.0.1:8080` 时，读 `/proc/net/tcp{,6}` 看到 App(uid 10079) 有**多条直连 `28.0.0.225:443` 的 ESTABLISHED**，没有任何连向代理端口的连接 |
| 3 | **对照** | 5 分钟持续操作 App（切页、刷出新的 banner 图），探测端口**只收到 5 个 Java SDK 域名，Dart 业务域名 0 个**；同时页面数据正常渲染 |

第 3 条特别要记住：**不能用"App 还能不能用"判断代理是否生效。** 探测器对每个连接都回 `502` 并断开，
若 Dart 走代理，其请求必然失败、页面必然报错；页面正常渲染即说明 Dart 走的是直连。
判断代理是否生效，唯一可靠手段是看内核连接表的 remote 地址（用 `scripts/dump_app_sockets.py`）。

### 修正后的传输方案对比

| 机制 | 能拿到 Dart 流量 | Windows 宿主可行性 | 备注 |
|---|---|---|---|
| `settings put global http_proxy` | ❌ | — | Dart 不读；仅 Java 侧生效 |
| `mitmproxy --mode transparent` | ✅ | ❌ **不支持** | 官方仅 Linux/macOS（依赖 `SO_ORIGINAL_DST`） |
| `mitmproxy --mode wireguard@51820` | ✅ **全量流量** | ✅ 支持（12.2.3 已确认） | 设备装 WireGuard 客户端连入；不依赖 App 是否读代理 |
| 设备内 VPN 抓包 App（Reqable / HttpCanary） | ✅ | ✅ 已在设备上 | 本地 VPN 拦截，最省事；代价是包名本身成指纹 |

**推荐 `--mode wireguard`**：唯一"不依赖目标 App 配合、且 Windows 宿主可用"的机制。
证书仍由我们自己那张 CA 签发（第 3 节那套照用），设备侧只需一个 WireGuard 客户端，抓完一并卸载。

**同时修正一条之前的建议**：不要急着卸载 Reqable / HttpCanary。在本项目"系统代理对 Dart 无效"这个现实下，
它们反而是能立刻用起来的手段；要解决的是它们的 **CA** 和**包名暴露**，不是把工具删掉。两条路都留着，
按当次抓包的时长与敏感度选。

### 可选加固：让 Dart 也认环境变量

理论上可用 Android 的 `wrap.<pkg>` 机制给 App 进程注入 `http_proxy` 环境变量，
让 `findProxyFromEnvironment` 起作用。**不建议**：需要在设备上放 `wrap.sh` 并写 `setprop`，
而 `wrap.com.chagee.application.cn` 这个属性本身就是更容易被发现的指纹，得不偿失。
