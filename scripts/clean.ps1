# 清理构建产物与临时缓存（**绝不动源码与 data/**）。
#
# 用法（仓库根）：
#   powershell -ExecutionPolicy Bypass -File scripts/clean.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/clean.ps1 -Deep     # 连 cargo 缓存一起清
#   powershell -ExecutionPolicy Bypass -File scripts/clean.ps1 -WhatIf   # 只看会删什么
param(
    [switch]$Deep,
    [switch]$WhatIf
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 白名单式清理：只清"确定是产物"的路径；任何源码/配置/数据都不在列表里
$targets = @(
    'build',
    'dist',
    '.cache',
    '.pytest_cache',
    '.ruff_cache',
    'testpilot\app\desktop\dist',
    'testpilot\app\desktop\src-tauri\target',
    'testpilot\app\desktop\src-tauri\gen',
    'node_modules',
    'testpilot\app\desktop\node_modules'
)
if ($Deep) {
    $targets += @('.venv', 'logs\_cargohome', 'logs\_cargotarget', 'logs\_npmcache')
}

$total = 0
foreach ($rel in $targets) {
    $path = Join-Path $root $rel
    if (-not (Test-Path $path)) { continue }
    $size = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
             Measure-Object Length -Sum).Sum
    $total += [int]$size
    if ($WhatIf) {
        "将会删除 {0}  ({1:N0} MB)" -f $rel, ($size / 1MB)
    } else {
        "删除 {0}  ({1:N0} MB)" -f $rel, ($size / 1MB)
        Remove-Item -Recurse -Force $path -ErrorAction SilentlyContinue
    }
}

# logs 目录只清内容，保留 README（目录本身必须存在）
$logs = Join-Path $root 'logs'
if (Test-Path $logs) {
    Get-ChildItem $logs -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'README.md' } |
        ForEach-Object {
            if ($WhatIf) { "将会删除 logs\{0}" -f $_.Name }
            else { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }
        }
}

Write-Host ''
if ($WhatIf) {
    Write-Host ("预计释放 {0:N0} MB（-WhatIf 未实际删除）" -f ($total / 1MB))
} else {
    Write-Host ("已释放约 {0:N0} MB" -f ($total / 1MB))
    Write-Host '保留：源码 / docs / packaging / scripts / tests / data / .venv（除非 -Deep）'
}
