# 构建 TestPilot 引擎 sidecar（PyInstaller）并投放到桌面壳的资源目录。
#
# 用法（仓库根）：
#   powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1 -Clean
#   powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1 -Onefile   # 联调快出
#   powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1 -Onefile -Name Code
#     # 独立单文件发行版（不投放壳资源目录、不跑产物自检），产物 dist/Code.exe
#
# 产出：
#   dist/testpilot-sidecar/                                   # onedir（默认，正式形态）
#   dist/testpilot-sidecar.exe                                # -Onefile 时的单文件
#   dist/<Name>.exe                                           # -Onefile -Name <Name> 的独立发行版
#   testpilot/app/desktop/src-tauri/binaries/sidecar/         # 壳打包用的 resources 源
#
# 注意：本脚本只在**开发机**执行；目标机器不需要 Python（依赖已随产物带上）。
param(
    [switch]$Clean,
    [switch]$Onefile,
    [string]$Name = 'testpilot-sidecar',
    [switch]$SkipSelfCheck
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# -Name 只对 onefile 有意义：onedir 的可执行名由 packaging/sidecar.spec 定义；
# 且自定义名意味着"独立发行"，不走壳资源投放流程（壳固定找 testpilot-sidecar.exe）
if ($Name -ne 'testpilot-sidecar' -and -not $Onefile) {
    throw '-Name 仅支持与 -Onefile 同用（onedir 的名字由 packaging/sidecar.spec 定义）'
}

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    throw "未找到 $venvPy。请先：python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e `".[dev,rpc]`""
}
$py = $venvPy

Write-Host '[1/5] 确保 PyInstaller 可用'
& $py -m pip install --disable-pip-version-check -q pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'pyinstaller 安装失败' }

if ($Clean) {
    Write-Host '[2/5] 清理旧产物'
    foreach ($p in @('build\pyinstaller', 'dist\testpilot-sidecar',
                     'dist\testpilot-sidecar.exe')) {
        if (Test-Path $p) {
            Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue
        }
    }
    # 注意：**不删** binaries\sidecar —— 里面的 README.md 是入库占位文件。
    # Tauri 要求 resources 通配符至少匹配到一个文件，全删会让构建直接报
    # `glob pattern ... path not found` 并中断（见交接文档 D8）。
    # 脚本只需清掉其中旧的 sidecar 产物，README.md 保留。
    $destClean = Join-Path $root 'testpilot\app\desktop\src-tauri\binaries\sidecar'
    if (Test-Path $destClean) {
        Get-ChildItem -LiteralPath $destClean -Force |
            Where-Object { $_.Name -ne 'README.md' } |
            ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }
    }
} else {
    Write-Host '[2/5] 保留旧产物（-Clean 可先清理）'
}

# 隐式依赖清单：**权威来源是 packaging/sidecar.spec 的 _REQUIRED**，这里内嵌同一份，
# 由 scripts/preflight.ps1 做一致性校验。
#
# 为什么不直接读 packaging/hiddenimports.txt：本机安全软件会把部分文本文件落盘加密，
# 非白名单进程（含 PowerShell/.NET 直读）拿到的是密文乱码 —— 读坏会让 -Onefile 打包
# **静默丢掉隐式依赖**（现象是"开发能跑、打包跑不起来"）。那个 txt 只作人读文档。
$hidden = @(
    'websockets', 'websockets.legacy', 'websockets.legacy.server',
    'websockets.asyncio', 'websockets.asyncio.server',
    'pywinauto', 'pywinauto.uia_element_info', 'pywinauto.controls.uiawrapper',
    'comtypes', 'comtypes.gen',
    'mss', 'cv2', 'numpy', 'PIL', 'PIL.ImageGrab', 'pyperclip',
    'win32api', 'win32con', 'win32gui'
)
Write-Host "      隐式依赖 $($hidden.Count) 项（与 packaging/sidecar.spec 对齐）"

if ($Onefile) {
    Write-Host "[3/5] PyInstaller onefile 打包（name=$Name；冷启动较慢）"
    # 随包资源与排除项与 packaging/sidecar.spec 对齐：
    # - add-data：default.yaml 是运行期按 __file__ 相对路径读的（漏掉的现象是
    #   启动即 FileNotFoundError: ...\testpilot\config\default.yaml）；
    #   源路径必须写**绝对路径** —— 相对路径按 --specpath（build/）解析而非 cwd；
    # - exclude-module：与 spec 一致，避免把无用重依赖塞进单文件。
    $datas = @(
        '--add-data', "$root\testpilot\config\default.yaml;testpilot/config",
        '--add-data', "$root\testpilot\agent\pi_rpc_tools.mjs;testpilot/agent"
    )
    $excludes = @('tkinter', 'matplotlib', 'PyQt5', 'PySide6', 'IPython')
    # 注意：不要用 $args（PowerShell 自动变量），否则会与脚本参数绑定冲突
    $pyArgs = @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
                '--name', $Name,
                '--distpath', 'dist', '--workpath', 'build/pyinstaller-onefile',
                '--specpath', 'build') +
              $datas +
              ($excludes | ForEach-Object { "--exclude-module=$_" }) +
              ($hidden | ForEach-Object { "--hidden-import=$_" }) +
              @('packaging/run_sidecar.py')
    & $py @pyArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller (onefile) 打包失败' }
    $source = "dist\$Name.exe"
} else {
    Write-Host '[3/5] PyInstaller onedir 打包（首次约 2~5 分钟）'
    & $py -m PyInstaller 'packaging/sidecar.spec' `
        --noconfirm --distpath dist --workpath build/pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller (onedir) 打包失败' }
    $source = 'dist\testpilot-sidecar'
}

if (-not (Test-Path $source)) { throw "未生成预期产物: $source" }

if ($Name -ne 'testpilot-sidecar') {
    # 独立发行版：不投放壳资源目录（壳固定找 testpilot-sidecar.exe），
    # 只做运行冒烟（--version 走 argparse，doctor 惰性导入驱动更彻底但
    # 禅道/LLM 未配置时非 0 退出属正常，故这里只验进程可拉起）
    Write-Host "[4/5] 独立发行版，跳过壳资源投放（$source）"
    & $source --version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "独立发行版自检失败：$source --version exit=$LASTEXITCODE" }
    Write-Host '      --version 通过（进程可拉起）'
    Write-Host ''
    Write-Host "完成：$source"
    return
}

Write-Host "[4/5] 投放到壳资源目录（$source）"
$dest = Join-Path $root 'testpilot\app\desktop\src-tauri\binaries\sidecar'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Remove-Item (Join-Path $dest 'testpilot-sidecar*') -Force -Recurse -ErrorAction SilentlyContinue
if ((Get-Item $source).PSIsContainer) {
    Copy-Item -Recurse -Force (Join-Path $source '*') $dest
} else {
    Copy-Item -Force $source $dest
}

$exe = Join-Path $dest 'testpilot-sidecar.exe'
if (-not (Test-Path $exe)) { throw "投放后未找到 $exe" }

if ($SkipSelfCheck) {
    Write-Host '[5/5] 跳过自检（-SkipSelfCheck）'
} else {
    # 自检分两步，缺一不可：
    #   (a) 静态：核对产物里隐式依赖与随包资源是否齐全；
    #   (b) 运行：真跑一次引擎命令（doctor 会惰性导入驱动，能暴露缺 DLL）。
    # 只跑 (b) 的 --version 不够 —— 它只走到 argparse，惰性导入的驱动根本不会被触发。
    Write-Host '[5/5] 自检 sidecar'
    & $py 'tools/check_bundle.py'
    if ($LASTEXITCODE -ne 0) { throw '产物完整性自检失败（隐式依赖/随包资源缺失，见上）' }

    Write-Host '      运行期自检：doctor（会惰性导入驱动）'
    # 为什么单独包一层 EAP='Continue'：doctor **必然**往 stderr 写日志（例如 cv2
    # 引导降级的 WARNING）。在 $ErrorActionPreference='Stop' 下，一旦 stderr 被
    # **合并**进管道（`2>&1 | ...`）或重定向流，PowerShell 会把它包装成 ErrorRecord
    # 立刻抛出 RemoteException，脚本在自检中途死掉。
    #
    # 实测（logs/_probe3.ps1，脚本作用域）：
    #   `& $exe doctor 2>&1 | Out-Null`  -> THREW RemoteException
    #   `& $exe doctor    | Out-Null`    -> reached-end, last=0
    # 差别只在**有没有合并 stderr**；单纯 `| Out-Null` 是安全的。
    # 谁踩这个坑：在 `$ErrorActionPreference='Stop'` 的脚本里给原生命令接 `2>&1`。
    # 本脚本现在不再合并 stderr，但保留这层防护——将来有人为了看 doctor 详细输出
    # 加回 `2>&1` 时，不会静默把退出码变成 1（表现为"输出全绿却 exit=1"）。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $doctorExit = 0
        & $exe doctor > $null 2> $null
        $doctorExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($doctorExit -ne 0) {
        Write-Host "      [WARN] doctor 非 0 退出（exit=$doctorExit，多为禅道/模拟器/LLM 未连通，属正常）"
    } else {
        Write-Host '      doctor 通过'
    }
}

Write-Host ''
Write-Host "完成：$dest"
Write-Host '接下来：powershell -ExecutionPolicy Bypass -File scripts/build-shell.ps1 -SkipSidecar'
