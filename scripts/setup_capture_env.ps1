<#
.SYNOPSIS
  抓包环境管理器（雷电模拟器 / Android 9）—— 临时挂载自有 CA，用完一条命令还原。

.DESCRIPTION
  做三件事，全部可逆，不写系统镜像、不重启：
    1. 清空用户证书库（先备份到设备 /data/local/tmp），消除最响亮的中间人指纹；
    2. 把自己的 CA 以 PEM 形式塞进「系统证书目录」的 bind-mount 副本，
       让 Flutter/Dart 的 BoringSSL 能信任它（Dart 只读系统目录，不读用户库）；
    3. 可选：配置系统代理 + adb reverse，把流量导给宿主 mitmproxy。

  注意：Android 系统证书库里的文件是 PEM；用户库接受 DER。本脚本按 PEM 注入。
  设备端回显一律用 ASCII —— adb shell 的输出不走 UTF-8 解码，中文会变乱码。

.PARAMETER Action
  status = 只读审计当前暴露面；apply = 部署；revert = 还原全部改动。

.EXAMPLE
  pwsh -File scripts/setup_capture_env.ps1 -Action status
  pwsh -File scripts/setup_capture_env.ps1 -Action apply -CaFile .\myca.pem -SetProxy
  pwsh -File scripts/setup_capture_env.ps1 -Action revert
#>
param(
  [ValidateSet('status', 'apply', 'revert')]
  [string]$Action = 'status',
  [string]$Serial = 'emulator-5554',
  [string]$CaFile = '',
  [int]$ProxyPort = 8080,
  [switch]$SetProxy,
  [string]$Adb = 'C:\Users\Administrator\Tools\platform-tools\platform-tools\adb.exe',
  [string]$Python = 'D:\txAI\WorkBuddy-data\binaries\python\envs\default\Scripts\python.exe'
)

$ErrorActionPreference = 'Stop'

$STAGE = '/data/local/tmp/capenv'
$SYSSTORE = '/system/etc/security/cacerts'
$USERSTORE = '/data/misc/user/0/cacerts-added'
$RUNNER = '/data/local/tmp/capenv_run.sh'

# PS 5.1 在 ErrorActionPreference=Stop 下会把原生命令的 stderr 当成终止错误，
# adb 的 "* daemon not running" 提示就会炸掉整个脚本。所有 adb 调用统一走这里。
function Invoke-AdbCli {
  param([Parameter(ValueFromRemainingArguments = $true)][object[]]$CliArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $result = & $Adb @CliArgs 2>&1
  } finally {
    $ErrorActionPreference = $prev
  }
  return ($result | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
}

function Invoke-Adb {
  param([Parameter(ValueFromRemainingArguments = $true)][object[]]$CliArgs)
  return Invoke-AdbCli @('-s', $Serial) @CliArgs
}

# 取回文本结果。注意不要写成 `Invoke-Adb @(...) -join ' '` ——
# 那样 -join 会被当成 adb 的额外参数塞进命令里，导致静默失败。
function Invoke-AdbText {
  param([Parameter(ValueFromRemainingArguments = $true)][object[]]$CliArgs)
  $r = Invoke-Adb @CliArgs
  if ($null -eq $r) { return '' }
  return (($r | ForEach-Object { [string]$_ }) -join "`n")
}

function Assert-Adb {
  if (-not (Test-Path -LiteralPath $Adb)) { throw "找不到 adb: $Adb" }
  Invoke-AdbCli @('start-server') | Out-Null
  Invoke-AdbCli @('connect', '127.0.0.1:5555') | Out-Null
  $state = (Invoke-AdbText @('get-state')).Trim()
  if ($state -ne 'device') {
    throw "设备 $Serial 不在线（state='$state'）。请确认雷电模拟器已启动、adb 调试已打开。"
  }
}

function Assert-Root {
  $id = (Invoke-AdbText @('shell', "su -c 'id'")).Trim()
  if ($id -notmatch 'uid=0') { throw "设备未取得 root：$id" }
}

function Invoke-Guest {
  param([string]$Body, [switch]$AsRoot)
  $body2 = $Body -replace "`r`n", "`n"
  $tmp = Join-Path $env:TEMP 'capenv_guest.sh'
  [System.IO.File]::WriteAllText($tmp, $body2, (New-Object System.Text.UTF8Encoding($false)))
  Invoke-Adb @('push', $tmp, $RUNNER) | Out-Null
  if ($AsRoot) {
    return Invoke-Adb @('shell', "su -c 'sh $RUNNER'")
  }
  return Invoke-Adb @('shell', "sh $RUNNER")
}

function Get-CaHashName {
  param([string]$Pem)

  # 优先 openssl：参数是简单的单词列表，不会踩 PowerShell 原生参数引号问题
  $cands = @('openssl')
  $glob = @(
    'C:\Program Files\Git\usr\bin\openssl.exe',
    'D:\txAI\WorkBuddy-data\binaries\PortableGit\*\usr\bin\openssl.exe'
  )
  $cands += (Get-ChildItem $glob -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
  foreach ($c in $cands) {
    try {
      $h = (& $c x509 -in $Pem -subject_hash_old -noout 2>$null | Out-String).Trim()
      if ($h -match '^[0-9a-fA-F]{8}$') { return "$h.0" }
    } catch { }
  }

  # 兜底：把代码落到临时文件再执行，避免把含引号的脚本当参数传
  if (Test-Path -LiteralPath $Python) {
    $py = Join-Path $env:TEMP 'capenv_ca_hash.py'
    $code = @'
import hashlib, sys
from cryptography import x509
c = x509.load_pem_x509_certificate(open(sys.argv[1], "rb").read())
print("%08x.0" % int.from_bytes(hashlib.md5(c.subject.public_bytes()).digest()[:4], "little"))
'@
    [System.IO.File]::WriteAllText($py, $code, (New-Object System.Text.UTF8Encoding($false)))
    $h = (& $Python $py $Pem 2>$null | Out-String).Trim()
    if ($h) { return "$h" }
  }
  throw "需要 openssl 或带 cryptography 的 python 来计算 Android 证书文件名（subject_hash_old）。"
}

# ---------------------------------------------------------------- status
if ($Action -eq 'status') {
  Assert-Adb
  Write-Output "== 设备 =="
  Invoke-Guest @'
echo "  Android   : $(getprop ro.build.version.release) (sdk $(getprop ro.build.version.sdk))"
echo "  ABI       : $(getprop ro.product.cpu.abi)"
echo "  debuggable: $(getprop ro.debuggable)"
echo "  SELinux   : $(getenforce)"
'@
  Write-Output "== 系统证书库（应全为原厂 PEM）=="
  Invoke-Guest @"
echo "  count = `$(ls $SYSSTORE | wc -l)"
echo "  non PEM (suspicious):"
for f in $SYSSTORE/*; do head -n 1 "`$f" | grep -q 'BEGIN CERTIFICATE' || echo "    [!] `$f"; done
"@
  Write-Output "== 用户证书库（市售设备应为空）=="
  Invoke-Guest "su -c 'ls -l $USERSTORE 2>/dev/null || echo NOT_FOUND'"
  Write-Output "== 是否有 CAP 覆盖层在挂载 =="
  Invoke-Guest "su -c 'mount | grep security/cacerts || echo NOT_MOUNTED'"
  Write-Output "== 抓包工具 / 代理指纹 =="
  Invoke-Guest @'
echo "  capture apps:"
pm list packages | grep -i -E 'reqable|httpcanary|charles|proxypin|fiddler|burp|mitm' | sed 's/^/    /'
# Android 的代理状态跨 5 个 key，必须全看，只看 http_proxy 会漏判。
for k in http_proxy global_http_proxy_host global_http_proxy_port global_http_proxy_exclusion_list global_proxy_pac_url; do
  echo "  $k = $(settings get global $k)"
done
'@
  Write-Output ""
  Write-Output "判定：用户证书库非空、或上面列出任何包名，都是可被识别的中间人指纹。"
  exit 0
}

# ---------------------------------------------------------------- apply
if ($Action -eq 'apply') {
  Assert-Adb
  Assert-Root

  if (-not $CaFile) { throw "apply 需要 -CaFile（你的 CA PEM 文件）。" }
  if (-not (Test-Path -LiteralPath $CaFile)) { throw "找不到 CA 文件: $CaFile" }
  $caFull = (Resolve-Path -LiteralPath $CaFile).Path

  $pemHead = Get-Content -LiteralPath $caFull -TotalCount 1
  if ($pemHead -notmatch 'BEGIN CERTIFICATE') {
    throw "$CaFile 不是 PEM 证书。请只提取证书部分：openssl x509 -in ca.pem -out cert.pem"
  }

  $caName = Get-CaHashName -Pem $caFull
  Write-Output "[1/4] CA 落盘名（Android subject_hash_old）: $caName"
  Invoke-Adb @('shell', "mkdir -p $STAGE/rw $STAGE/userbackup") | Out-Null
  Invoke-Adb @('push', $caFull, "$STAGE/ca.pem") | Out-Null

  Write-Output "[2/4] 备份并清空用户证书库"
  Invoke-Guest -AsRoot @"
USERSTORE=$USERSTORE
STAGE=$STAGE
if [ -d "`$USERSTORE" ]; then
  n=`$(ls "`$USERSTORE" 2>/dev/null | wc -l)
  if [ "`$n" -gt 0 ]; then
    cp -f "`$USERSTORE"/* "`$STAGE/userbackup/" 2>/dev/null || true
    rm -f "`$USERSTORE"/* 2>/dev/null || true
  fi
  echo "  backed up and cleared, was `$n entry(ies)"
else
  echo "  user store absent, skipped"
fi
"@

  Write-Output "[3/4] 构建系统证书库副本并注入自有 CA"
  Invoke-Guest -AsRoot @"
STAGE=$STAGE
SYSSTORE=$SYSSTORE
rm -rf "`$STAGE/rw"
mkdir -p "`$STAGE/rw"
cp "`$SYSSTORE"/* "`$STAGE/rw/" 2>/dev/null || true
chmod 644 "`$STAGE/rw"/* 2>/dev/null || true
cp -f "`$STAGE/ca.pem" "`$STAGE/rw/$caName"
chmod 644 "`$STAGE/rw/$caName"
chcon u:object_r:system_file:s0 "`$STAGE/rw/$caName" 2>/dev/null || true
echo "  overlay entries = `$(ls `$STAGE/rw | wc -l)"
"@

  Write-Output "[4/4] bind-mount 覆盖系统证书目录"
  Invoke-Guest -AsRoot @"
STAGE=$STAGE
SYSSTORE=$SYSSTORE
mount | grep -q "`$SYSSTORE" && umount "`$SYSSTORE" 2>/dev/null || true
mount --bind "`$STAGE/rw" "`$SYSSTORE"
echo "  visible entries = `$(ls `$SYSSTORE | wc -l)"
echo "  mount = `$(mount | grep security/cacerts)"
"@

  if ($SetProxy) {
    Write-Output "[+] 配置系统代理 -> 宿主 mitmproxy"
    Invoke-Adb @('reverse', "tcp:$ProxyPort", "tcp:$ProxyPort") | Out-Null
    # 注意：Android 代理状态跨多个 key。只写 http_proxy 会留下
    # global_http_proxy_host/port 残留，revert 时漏删就让 App 连不上网（已踩过）。
    Invoke-Guest -AsRoot @"
settings put global http_proxy 127.0.0.1:$ProxyPort
settings put global global_http_proxy_host 127.0.0.1
settings put global global_http_proxy_port $ProxyPort
settings delete global global_http_proxy_exclusion_list >/dev/null 2>&1 || true
settings delete global global_proxy_pac_url >/dev/null 2>&1 || true
for k in http_proxy global_http_proxy_host global_http_proxy_port; do echo "  `$k = `$(settings get global `$k)"; done
"@
  }

  Write-Output ""
  Write-Output "完成。抓包结束后必须执行："
  Write-Output "  pwsh -File scripts/setup_capture_env.ps1 -Action revert"
  exit 0
}

# ---------------------------------------------------------------- revert
if ($Action -eq 'revert') {
  Assert-Adb
  Write-Output "[1/3] 卸载证书覆盖层"
  Invoke-Guest "su -c 'umount $SYSSTORE 2>/dev/null; if mount | grep -q security/cacerts; then echo STILL_MOUNTED; else echo UMOUNTED; fi'"

  Write-Output "[2/3] 还原用户证书库"
  Invoke-Guest -AsRoot @"
USERSTORE=$USERSTORE
STAGE=$STAGE
mkdir -p "`$USERSTORE"
if [ -d "`$STAGE/userbackup" ]; then
  cp -f "`$STAGE/userbackup"/* "`$USERSTORE/" 2>/dev/null || true
  chown system:system "`$USERSTORE"/*.0 2>/dev/null || true
  chmod 644 "`$USERSTORE"/*.0 2>/dev/null || true
fi
echo "  user store entries = `$(ls "`$USERSTORE" 2>/dev/null | wc -l)"
"@

  Write-Output "[3/3] 清理代理与端口转发"
  # 五个 key 必须全清。漏掉 global_http_proxy_host/port 是最隐蔽的坑：
  # http_proxy 显示 null，但 App 仍被指向 127.0.0.1:8080，表现为"不走流量"。
  Invoke-Guest -AsRoot @'
for k in http_proxy global_http_proxy_host global_http_proxy_port global_http_proxy_exclusion_list global_proxy_pac_url; do
  settings delete global "$k" >/dev/null 2>&1 || true
done
for k in http_proxy global_http_proxy_host global_http_proxy_port global_http_proxy_exclusion_list global_proxy_pac_url; do
  echo "  $k = $(settings get global $k)"
done
'@
  Invoke-Adb @('reverse', '--remove-all') | Out-Null

  Write-Output ""
  Write-Output "已还原。建议再跑一次 status 确认系统证书库条数为 137。"
  exit 0
}
