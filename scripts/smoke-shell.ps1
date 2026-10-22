# 端到端冒烟：验证「壳能拉起 sidecar 并连上 RPC」（M4-1 实机联调的最小可验证集）。
#
# 为什么需要它：安装包构建成功 ≠ 应用能跑。此前真实踩到过的坑包括
#   * resources 单层 glob 导致 _internal/ 丢失（装完拉不起引擎）；
#   * 默认配置里的 default.yaml 未随包（引擎读配置即 FileNotFoundError）；
#   * DLP 让非白名单进程读到密文（cv2 引导文件 -> 视觉能力静默消失）。
# 三者都不会让构建失败，只有真的把壳跑起来才能发现。
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts/smoke-shell.ps1 [-Exe <path>]
#       [-SidecarName <name>]   # 单文件绿色版释放出的引擎叫 Code.exe，传 -SidecarName Code
param(
    [string]$Exe = '',
    [int]$WaitSeconds = 25,
    [string]$SidecarName = 'testpilot-sidecar'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not $Exe) {
    $targetDir = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR }
                 else { Join-Path $root 'testpilot\app\desktop\src-tauri\target' }
    if (-not [System.IO.Path]::IsPathRooted($targetDir)) {
        $targetDir = Join-Path $root $targetDir
    }
    $Exe = Join-Path $targetDir 'release\testpilot-shell.exe'
}
if (-not (Test-Path $Exe)) { throw "未找到壳可执行文件: $Exe（先跑 build-shell.ps1）" }

# 用独立数据根，避免污染开发态数据；跑完可整体丢弃
$home_ = Join-Path $root '.cache\smoke-home'
New-Item -ItemType Directory -Force -Path $home_ | Out-Null
$env:TESTPILOT_HOME = $home_

Write-Host "==> 启动壳: $Exe"
$shell = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds $WaitSeconds

$ok = $true
function Report([bool]$cond, [string]$what) {
    if ($cond) { Write-Host "  [OK]   $what" }
    else { Write-Host "  [FAIL] $what"; $script:ok = $false }
}

Write-Host ''
Write-Host '==> 检查'
Report (-not $shell.HasExited) '壳进程存活'
$sidecar = Get-Process -Name $SidecarName -ErrorAction SilentlyContinue
Report ([bool]$sidecar) "侧车进程已拉起（$SidecarName）"
if ($sidecar) { Write-Host "       sidecar PID=$($sidecar.Id)" }

# RPC 端口（配置默认 9301）：壳与引擎之间的 JSON-RPC over WebSocket
$port = 9301
$listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port $port `
    -InformationLevel Quiet -WarningAction SilentlyContinue) 2>$null
if ($null -eq $listening) {
    $listening = [bool](Get-NetTCPConnection -LocalPort $port -State Listen `
        -ErrorAction SilentlyContinue)
}
Report ([bool]$listening) "RPC 端口 $port 处于监听"

Report (Test-Path (Join-Path $home_ 'data\testpilot.db')) '数据根已建库（TESTPILOT_HOME 生效）'
Report (Test-Path (Join-Path $home_ 'logs')) '数据根已建日志目录'

Write-Host ''
Write-Host '==> 清理'
if ($sidecar) { Stop-Process -Id $sidecar.Id -Force -ErrorAction SilentlyContinue }
if (-not $shell.HasExited) { Stop-Process -Id $shell.Id -Force -ErrorAction SilentlyContinue }
Write-Host '     已停止壳与侧车进程'

Write-Host ''
if ($ok) { Write-Host '冒烟通过：壳可拉起引擎并建库'; exit 0 }
else { Write-Host '冒烟失败：见上方 [FAIL] 项'; exit 1 }
