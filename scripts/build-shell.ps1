# 桌面壳一体化构建：sidecar -> 前端 -> Tauri bundle -> 顶层 dist/。
#
# 用法（仓库根）：
#   powershell -ExecutionPolicy Bypass -File scripts/build-shell.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/build-shell.ps1 -SkipSidecar    # 复用已有 sidecar
#   powershell -ExecutionPolicy Bypass -File scripts/build-shell.ps1 -Dev            # 只起开发态
#   powershell -ExecutionPolicy Bypass -File scripts/build-shell.ps1 -Bundles msi    # 换包格式
#
# 产物（统一落顶层 dist/）：
#   dist/testpilot-sidecar/           PyInstaller onedir
#   dist/desktop/                     前端静态产物
#   dist/bundle/<format>/*.exe        Tauri 安装包（目标机免预装 Python/Node）
param(
    [switch]$SkipSidecar,
    [switch]$Dev,
    [string]$Bundles = 'nsis'
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

# ---------- 前置检查 ----------
Step '前置检查'
foreach ($cmd in @('node', 'npm', 'cargo')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "缺少 $cmd（Rust 用 rustup 安装；Node 22+ 官方安装包）"
    }
}
if (-not (Test-Path $desktop)) { throw "未找到壳工程: $desktop" }
Write-Host "      node $(& node --version) / npm $(& npm --version)"

# ---------- 开发态短路 ----------
if ($Dev) {
    if (-not $SkipSidecar) {
        Step '构建 sidecar'
        & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build-sidecar.ps1')
    }
    Step 'tauri dev'
    Set-Location $desktop
    & npm install
    & npm run shell:dev
    return
}

# ---------- 1. sidecar ----------
if ($SkipSidecar) {
    Step '跳过 sidecar（-SkipSidecar）'
    $sidecarDir = Join-Path $desktop 'src-tauri\binaries\sidecar'
    if (-not (Test-Path (Join-Path $sidecarDir 'testpilot-sidecar.exe')) -and
        -not (Test-Path (Join-Path $sidecarDir 'testpilot-sidecar'))) {
        Write-Host '     ⚠ 资源目录里没有 sidecar 可执行文件：打出来的包会拉不起引擎'
    }
} else {
    Step '构建 sidecar'
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build-sidecar.ps1')
}

# ---------- 2. 前端 ----------
Step '前端依赖与构建（tsc --noEmit && vite build）'
Set-Location $desktop
if (-not (Test-Path 'node_modules')) {
    & npm install
    Assert-LastExit 'npm install'
}
& npm run build
Assert-LastExit 'npm run build'

# ---------- 3. Tauri 打包 ----------
Step "Tauri 打包（bundles=$Bundles）"
& npm run shell:build -- --bundles $Bundles
Assert-LastExit 'tauri build'

# ---------- 4. 归档 ----------
Step '归档产物到顶层 dist/'
Set-Location $root
# 归档路径必须跟着 CARGO_TARGET_DIR 走：本机 C 盘几乎满，构建脚本会把
# CARGO_HOME / CARGO_TARGET_DIR 重定向到仓库内的 .cache/，此时 bundle 不在
# src-tauri/target 下 —— 写死路径会导致"打包成功但归档为空"（静默丢产物）。
$targetDir = if ($env:CARGO_TARGET_DIR) {
    $env:CARGO_TARGET_DIR
} else {
    Join-Path $desktop 'src-tauri\target'
}
if (-not [System.IO.Path]::IsPathRooted($targetDir)) {
    $targetDir = Join-Path $root $targetDir
}
Write-Host "     target 目录：$targetDir"

$bundleRoot = Join-Path $targetDir 'release\bundle'
$outBundle = Join-Path $root 'dist\bundle'
if (Test-Path $bundleRoot) {
    New-Item -ItemType Directory -Force -Path $outBundle | Out-Null
    Copy-Item -Recurse -Force (Join-Path $bundleRoot '*') $outBundle
} else {
    Write-Host "     ⚠ 未找到 $bundleRoot（tauri build 是否真跑成功？）"
}

$front = Join-Path $desktop 'dist'
if (Test-Path $front) {
    $outFront = Join-Path $root 'dist\desktop'
    New-Item -ItemType Directory -Force -Path $outFront | Out-Null
    Copy-Item -Recurse -Force (Join-Path $front '*') $outFront
}

# ---------- 5. 结果清单 ----------
Step '产物清单'
Get-ChildItem -Recurse -File $outBundle -ErrorAction SilentlyContinue |
    ForEach-Object { "     {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB) }

Write-Host ''
Write-Host '完成。安装包：dist/bundle/'
Write-Host '提示：目标机无需预装 Python/Node；首次运行会在 %LOCALAPPDATA% 建库与日志。'
