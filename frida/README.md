# 云手机抓包链路（霸王茶姬）— 2026-09-23 01:15 修正版

## 真实架构（双通道，勿混淆）

```
【业务流量 = Dart 层，无视系统代理】 ← 知识库 B1 结论，今日实测复现
  App → /system/etc/hosts 劫持 4 域名 → 127.0.0.1:443
      → adb reverse tcp:443 tcp:8443
      → PC mitmdump (reverse 模式 + sni_route_addon.py + lazy, 监听 8443, PID 见 netstat)
      → SNI 路由到真实上游，mitmproxy CA (c8750f0d.0) 解密
      → 落盘 capture/chagee_dart_newdevice_20260923.flows（新设备纪元文件）（python -c "from mitmproxy import io" 可读）

【原生 SDK 流量 = Java 层，走系统代理】
  App → 全局代理 127.0.0.1:9000 → adb reverse tcp:9000
      → PC Reqable (0.0.0.0:9000)，Reqable CA (b0ef9388.0) 解密
      → 神策/Sentry/极光等遥测流量（业务接口不会出现在这里！）
```

被劫持的 4 域名：gw.chagee.com / gj-api.bwcj.com / api-cn.chagee.com / h5-sea.chagee.com

## ⚠️ 会话断线恢复清单（按序执行）

每次 adb 断线重连后，reverse 隧道全部丢失，App 会报"网络异常"：

```bash
export MSYS_NO_PATHCONV=1
adb connect 220.190.18.21:43075                 # IP 可能变化，端口 56915 不变
# 1. 检查 PC mitmdump 还活着吗（监听 8443）：
netstat -ano | findstr :8443                   # 没有 → 见下方"重启 mitmdump"
# 2. 重建双隧道：
adb -s 220.190.18.21:43075 reverse tcp:443 tcp:8443     # 业务（关键！）
adb -s 220.190.18.21:43075 reverse tcp:9000 tcp:9000    # 原生 SDK → Reqable
adb -s 220.190.18.21:43075 forward tcp:27042 tcp:27042   # frida（要用时）
# 3. 验收（KB B2 原则：用独占端点探针，别只看日志）：
adb -s 220.190.18.21:43075 shell "curl -x 127.0.0.1:9000 -s -o /dev/null -w '%{http_code}' https://www.baidu.com"  # 应 200
python -c "from mitmproxy import io; fs=[f for f in io.FlowReader(open(r'E:/霸王茶姬/capture/chagee_dart_resession.flows','rb')).stream()]; print('flows:',len(fs),'last:',fs[-1].request.host)"  # 应在增长
```

重启 mitmdump（若 8443 无人监听；注意用全路径，工作目录必须是项目根）：
```bash
cd E:/霸王茶姬 && "C:/Users/Administrator/AppData/Roaming/Python/Python314/Scripts/mitmdump.exe" \
  --mode reverse:https://gw.chagee.com/ --listen-port 8443 \
  --set connection_strategy=lazy -s scripts/sni_route_addon.py \
  -w capture/chagee_dart_newdevice_20260923.flows
```

## 今日教训（2026-09-23 网络异常事故）

- **根因**：昨天会话断开 → reverse 443 丢失 → hosts 劫持残留 → 业务域名解析到 127.0.0.1 无监听 → 全部业务 API 失败。
- **误诊弯路**：先怀疑 SSL Pinning（App 是 Flutter），注入 httptoolkit 反 Pinning 脚本无效且其 Flutter 模块扫描卡死 JS 线程。实际上 KB 方法论 #1 早写了"Flutter 只读系统 CA"——**根本不需要反 Pinning**，CA 在系统库即可解密。
- **判定依据**（可复用）：抓包里只有神策/Sentry 而无业务域名 = Dart 绕过代理；设备 netstat 无直连外网但有大量 127.0.0.1:443 尝试 = hosts 劫持在起作用。
- frida 注入已停用（无需要，还引入不稳定性）；frida-server 16.7.19 保留在 /data/local/tmp/frida-server，做 libapp 内存分析时再用。

## 组件版本

- frida 双端 16.7.19（server: /data/local/tmp/frida-server；PC: frida 16.7.19 + frida-tools 14.10.4，命令在 %APPDATA%\Python\Python314\Scripts\）
- Reqable 桌面版（代理 0.0.0.0:9000 + MCP mcp-server.exe，工具需重启 ZCode 加载）
- mitmproxy（mitmdump 全路径见上，CA c8750f0d.0 已在设备系统库）
- App: com.chagee.application.cn v638, c_debug_env=release（生产+真实登录态）
- airhub queryList 3次/秒重试风暴 = 已知背景噪音（KB 2.3），非故障
