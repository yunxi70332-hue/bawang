# 云真机脱壳执行手册（平台恢复后按序执行）

状态：平台网关（pc.tjphone.cloud → 183.253.116.107:80）约 20:41 起不可达，串流断开（错误码 1001）。
已就绪：dex 上传器（tools/uploader/classes.dex，3.5KB）、r8 工具链、root 通道已验证（su -c id → uid=0）。
设备侧已有数据：/sdcard/maps2.txt（PID=5473 + libapp.so/libflutter.so 映射行 + maps 总行数）。

## 恢复后执行序列

### 1. 重连与验证
- 浏览器控制窗口：点掉「连接断开 1001」弹窗的「确定」，等「正在连接设备...」变为已连接（FPS 恢复）。
- curl 验证 API：GET /api/info/brand 返回 200。

### 2. 部署 dex 上传器（一次性）
```
1) curl 上传 classes.dex → POST /api/file/upload (X-Token)          # 本机
2) 推送到设备：H5 推送（或 API /devices/push_file）                 # 落 /sdcard/classes.dex
3) 设备验证：app_process 冒烟测试（跑一次上传 maps2.txt 本身）
```

### 3. 设备端执行模板（自定义命令）
```sh
# 上传任意文件回云盘（shell 身份即可，INTERNET 权限 shell 有）
CLASSPATH=/sdcard/classes.dex app_process / Uploader /sdcard/<文件> <X-TOKEN>
# root 文件（如 /proc/<pid>/maps）用：
su -c "CLASSPATH=/sdcard/classes.dex app_process / Uploader /sdcard/<文件> <X-TOKEN>"
```

### 4. 5c 内存 dump libapp.so
```sh
am start -n com.chagee.application.cn/com.chagee.application.cn.MainActivity
sleep 5
PID=$(pidof com.chagee.application.cn)
su -c "grep libapp.so /proc/$PID/maps" > /sdcard/dump_plan.txt   # 地址段+偏移
# 按 maps 的 file-backed r--/r-x 段，用 dd 从 /proc/$PID/mem 抠出（注意 arm64 libapp.so 无重定位写时拷贝，
# file 段内容应与 APK 内原件一致 → 直接对比 /proc/PID/map_files 或 dd 结果 sha256）
su -c "sha256sum \$(su -c 'ls /proc/$PID/map_files/' 2>/dev/null | head -1)" # 不通，改为逐段 dd
# 实操：对每个 libapp.so 的 file-backed 段：
#   su -c "dd if=/proc/$PID/mem bs=4096 skip=\$((start/4096)) count=\$((size/4096)) of=/sdcard/libapp_chunk_N.bin"
# 拼接后 sha256sum，对比 8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7
```
简化路径（优先试）：`su -c "cat /proc/$PID/map_files/<start>-<end>"` 直接读 file-backed 段的原始文件句柄。

### 5. exfil 与校验
- Uploader 上传 dump 产物 → 本机 curl 下载（file_download_link）→ 本地 sha256 对比。
- maps2.txt 同样 exfil 后归档 docs/。

### 6. 5d frida（时间允许）
- frida-server-16.x-android-arm64 上传云盘 → 推送 → /data/local/tmp/ 改名 chmod
- su 启动；本机 frida 无法直连（无 adb）——改为：blutter_frida.js 通过 frida CLI 跑在……
  本机没有到设备的 frida 通道（adb 断）。变通：在设备上用 frida 的 -l 模式把结果写文件，
  或放弃 frida，纯 dump + 静态 RVA 已足够验证锚点。
  （结论：无 adb 时 frida 远控不可行，锚点验证以 maps+dump+静态 Blutter 交叉为准）

### 7. 收尾
- rm /sdcard/audit*.txt /sdcard/pkgs*.txt /sdcard/rs.txt /sdcard/find1.txt /sdcard/maps*.txt /sdcard/root*.txt /sdcard/classes.dex
- 一键新机（用户确认后）；本地更新 reverse_learning_log.md
