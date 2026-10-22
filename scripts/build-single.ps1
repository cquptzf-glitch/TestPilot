# 单文件绿色版构建：把 onefile 引擎（Code.exe）**嵌入**壳二进制，产出免安装单 exe。
#
# 与 build-shell.ps1 的关系：彼此独立、可并存——
#   build-shell.ps1  -> NSIS 安装包（onedir 引擎随 resources 分发，正式形态）
#   build-single.ps1 -> dist/TestPilot.exe（引擎以 include_bytes! 嵌入，首启释放到
#                       数据根 engine/ 子目录，双击即用、免安装）
#
# 用法（仓库根）：
#   powershell -ExecutionPolicy Bypass -File scripts/build-single.ps1               # 全量
#   powershell -ExecutionPolicy Bypass -File scripts/build-single.ps1 -SkipEngine   # 复用已有 dist/Code.exe
#   powershell -ExecutionPolicy Bypass -File scripts/build-single.ps1 -SkipSmoke    # 跳过冒烟
#
# 已知代价（见 docs/dev-log 第十三轮）：
#   - 产物约 130 MB（壳 ~15 MB + 嵌入引擎 ~72 MB + 前端）；
#   - 首次启动 = 释放 72 MB + onefile 解包，比安装版慢数秒（之后命中缓存）；
#   - 自解压形态可能被杀软启发式误报，正式分发请对产物做代码签名。
param(
    [switch]$SkipEngine,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$desktop = Join-Path $root 'testpilot\app\desktop'

function Assert-LastExit([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What 失败（exit=$LASTEXITCODE）" }
}
function Step([string]$Title) {
    Write-Host ''
    Write-Host "==> $Title"
}

# ---------- 1. 引擎单文件 ----------
if (-not $SkipEngine) {
    Step '构建引擎单文件（build-sidecar.ps1 -Onefile -Name Code）'
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build-sidecar.ps1') `
        -Onefile -Name Code
    Assert-LastExit 'build-sidecar（onefile/Code）'
}
$engine = Join-Path $root 'dist\Code.exe'
if (-not (Test-Path $engine)) {
    throw "未找到引擎单文件: $engine（先跑 build-sidecar.ps1 -Onefile -Name Code）"
}

# ---------- 2. 嵌入式壳（不出安装包，只要裸 exe；前端本就内嵌） ----------
Step '构建嵌入式壳（tauri build --no-bundle）'
Set-Location $desktop
$env:TESTPILOT_EMBED_ENGINE = $engine    # build.rs 读取：定义 cfg embedded_engine
& npm run shell:build -- --no-bundle
Assert-LastExit 'tauri build --no-bundle'
Remove-Item Env:TESTPILOT_EMBED_ENGINE -ErrorAction SilentlyContinue

# ---------- 3. 归档 ----------
Set-Location $root
# 归档路径跟着 CARGO_TARGET_DIR 走（本机构建目录被重定向，见 build-shell.ps1 注释）
$targetDir = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR }
             else { Join-Path $desktop 'src-tauri\target' }
if (-not [System.IO.Path]::IsPathRooted($targetDir)) {
    $targetDir = Join-Path $root $targetDir
}
$built = Join-Path $targetDir 'release\testpilot-shell.exe'
if (-not (Test-Path $built)) { throw "未找到壳产物: $built" }

$out = Join-Path $root 'dist\TestPilot.exe'
Copy-Item -Force $built $out
Write-Host ''
Write-Host ("完成：{0}（{1:N1} MB，含嵌入引擎）" -f $out, ((Get-Item $out).Length / 1MB))

# ---------- 4. 冒烟（首启要释放 72MB 引擎 + onefile 解包，等待窗口放大） ----------
if (-not $SkipSmoke) {
    Step '端到端冒烟（单文件版，-SidecarName Code）'
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'smoke-shell.ps1') `
        -Exe $out -SidecarName Code -WaitSeconds 60
    Assert-LastExit 'smoke-shell（单文件版）'
}
