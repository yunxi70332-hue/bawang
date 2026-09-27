#!/system/bin/sh
# 云真机部署 CA 到系统证书库（Android 13, KSU root, 无 adb 环境）
# 原理：/system 只读，但 root 可以 mount --bind 覆盖 /system/etc/security/cacerts 目录
# （与雷电模拟器上验证过的方案同构，见 docs/抓包与CA隐匿方案.md）
# 用法: su -c "sh /sdcard/deploy_ca.sh <CA PEM 路径>"
# 注意：bind-mount 在全局 mount namespace 生效；Flutter(Dart) 每次新建 TLS 连接都会
#       重新读 /system/etc/security/cacerts，因此挂载后新发起的请求即信任新 CA。

set -e
CA_SRC="$1"
[ -f "$CA_SRC" ] || { echo "ERR: CA file not found: $CA_SRC"; exit 1; }

SYS_CA=/system/etc/security/cacerts
STAGE=/data/local/tmp/cacerts_stage

# 1) 暂存目录：拷贝现有系统证书（Android 13 上该目录是链接到 apex 的挂载点，
#    直接 cp -a 源路径内容）
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$SYS_CA"/*.0 "$STAGE"/ 2>/dev/null || true
echo "copied existing certs: $(ls "$STAGE" | wc -l)"

# 2) 计算 OpenSSL subject_hash_old 命名。toybox 没有 openssl，改用固定命名规则：
#    Reqable/HttpCanary 的 CA 通常以 <hash>.0 落盘。若设备无 openssl，
#    直接以 'reqable.0' 命名也会被 conscrypt 枚举（文件名只需 <something>.0，
#    内容为 PEM 即可——Android 的 TrustedCertificateStore 按文件名索引但不对内容做 hash 校验）。
CA_NAME=reqable_ca.0
cp "$CA_SRC" "$STAGE/$CA_NAME"

# 3) bind-mount 覆盖（在当前=全局 namespace）
mount --bind "$STAGE" "$SYS_CA" 2>/dev/null || {
  # Android 13 上 /system/etc/security/cacerts 可能是 bind 到 apex 的只读挂载，
  # 直接覆盖失败时用 tmpfs + 全量拷贝再 bind 的两段式
  MNT=/data/local/tmp/cacerts_mnt
  mkdir -p "$MNT"
  mount -t tmpfs tmpfs "$MNT" 2>/dev/null || true
  cp "$STAGE"/* "$MNT"/ 2>/dev/null || true
  umount "$SYS_CA" 2>/dev/null || true
  mount --bind "$MNT" "$SYS_CA" || { echo "ERR: bind mount failed"; exit 2; }
}

echo "mounted certs: $(ls $SYS_CA | wc -l)"
echo "OK: CA deployed as $SYS_CA/$CA_NAME"

# 验证：应用层用 openssl 不可用，改由抓包时观察 TLS 握手是否信任。
# 回滚: umount /system/etc/security/cacerts
