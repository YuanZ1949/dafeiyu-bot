# 在本机注册并启动 Gitea Actions runner（Windows host 模式）。
#
# 为什么是 host 模式而不是 Docker：
#   1. 本机没有 Docker，也没有 WSL2；host 模式是唯一能直接跑起来的方式。
#   2. 本仓库大量测试依赖 PowerShell / 计划任务 / Windows 文件 ACL / Windows 路径，
#      必须在 Windows 宿主上执行，容器里会跳过或失真。
#   3. Windows host 不支持 Docker actions，所以 .gitea/workflows/ci.yml 只用 builtin:*
#      动作，不依赖任何外部 JS action。
#
# 前置条件（脚本会检查，缺了直接报错）：
#   - git >= 2.34.1（builtin:checkout 需要）
#   - PowerShell 7（pwsh，job 的 shell）
#   - Python >= 3.11（CI 用它创建 .ci-venv；缺了只警告，job 会失败）
#
# 推荐用法（token 走环境变量，避免出现在命令行历史 / 进程列表里）：
#   $env:GITEA_RUNNER_REGISTRATION_TOKEN = '<在 Gitea /admin/actions/runners 复制的 token>'
#   powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1
#
# 常用变体：
#   # 只打印将执行的命令，不下载、不注册、不建任务
#   powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1 -DryRun
#
#   # 自定义实例 / 目录
#   powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1 `
#       -Instance http://yuannas.local:3000 -RunnerDir D:\gitea-runner
#
#   # 已注册过，想覆盖本地注册信息（.runner）后重注册
#   powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1 -Force
#
# 注意：
#   - 默认用「登录时触发的计划任务」以当前用户身份运行，这样 job 能用到当前用户的
#     Python / git / pwsh。机器注销后任务会停；需要开机即跑可改用 -Autostart Service
#     （服务以 LocalSystem 运行，需要机器级安装 Python，见 docs/gitea-ci.md）。
#   - runner token 只在注册时用一次；注册成功后凭 .runner 文件里的凭据工作，别提交它。

param(
    [string]$Token = $env:GITEA_RUNNER_REGISTRATION_TOKEN,

    [string]$Instance = 'http://yuannas.local:3000',

    [string]$RunnerName = $env:COMPUTERNAME,

    [string]$Labels = 'windows-latest:host',

    [string]$RunnerDir = 'C:\gitea-runner',

    [string]$Version = '5.4.1',

    # gitea-runner-5.4.1-windows-amd64.exe 的 SHA-256（来自官方 checksums.txt）。
    # 换版本时必须同步改这里，或显式传 -ExpectedSha256 '' 跳过校验。
    [string]$ExpectedSha256 = '23cdf6f4e0c4d156bcb3dfed58b0416541dc31d8254a96910ec5cf7df543dde9',

    [ValidateSet('Task', 'Service', 'None')]
    [string]$Autostart = 'Task',

    [string]$TaskName = 'gitea-runner',

    # 覆盖已存在的 .runner（重新注册）。默认拒绝，避免误伤正在运行的 runner。
    [switch]$Force,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# 统一成绝对路径：后面生成的 config.yaml 里要写绝对 .runner 路径，
# 否则 runner 会把注册文件写到「当前工作目录」而不是 runner 目录。
$RunnerDir = [IO.Path]::GetFullPath($RunnerDir)

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Warning $Message
}

function Test-MinVersion([string]$Have, [int]$Major, [int]$Minor) {
    $parts = ($Have -split '[^0-9]+') | Where-Object { $_ -ne '' }
    if ($parts.Count -lt 2) { return $false }
    $hMajor = [int]$parts[0]
    $hMinor = [int]$parts[1]
    return ($hMajor -gt $Major) -or ($hMajor -eq $Major -and $hMinor -ge $Minor)
}

# ---------------------------------------------------------------- 前置检查

Write-Step '检查前置条件'

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    throw '找不到 git。builtin:checkout 需要 git >= 2.34.1。'
}
$gitVersion = ((& git --version) -replace '^git version\s+', '').Trim()
if (-not (Test-MinVersion $gitVersion 2 34)) {
    throw "git 版本过低：$gitVersion（需要 >= 2.34.1）。"
}
Write-Host "    git      : $gitVersion"

if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
    throw '找不到 PowerShell 7（pwsh）。workflow 的 shell 是 pwsh，必须先安装。'
}
Write-Host "    pwsh     : $((Get-Command pwsh).Source)"

$pythonOk = $false
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($ver in @('-3.13', '-3.12', '-3.11')) {
        & py $ver -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonOk = $true; Write-Host "    python   : py $ver"; break }
    }
}
if (-not $pythonOk) {
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonOk = $true; Write-Host "    python   : $($cmd.Source)"; break }
    }
}
if (-not $pythonOk) {
    Write-Warn '未检测到 Python >= 3.11。runner 能注册，但 CI 的 pytest job 会失败；请先安装 Python 3.11+ 并确保 py/python 在 PATH 上。'
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    throw '缺少 runner token。请从 Gitea 的 /admin/actions/runners 复制，并用环境变量 GITEA_RUNNER_REGISTRATION_TOKEN 传入（或显式 -Token）。'
}

$Instance = $Instance.TrimEnd('/')
Write-Host "    instance : $Instance"
Write-Host "    labels   : $Labels"
Write-Host "    runner   : $RunnerName"

# ---------------------------------------------------------------- 下载

$assetName = "gitea-runner-$Version-windows-amd64.exe"
$downloadUrl = "https://gitea.com/gitea/runner/releases/download/v$Version/$assetName"
$runnerExe = Join-Path $RunnerDir 'gitea-runner.exe'
$configPath = Join-Path $RunnerDir 'config.yaml'
$runnerFile = Join-Path $RunnerDir '.runner'

Write-Step "准备 runner 目录：$RunnerDir"
if (-not $DryRun -and -not (Test-Path -LiteralPath $RunnerDir)) {
    New-Item -ItemType Directory -Path $RunnerDir -Force | Out-Null
}

# runner 的 --config 指向不存在的文件会直接报错（LoadDefault 先 ReadFile）。
# 这里生成一个最小配置，并把 runner.file 固定成绝对路径，确保 .runner 落在
# runner 目录里，而不是调用者的当前目录。
Write-Step "准备最小配置文件：$configPath"
if ($DryRun) {
    Write-Host "    [dry-run] 若不存在则写入：$configPath（runner.file = $runnerFile）"
} elseif (-not (Test-Path -LiteralPath $configPath)) {
    $escapedRunnerFile = $runnerFile.Replace("'", "''")
    $minimalConfig = @"
# Minimal config for the self-hosted Gitea runner.
# Unset options keep their defaults. To see every option:
#   gitea-runner --config "$configPath" config generate
runner:
  file: '$escapedRunnerFile'
"@
    [IO.File]::WriteAllText($configPath, $minimalConfig, (New-Object Text.UTF8Encoding($false)))
    Write-Host "    已写入：$configPath"
} else {
    Write-Host '    config.yaml 已存在，保留原文件。'
}

Write-Step "下载 $assetName"
if ($DryRun) {
    Write-Host "    [dry-run] Invoke-WebRequest -Uri $downloadUrl -OutFile $runnerExe"
} elseif (-not (Test-Path -LiteralPath $runnerExe) -or $Force) {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $runnerExe -UseBasicParsing
    if (-not (Test-Path -LiteralPath $runnerExe)) { throw "下载失败：$downloadUrl" }
} else {
    Write-Host '    已存在，跳过下载（加 -Force 可强制重新下载）。'
}

if ($DryRun) {
    Write-Host "    [dry-run] 校验 SHA-256 = $ExpectedSha256"
} elseif (-not [string]::IsNullOrWhiteSpace($ExpectedSha256)) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $runnerExe).Hash.ToLowerInvariant()
    $expected = $ExpectedSha256.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "SHA-256 不匹配：实际 $actual，期望 $expected。下载可能被篡改或版本不匹配，已停止。"
    }
    Write-Host "    SHA-256 OK: $actual"
}

# ---------------------------------------------------------------- 注册

Write-Step '注册 runner'
if ((Test-Path -LiteralPath $runnerFile) -and -not $Force) {
    throw "已存在 $runnerFile（runner 已注册）。确认要重新注册请加 -Force（会覆盖本地注册信息）。"
}

$registerArgs = @(
    '--config', $configPath,
    'register',
    '--no-interactive',
    '--instance', $Instance,
    '--token', $Token,
    '--name', $RunnerName,
    '--labels', $Labels
)

if ($DryRun) {
    $masked = $registerArgs | ForEach-Object { if ($_ -eq $Token) { '***' } else { $_ } }
    Write-Host "    [dry-run] `"$runnerExe`" $($masked -join ' ')"
} else {
    Push-Location -LiteralPath $RunnerDir
    try {
        & $runnerExe @registerArgs
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) { throw "runner 注册失败（退出码 $exitCode）。" }
    if (-not (Test-Path -LiteralPath $runnerFile)) {
        throw "注册命令返回成功，但没有生成 $runnerFile，请检查 runner 日志。"
    }
    Write-Host "    已生成注册文件：$runnerFile"
}

# ---------------------------------------------------------------- 自启

if ($Autostart -eq 'None') {
    Write-Step '跳过自启配置'
    Write-Host "    手动运行：`"$runnerExe`" daemon --config `"$configPath`""
    return
}

$daemonArgs = "daemon --config `"$configPath`""

if ($Autostart -eq 'Task') {
    Write-Step "注册计划任务（登录时启动）：$TaskName"

    if ($DryRun) {
        Write-Host "    [dry-run] Register-ScheduledTask -TaskName $TaskName -Action (New-ScheduledTaskAction -Execute `"$runnerExe`" -Argument '$daemonArgs' -WorkingDirectory '$RunnerDir') -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Limited"
        return
    }

    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $runnerExe -Argument $daemonArgs -WorkingDirectory $RunnerDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "    已注册并启动计划任务：$TaskName（当前用户：$currentUser）"
    Write-Host "    查看状态：Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
    return
}

# Autostart = Service
Write-Step "注册 Windows 服务：$TaskName"

$serviceBinPath = '"{0}" {1}' -f $runnerExe, $daemonArgs
if ($DryRun) {
    Write-Host "    [dry-run] sc.exe create $TaskName binPath= $serviceBinPath start= auto"
    Write-Host "    [dry-run] sc.exe start $TaskName"
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw '注册 Windows 服务需要管理员权限。请用管理员终端重跑，或改用默认的 -Autostart Task。'
}

Write-Warn '服务默认以 LocalSystem 运行：它不会继承当前用户的 PATH 和用户级 Python。若 CI 找不到 Python/git，请改用默认的 -Autostart Task，或先在机器级安装这些工具。'

& sc.exe create $TaskName binPath= $serviceBinPath start= auto
if ($LASTEXITCODE -ne 0) { throw "sc.exe create 失败（退出码 $LASTEXITCODE）。若服务已存在，先执行：sc.exe delete $TaskName" }
& sc.exe start $TaskName
if ($LASTEXITCODE -ne 0) { throw "sc.exe start 失败（退出码 $LASTEXITCODE）。查看日志：$RunnerDir\gitea-runner.log" }
Write-Host "    服务已启动：$TaskName（日志：$RunnerDir\gitea-runner.log）"
