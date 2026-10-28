# 大肥鱼首次安装：从裸 Python 一路装到能启动，共 0–8 九段。
# 用法：
#   setup.bat                                   （推荐：双击或命令行都能跑，退出码原样透传）
#   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 -Proxy http://127.0.0.1:10808
#   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 -Force
#
# 阶段：0 Python 预检 → 1 venv → 2 依赖 → 3 config.yaml → 4 .env 向导
#       → 5 NapCat 下载 → 6 NapCat OneBot 配置 → 7 opencode 下载 → 8 doctor
#
# 第 4 段的取值顺序：.env 里已有可用 BOT_QQ 就保留原值（重跑不改写）→ 否则用环境变量
# BOT_QQ（可无人值守）→ 否则提问。仍是空值/占位值时第 6 段会跳过，填好后重跑本脚本即可。
param(
    [string]$Proxy = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$pythonUrl = "https://www.python.org/downloads/"
$envPath = Join-Path $root ".env"
$envTemplate = Join-Path $root ".env.example"
$napcatScript = Join-Path $PSScriptRoot "setup-napcat.ps1"
$opencodeScript = Join-Path $PSScriptRoot "setup-opencode.ps1"

# 只取值、不打印：第 4、6 段用它判断 BOT_QQ 是否已填。
# 必须显式 -Encoding UTF8：模板是无 BOM 的 UTF-8，PS 5.1 默认按 ANSI 代码页读会把中文注释读坏。
function Get-EnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) {
        return ""
    }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*?)\s*$'
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match $pattern) {
            return $matches[1].Trim('"').Trim("'")
        }
    }
    return ""
}

# 只改目标键这一行，其它键与注释原样保留；不回显写入的值。
# Get-Content 同样要 -Encoding UTF8，否则读写一轮就会把中文注释毁掉。
function Set-EnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    $hit = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $hit = $true
        }
    }
    if (-not $hit) {
        $lines += "$Key=$Value"
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($Path, $lines, $utf8NoBom)
}

# QQ 号的形状：5–12 位纯数字。模板自带的占位值也满足这个形状，所以另按模板值单独排除。
function Test-BotQQ {
    param([string]$Value)
    return ($Value -match '^\d{5,12}$')
}

# 失败就停在这一段，并给出可照抄的下一步。
function Stop-Stage {
    param([string]$Message, [string]$Hint = "")
    Write-Host "  失败：$Message" -ForegroundColor Red
    if ($Hint) {
        Write-Host "        $Hint" -ForegroundColor Yellow
    }
    exit 1
}

# ---- 0. Python 预检 ------------------------------------------------------
Write-Host "[0/8] 检查 Python 版本（需要 3.11 或更新）..." -ForegroundColor Cyan
$pyVersion = ""
try {
    $pyRaw = & python --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $pyVersion = ($pyRaw | Out-String).Trim()
    }
} catch {
    $pyVersion = ""
}
if (-not $pyVersion -or $pyVersion -notmatch '(\d+)\.(\d+)') {
    Stop-Stage "PATH 里没有可用的 python 命令。" "请从 $pythonUrl 安装 Python 3.11+（安装时勾选 Add python.exe to PATH），重开终端后再运行本脚本。"
}
$pyMajor = [int]$matches[1]
$pyMinor = [int]$matches[2]
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 11)) {
    Stop-Stage "当前 Python $pyMajor.$pyMinor 太旧，需要 3.11 或更新版本。" "请从 $pythonUrl 安装 Python 3.11+，重开终端后再运行本脚本。"
}
Write-Host "      Python $pyMajor.$pyMinor 可用" -ForegroundColor Green

# ---- 1. venv ------------------------------------------------------------
Write-Host "[1/8] 准备虚拟环境 .venv ..." -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path ".venv\Scripts\python.exe")) {
        Stop-Stage "创建 .venv 失败（python -m venv 退出码 $LASTEXITCODE）。" "删掉半成品 .venv 后重跑；也可手动执行 python -m venv .venv 看完整报错。"
    }
    Write-Host "      已创建 .venv" -ForegroundColor Green
} else {
    Write-Host "      .venv 已存在，跳过" -ForegroundColor Green
}

# ---- 2. 依赖 ------------------------------------------------------------
Write-Host "[2/8] 安装依赖 ..." -ForegroundColor Cyan
$marker = ".venv\.deps-installed"
$needInstall = $true
if ((Test-Path $marker) -and (Test-Path "pyproject.toml")) {
    if ((Get-Item $marker).LastWriteTime -ge (Get-Item "pyproject.toml").LastWriteTime) {
        $needInstall = $false
    }
}
if ($needInstall) {
    & .venv\Scripts\python -m pip install -q --disable-pip-version-check -e .
    if ($LASTEXITCODE -ne 0) {
        Stop-Stage "安装依赖失败（pip 退出码 $LASTEXITCODE）。" "检查网络或代理后重跑；必要时先执行 .venv\Scripts\python -m pip install -U pip。"
    }
    Set-Content -LiteralPath $marker -Value (Get-Date).ToString("o")
    Write-Host "      依赖已安装" -ForegroundColor Green
} else {
    Write-Host "      依赖已安装，跳过" -ForegroundColor Green
}

# ---- 3. config.yaml -----------------------------------------------------
Write-Host "[3/8] 生成 config.yaml ..." -ForegroundColor Cyan
if (-not (Test-Path "config.yaml")) {
    & .venv\Scripts\python -m dafeiyu.cli init
    if ($LASTEXITCODE -ne 0) {
        Stop-Stage "生成 config.yaml 失败（dafeiyu init 退出码 $LASTEXITCODE）。" "确认 config.example.yaml 还在，然后重跑本脚本。"
    }
} else {
    Write-Host "      config.yaml 已存在，跳过" -ForegroundColor Green
}

# ---- 4. .env 向导 -------------------------------------------------------
# 只报告「已创建 / 已存在 / BOT_QQ 是否可用」，永不回显文件内容或具体值。
# 模板里的 BOT_QQ 是占位值（形状合法但属于模板自己），必须当成「没填」：
# 新建的文件会先清空该行，已存在的旧文件里等于模板值的也按无效处理。
$templateQQ = Get-EnvValue $envTemplate "BOT_QQ"

Write-Host "[4/8] 准备 .env（QQ 号等凭据）..." -ForegroundColor Cyan
if (Test-Path $envPath) {
    Write-Host "      .env 已存在" -ForegroundColor Green
} else {
    if (-not (Test-Path $envTemplate)) {
        Stop-Stage "找不到 .env.example 模板。" "发行包不完整：重新解压一次发行包，或从仓库取 .env.example 放回根目录。"
    }
    Copy-Item -LiteralPath $envTemplate -Destination $envPath
    Write-Host "      已创建 .env（来自 .env.example）" -ForegroundColor Green
    if ($templateQQ) {
        # 占位值不能留在新文件里，否则下面会把它当作用户填的号。
        Set-EnvValue $envPath "BOT_QQ" ""
        Write-Host "      模板里的 BOT_QQ 是占位值，已清空待填" -ForegroundColor DarkGray
    }
}

$envVarQQ = ""
if ($env:BOT_QQ) {
    $envVarQQ = $env:BOT_QQ.Trim()
}
$currentQQ = Get-EnvValue $envPath "BOT_QQ"
$currentValid = (Test-BotQQ $currentQQ) -and ($currentQQ -ne $templateQQ)

if ($currentValid) {
    Write-Host "      .env 里已有可用 BOT_QQ，保留原值（不询问、不覆盖）" -ForegroundColor Green
} elseif ($envVarQQ -and (Test-BotQQ $envVarQQ) -and ($envVarQQ -ne $templateQQ)) {
    Set-EnvValue $envPath "BOT_QQ" $envVarQQ
    Write-Host "      BOT_QQ 已按环境变量写入 .env（不回显具体值）" -ForegroundColor Green
} else {
    if ($currentQQ) {
        Write-Host "      现有 BOT_QQ 无效（模板占位值或不是 5–12 位数字），需要重新填写。" -ForegroundColor Yellow
    }
    if ($envVarQQ) {
        Write-Host "      环境变量 BOT_QQ 也无效（需要 5–12 位数字），改为手工输入。" -ForegroundColor Yellow
    }
    Write-Host "      请输入机器人 QQ 号（5–12 位数字，直接回车可跳过）" -ForegroundColor Yellow
    $answer = Read-Host "      BOT_QQ"
    if ($answer -and $answer.Trim()) {
        if (Test-BotQQ $answer.Trim()) {
            Set-EnvValue $envPath "BOT_QQ" $answer.Trim()
            Write-Host "      BOT_QQ 已写入 .env" -ForegroundColor Green
        } else {
            Write-Host "      输入的不是 5–12 位数字，未写入 .env。" -ForegroundColor Yellow
        }
    } else {
        Write-Host "      未填 BOT_QQ：第 6 段会跳过，填好后重跑本脚本即可补上 OneBot 配置。" -ForegroundColor Yellow
    }
}

# ---- 5. NapCat 下载 -----------------------------------------------------
Write-Host "[5/8] 下载 NapCat（解压后约 345MB，含 node）..." -ForegroundColor Cyan
$napcatDownload = @{ DownloadOnly = $true }
if ($Proxy) {
    $napcatDownload["Proxy"] = $Proxy
}
if ($Force) {
    $napcatDownload["Force"] = $true
}
$LASTEXITCODE = 0
try {
    & $napcatScript @napcatDownload
} catch {
    Stop-Stage "NapCat 下载失败：$_" "可加 -Proxy 重试，或手动执行 powershell -ExecutionPolicy Bypass -File scripts\setup-napcat.ps1 -DownloadOnly。"
}
if ($LASTEXITCODE -ne 0) {
    Stop-Stage "NapCat 下载失败（退出码 $LASTEXITCODE）。" "可加 -Proxy 重试，或手动执行 powershell -ExecutionPolicy Bypass -File scripts\setup-napcat.ps1 -DownloadOnly。"
}

# ---- 6. NapCat OneBot 配置 ----------------------------------------------
# 不带 -DownloadOnly 的调用要读 .env 里的 BOT_QQ，缺了/是占位值就跳过而不是报错。
Write-Host "[6/8] 写入 NapCat OneBot 配置 ..." -ForegroundColor Cyan
$botQQ = Get-EnvValue $envPath "BOT_QQ"
if (-not (Test-BotQQ $botQQ) -or $botQQ -eq $templateQQ) {
    Write-Host "      跳过：.env 里还没有可用的 BOT_QQ（5–12 位数字），填好后重跑本脚本即可写入。" -ForegroundColor Yellow
} else {
    $napcatConfig = @{}
    if ($Proxy) {
        $napcatConfig["Proxy"] = $Proxy
    }
    $LASTEXITCODE = 0
    try {
        & $napcatScript @napcatConfig
    } catch {
        Stop-Stage "写 OneBot 配置失败：$_" "确认 .env 里的 BOT_QQ / NAPCAT_WS_PORT，然后手动执行 powershell -ExecutionPolicy Bypass -File scripts\setup-napcat.ps1。"
    }
    if ($LASTEXITCODE -ne 0) {
        Stop-Stage "写 OneBot 配置失败（退出码 $LASTEXITCODE）。" "确认 .env 里的 BOT_QQ / NAPCAT_WS_PORT 后重跑本脚本。"
    }
}

# ---- 7. opencode -------------------------------------------------------
Write-Host "[7/8] 下载 opencode.exe ..." -ForegroundColor Cyan
$opencodeParams = @{}
if ($Proxy) {
    $opencodeParams["Proxy"] = $Proxy
}
if ($Force) {
    $opencodeParams["Force"] = $true
}
$LASTEXITCODE = 0
try {
    & $opencodeScript @opencodeParams
} catch {
    Stop-Stage "opencode 下载失败：$_" "可加 -Proxy 重试，或手动下载后放到 vendor\opencode\opencode.exe 再重跑本脚本。"
}
if ($LASTEXITCODE -ne 0) {
    Stop-Stage "opencode 下载失败（退出码 $LASTEXITCODE）。" "可加 -Proxy 重试，或手动下载后放到 vendor\opencode\opencode.exe 再重跑本脚本。"
}

# ---- 8. 体检，退出码原样透传 ------------------------------------------
Write-Host "[8/8] 运行体检（doctor）..." -ForegroundColor Cyan
$LASTEXITCODE = 0
& .venv\Scripts\python -m dafeiyu.cli doctor
$doctorCode = $LASTEXITCODE
Write-Host ""
if ($doctorCode -eq 0) {
    Write-Host "安装完成。下一步：scripts\start.bat" -ForegroundColor Green
} else {
    Write-Host "体检还有阻塞项（退出码 $doctorCode）：按上面 → 的提示补齐后重跑本脚本。" -ForegroundColor Yellow
}
exit $LASTEXITCODE
