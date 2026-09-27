param(
  [string]$PackageName = 'com.chagee.application.cn',
  [string]$Serial = 'emulator-5554',
  [string]$OutDir = '.\capture'
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$raw = Join-Path $OutDir "runtime-$stamp.raw.log"
$clean = Join-Path $OutDir "runtime-$stamp.sanitized.log"
$state = (adb -s $Serial get-state).Trim()
if ($state -ne 'device') {
  throw "ADB serial '$Serial' is not online (state='$state')."
}

try {
  adb -s $Serial logcat -c
  adb -s $Serial shell am force-stop $PackageName
  adb -s $Serial shell monkey -p $PackageName 1 | Out-Null
  Start-Sleep -Seconds 8
  adb -s $Serial logcat -d -v threadtime |
    Select-String -Pattern 'flutter|chagee|QuickLogin|DioException|MainActivity|libflutter|libapp' |
    ForEach-Object { $_.Line } |
    Set-Content -Encoding utf8 $raw
  python .\scripts\sanitize_log.py $raw $clean
  if ($LASTEXITCODE -ne 0) {
    throw "sanitize_log.py failed with exit code $LASTEXITCODE"
  }
  Write-Output "Sanitized runtime capture: $clean"
}
finally {
  if (Test-Path -LiteralPath $raw) {
    Remove-Item -LiteralPath $raw -Force
  }
}
