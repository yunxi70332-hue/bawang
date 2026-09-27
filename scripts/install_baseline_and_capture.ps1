param(
  [Parameter(Mandatory = $true)]
  [string]$Serial,
  [string]$ApkPath = '',
  [string]$PackageName = 'com.chagee.application.cn',
  [switch]$CleanInstall,
  [switch]$CaptureStartup,
  [string]$OutDir = '.\capture'
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ApkPath)) {
  # Avoid embedding a non-ASCII filename in a Windows PowerShell source file:
  # resolve the sole APK under the workspace's apk directory instead.
  $apkDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\apk'))
  $candidates = @(Get-ChildItem -LiteralPath $apkDir -Filter '*.apk' -File)
  if ($candidates.Count -ne 1) {
    throw "Expected exactly one APK under $apkDir, found $($candidates.Count)."
  }
  $ApkPath = $candidates[0].FullName
}
$ApkPath = [IO.Path]::GetFullPath($ApkPath)

function Invoke-AdbText {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
  $output = & adb -s $Serial @Arguments 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "adb $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
  }
  return @($output | ForEach-Object { $_.ToString().TrimEnd() })
}

function Get-PackageFacts {
  $path = Invoke-AdbText shell pm path $PackageName
  $details = Invoke-AdbText shell dumpsys package $PackageName
  $filtered = @($details | Where-Object {
    $_ -match '^(\s*)(versionName|versionCode|codePath|primaryCpuAbi|installerPackageName)='
  })
  return [ordered]@{
    package_paths = @($path | Where-Object { $_ -like 'package:*' })
    package_fields = $filtered
  }
}

if (-not (Test-Path -LiteralPath $ApkPath -PathType Leaf)) {
  throw "APK not found: $ApkPath"
}

$state = (Invoke-AdbText get-state | Select-Object -First 1).Trim()
if ($state -ne 'device') {
  throw "ADB serial '$Serial' is not online (state='$state')."
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$recordPath = Join-Path $OutDir "baseline-$stamp.json"
$facts = [ordered]@{
  captured_at = (Get-Date).ToString('o')
  serial = $Serial
  device = [ordered]@{
    model = (Invoke-AdbText shell getprop ro.product.model | Select-Object -First 1)
    abi = (Invoke-AdbText shell getprop ro.product.cpu.abi | Select-Object -First 1)
    android_release = (Invoke-AdbText shell getprop ro.build.version.release | Select-Object -First 1)
  }
  package_before = Get-PackageFacts
  clean_install = [bool]$CleanInstall
}

if ($CleanInstall) {
  $uninstall = & adb -s $Serial uninstall $PackageName 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "adb uninstall failed: $($uninstall -join [Environment]::NewLine)"
  }
}

$install = & adb -s $Serial install $ApkPath 2>&1
if ($LASTEXITCODE -ne 0 -or -not ($install -match '^Success$')) {
  throw "adb install failed: $($install -join [Environment]::NewLine)"
}

$facts.package_after = Get-PackageFacts
$facts | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $recordPath -Encoding utf8
Write-Output "Baseline record: $recordPath"

if ($CaptureStartup) {
  & (Join-Path $PSScriptRoot 'capture_runtime.ps1') -PackageName $PackageName -Serial $Serial -OutDir $OutDir
  if ($LASTEXITCODE -ne 0) {
    throw "runtime capture failed with exit code $LASTEXITCODE"
  }
}
