# 打包前静态自检 —— **只用 PowerShell 内建能力**，不派生子进程。
#
# 为什么需要它：本机安全软件会间歇性拦截会话派生的子进程（python/node/cargo 全部
# "退出码 0 但什么都不做"）。此时 pytest/tsc/cargo 都跑不了，但"配置对不对、产物齐不齐、
# 脚本有没有语法错"这些**打包必错项**仍然可以静态兜住 —— 提前发现远比打完包再查便宜。
#
# 用法（仓库根，两种皆可）：
#   powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
#   . .\scripts\preflight.ps1     # 点源（嵌套 powershell -File 被安全策略拦时用这个）
#
# 结果：逐条打印 + 设 `$global:PREFLIGHT_OK`。
# 退出码：`-File` 调用下失败返回 1（供 CI/脚本链判定）；点源调用下永不 exit
#         （否则会关掉用户的交互式会话）。实现见文件末尾。
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root
$global:PREFLIGHT_OK = $true

$problems = New-Object System.Collections.Generic.List[string]
$checks = 0
$script:warnings = 0

function Check([bool]$ok, [string]$what) {
    $script:checks++
    if ($ok) {
        Write-Output ('  [OK]   ' + $what)
    } else {
        Write-Output ('  [FAIL] ' + $what)
        $script:problems.Add($what)
    }
}

function Warn([string]$what) {
    $script:warnings++
    Write-Output ('  [WARN] ' + $what)
}

function Read-Json([string]$path) {
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

Write-Output ''
Write-Output '== 1. 关键文件与目录 =='
$required = @(
    'pyproject.toml', 'README.md',
    'testpilot\app\cli.py', 'testpilot\config\default.yaml',
    'testpilot\agent\pi_rpc_tools.mjs', 'testpilot\agent\pi_runtime.py',
    'testpilot\agent\change_service.py', 'testpilot\adapters\svn_adapter.py',
    'testpilot\app\desktop\package.json',
    'testpilot\app\desktop\vite.config.ts',
    'testpilot\app\desktop\index.html',
    'testpilot\app\desktop\tsconfig.json',
    'testpilot\app\desktop\src\main.tsx',
    'testpilot\app\desktop\src\App.tsx',
    'testpilot\app\desktop\src\bridge.ts',
    'testpilot\app\desktop\src-tauri\Cargo.toml',
    'testpilot\app\desktop\src-tauri\build.rs',
    'testpilot\app\desktop\src-tauri\tauri.conf.json',
    'testpilot\app\desktop\src-tauri\capabilities\default.json',
    'testpilot\app\desktop\src-tauri\src\main.rs',
    'testpilot\app\desktop\src-tauri\src\engine.rs',
    'testpilot\app\desktop\src-tauri\src\rpc.rs',
    'packaging\run_sidecar.py', 'packaging\sidecar.spec', 'packaging\hiddenimports.txt',
    'scripts\build-sidecar.ps1', 'scripts\build-shell.ps1', 'scripts\clean.ps1',
    'scripts\smoke-shell.ps1',
    'tools\verify.py', 'tools\check_bundle.py'
)
foreach ($rel in $required) {
    Check (Test-Path (Join-Path $root $rel)) $rel
}

Write-Output ''
Write-Output '== 2. tauri.conf.json =='
$confPath = Join-Path $root 'testpilot\app\desktop\src-tauri\tauri.conf.json'
$conf = $null
try {
    $conf = Read-Json $confPath
    Check $true 'tauri.conf.json 可解析为 JSON'
} catch {
    Check $false ('tauri.conf.json JSON 非法: ' + $_.Exception.Message)
}

if ($conf) {
    Check ([bool]$conf.productName) 'productName 非空'
    Check ([bool]$conf.identifier) 'identifier 非空'
    Check ($conf.build.frontendDist -eq '../dist') 'build.frontendDist = ../dist'
    Check ([bool]$conf.build.beforeBuildCommand) 'build.beforeBuildCommand 已配置（保证用最新前端产物）'
    Check ($conf.app.windows.Count -ge 1) '至少定义一个窗口'
    Check ($conf.app.windows[0].label -eq 'main') '主窗口 label = main（capabilities 按 label 授权）'
    Check ([bool]$conf.app.security.csp) '生产环境 CSP 非空'

    if ($conf.bundle.PSObject.Properties.Name -contains 'externalBin') {
        Check $false 'bundle.externalBin 必须移除（onedir 装不进单文件 externalBin）'
    } else {
        Check $true '未使用 externalBin（onedir 走 resources）'
    }
    $res = @($conf.bundle.resources)
    Check ($res -contains 'binaries/sidecar/**/*') `
        'bundle.resources 用递归 glob（单层 * 会丢掉 _internal/）'

    $engineRs = Get-Content -LiteralPath 'testpilot\app\desktop\src-tauri\src\engine.rs' -Raw -Encoding UTF8
    if ($engineRs -match 'pub const SIDECAR_DIR: &str = "([^"]+)"') {
        $dir = $Matches[1]
        Check ($res -contains "binaries/$dir/**/*") "resources 目录与 Rust SIDECAR_DIR 一致（$dir）"
    } else {
        Check $false 'engine.rs 未定义 SIDECAR_DIR'
    }
    Check ($engineRs -match 'current_dir') '壳把 sidecar 的 cwd 设到可写目录'
    Check ($engineRs -match 'TESTPILOT_HOME') '壳向引擎传递 TESTPILOT_HOME'

    foreach ($icon in @($conf.bundle.icon)) {
        $p = Join-Path (Split-Path $confPath -Parent) $icon
        Check (Test-Path $p) "图标存在: $icon"
    }
}

$specText = Get-Content -LiteralPath 'packaging\sidecar.spec' -Raw -Encoding UTF8
Check ($specText -match 'pi_rpc_tools\.mjs') 'Pi RPC extension 已列入 sidecar datas'
$buildSidecarText = Get-Content -LiteralPath 'scripts\build-sidecar.ps1' -Raw -Encoding UTF8
Check ($buildSidecarText -match 'pi_rpc_tools\.mjs;testpilot/agent') 'onefile 构建同样携带 Pi RPC extension'
Write-Output ''
Write-Output '== 3. capabilities / 权限最小化 =='
$capPath = Join-Path $root 'testpilot\app\desktop\src-tauri\capabilities\default.json'
try {
    $cap = Read-Json $capPath
    Check (($cap.permissions -join ',') -eq 'core:default') '权限仅 core:default（业务全走自定义命令）'
    Check ($cap.windows -contains 'main') '授权窗口含 main'
} catch {
    Check $false ('capabilities JSON 非法: ' + $_.Exception.Message)
}

Write-Output ''
Write-Output '== 4. sidecar 资源目录（Tauri glob 要求非空） =='
$sidecarDir = Join-Path $root 'testpilot\app\desktop\src-tauri\binaries\sidecar'
Check (Test-Path $sidecarDir) 'binaries/sidecar 目录存在'
$sidecarFiles = @(Get-ChildItem -LiteralPath $sidecarDir -Force -ErrorAction SilentlyContinue)
Check ($sidecarFiles.Count -ge 1) 'sidecar 目录至少有一个文件'
if (Test-Path (Join-Path $sidecarDir 'testpilot-sidecar.exe')) {
    Check $true '已生成 testpilot-sidecar.exe'
    # onedir 完整性的关键：单层 glob 只搬 exe，_internal/ 会静默丢失
    $internal = Join-Path $sidecarDir '_internal'
    Check (Test-Path $internal) 'onedir 的 _internal/ 已投放（否则安装后拉不起引擎）'
    if (Test-Path $internal) {
        $rt = @(Get-ChildItem -LiteralPath $internal -Filter 'python3*.dll' -ErrorAction SilentlyContinue)
        Check ($rt.Count -ge 1) '_internal/ 含 python3*.dll（Python 运行时）'
    }
} else {
    Warn '尚未生成 testpilot-sidecar.exe —— 先跑 scripts/build-sidecar.ps1'
}

Write-Output ''
Write-Output '== 5. PowerShell 脚本语法与编码 =='
foreach ($s in @('scripts\build-sidecar.ps1', 'scripts\build-shell.ps1',
                 'scripts\clean.ps1', 'scripts\preflight.ps1',
                 'scripts\smoke-shell.ps1')) {
    $full = Join-Path $root $s
    # 编码门禁：Windows PowerShell 5.1 对**无 BOM** 的 UTF-8 会按 ANSI(GBK) 解码，
    # 中文注释/字符串会把语法解析搞崩（报"意外的标记"）。这些脚本必须带 BOM。
    $bytes = [System.IO.File]::ReadAllBytes($full)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and
               $bytes[1] -eq 187 -and $bytes[2] -eq 191)
    Check $hasBom "$s 带 UTF-8 BOM（PS 5.1 正确解析中文的前提）"

    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $full, [ref]$null, [ref]$errors)
    if ($errors -and $errors.Count -gt 0) {
        Check $false ("$s 语法错误: " + $errors[0].Message)
    } else {
        Check $true "$s 语法通过"
    }

    # Stop 偏好 + 原生命令 + 合并 stderr（2>&1）= 定时炸弹：
    # 原生命令只要往 stderr 写一个字节，PowerShell 就抛 RemoteException 中断脚本，
    # 表现为"输出全绿却退出码 1"（本机实测：doctor 的 cv2 降级 WARNING 必触发）。
    # 这些脚本都设了 $ErrorActionPreference='Stop'，所以要拦住合并 stderr。
    #
    # 必须走 AST 的 MergingRedirectionAst，**不能**用正则找 "2>&1" 文本：
    # 正则会把字符串字面量 '2>&1'、[regex] 模式串、以及本段注释自身都当成命中，
    # 造成自指误报（实测：本脚本第一次就是被自己的检测代码误判为 FAIL）。
    if ($ast -and ($text = [System.IO.File]::ReadAllText($full)) -match "ErrorActionPreference\s*=\s*'Stop'") {
        $merges = @($ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.MergingRedirectionAst] }, $true))
        Check ($merges.Count -eq 0) ("$s 未在 Stop 偏好下合并 stderr（2>&1/>&2）" +
            $(if ($merges.Count -gt 0) {
                " —— 命中行: " + (($merges | ForEach-Object { $_.Extent.StartLineNumber }) -join ',')
              } else { '' }))
    }
}

Write-Output ''
Write-Output '== 6. 前端工程 =='
try {
    $pkg = Read-Json (Join-Path $root 'testpilot\app\desktop\package.json')
    Check ([bool]$pkg.scripts.build) 'package.json 有 build 脚本'
    Check ([bool]$pkg.scripts.'shell:build') 'package.json 有 shell:build 脚本'
    Check ([bool]$pkg.dependencies.'@tauri-apps/api') '@tauri-apps/api 已声明'
    Check ([bool]$pkg.devDependencies.'@tauri-apps/cli') '@tauri-apps/cli 已声明（tauri build 需要）'
} catch {
    Check $false ('package.json 非法: ' + $_.Exception.Message)
}
try {
    $ts = Read-Json (Join-Path $root 'testpilot\app\desktop\tsconfig.json')
    Check ($ts.compilerOptions.strict -eq $true) 'tsconfig strict 已开'
} catch {
    Check $false ('tsconfig.json 非法: ' + $_.Exception.Message)
}

Write-Output ''
Write-Output '== 7. 隐式依赖清单（spec <-> 快速模式 一致性） =='
# 权威来源是 sidecar.spec 的 _REQUIRED；build-sidecar.ps1 的 -Onefile 内嵌同一份。
# 两处漂移会导致"某种打包方式缺依赖"，因此这里直接比对（不依赖能否跑 Python）。
$specPath = Join-Path $root 'packaging\sidecar.spec'
$bsPath = Join-Path $root 'scripts\build-sidecar.ps1'
$specText = Get-Content -LiteralPath $specPath -Raw -Encoding UTF8
$bsText = Get-Content -LiteralPath $bsPath -Raw -Encoding UTF8

if ($specText -notmatch 'websockets') {
    Warn 'sidecar.spec 读取结果不含预期模块名（可能被安全软件加密）—— 请在用户终端复核'
}
$specBlock = [regex]::Match($specText, '_REQUIRED = \[(.*?)\r?\n\]', 'Singleline')
$bsBlock = [regex]::Match($bsText, '\$hidden = @\((.*?)\r?\n\)', 'Singleline')
if (-not $specBlock.Success) { Check $false 'spec 中未找到 _REQUIRED 列表' }
if (-not $bsBlock.Success) { Check $false 'build-sidecar.ps1 中未找到 $hidden 列表' }

$specMods = @()
$bsMods = @()
if ($specBlock.Success) {
    # _REQUIRED 块里只有模块名字符串，直接全取（不要再加"必须含点"之类过滤，
    # 否则裸模块名如 PIL 会被误剔除，造成假失败）
    $specMods = @([regex]::Matches($specBlock.Groups[1].Value, "'([A-Za-z_][\w.]*)'") |
                  ForEach-Object { $_.Groups[1].Value })
}
if ($bsBlock.Success) {
    $bsMods = @([regex]::Matches($bsBlock.Groups[1].Value, "'([A-Za-z_][\w.]*)'") |
                ForEach-Object { $_.Groups[1].Value })
}
Check ($specMods.Count -ge 15) ("spec 隐式依赖条目 = " + $specMods.Count)
$onlySpec = @($specMods | Where-Object { $bsMods -notcontains $_ })
$onlyBs = @($bsMods | Where-Object { $specMods -notcontains $_ })
Check ($onlySpec.Count -eq 0 -and $onlyBs.Count -eq 0) `
    ("两处清单一致（仅 spec 有: [" + ($onlySpec -join ',') + "] 仅脚本有: [" + ($onlyBs -join ',') + "]）")

Write-Output ''
Write-Output '== 8. Rust 依赖声明 =='
$cargo = Get-Content 'testpilot\app\desktop\src-tauri\Cargo.toml' -Raw -Encoding UTF8
Check ($cargo -match 'tauri-plugin-shell') 'Cargo.toml 依赖 tauri-plugin-shell（shell().command 需要）'
Check ($cargo -match 'tokio-tungstenite') 'Cargo.toml 依赖 tokio-tungstenite（RPC 客户端需要）'
Check ($cargo -match 'panic = "abort"') 'release profile 已配置 panic=abort'

Write-Output ''
if ($problems.Count -eq 0) {
    $global:PREFLIGHT_OK = $true
    Write-Output ("预检通过：{0} 项检查全部通过（警告 {1} 条）。" -f $checks, $warnings)
    Write-Output '下一步：powershell -ExecutionPolicy Bypass -File scripts\build-shell.ps1'
} else {
    $global:PREFLIGHT_OK = $false
    Write-Output ("预检失败：{0} 项检查中有 {1} 项不通过：" -f $checks, $problems.Count)
    foreach ($p in $problems) { Write-Output ('  - ' + $p) }
}

# 退出码契约：`-File` 调用（CI / 构建脚本链）失败必须非 0，否则上层的
# `if ($LASTEXITCODE -ne 0)` 永不触发 —— 预检会变成"永远通过"的假门禁。
# 点源调用（`. .\scripts\preflight.ps1`）则**不能** exit，否则会把用户当前的
# 交互式会话直接关掉（这正是本脚本原先完全不 exit 的原因）。
# 实测：InvocationName 在 -File 下是脚本路径，在点源下是 '.'。
if ($problems.Count -gt 0 -and $MyInvocation.InvocationName -ne '.') { exit 1 }
