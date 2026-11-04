# 启动/停止微信非 UIA 出站 hook 服务（scripts/wechat_hook_server.py）。
#
# 必须脱离当前终端启动：SSH / PowerShell 会话结束时子进程会被一起收走，
# 这里用 Win32_Process.Create 让 python 进程挂在系统上。
param(
    [string]$HelperRoot = "C:\MyFile\AICoding\helper",
    [int]$Port = 30011,
    [string]$Account = "",
    [string]$Token = "",
    [switch]$Stop,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $HelperRoot ".venv\Scripts\python.exe"
# 后台启动优先用 pythonw：python.exe 是 console 子系统，新建进程会多出一个黑框。
# -Foreground 仍然用 python.exe（日志直接打在终端上）。
$pythonw = Join-Path $HelperRoot ".venv\Scripts\pythonw.exe"
$script = Join-Path $root "scripts\wechat_hook_server.py"
$logDir = Join-Path $root "logs"
$logFile = Join-Path $logDir "wechat-hook.log"

function Get-HookProcess {
    # 兼容历史实例：早先是 python.exe，现在是 pythonw.exe。
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
            $_.CommandLine -like "*wechat_hook_server.py*"
        }
}

if ($Stop) {
    $procs = Get-HookProcess
    if (-not $procs) { Write-Host "hook 服务未在运行"; exit 0 }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "已停止 pid=$($p.ProcessId)"
    }
    exit 0
}

if (-not (Test-Path $python)) { throw "找不到 helper 的 Python：$python（先跑 pip install -r $HelperRoot\requirements.txt）" }
if (-not (Test-Path $script)) { throw "找不到 $script" }
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$existing = Get-HookProcess
if ($existing) {
    Write-Host "hook 服务已在运行：pid=$($existing.ProcessId | Select-Object -First 1)"
} else {
    $cliArgs = @("`"$script`"", "--helper-root", "`"$HelperRoot`"", "--port", "$Port",
                 "--log-file", "`"$logFile`"")
    if ($Account) { $cliArgs += @("--account", "`"$Account`"") }
    if ($Token) { $cliArgs += @("--token", "`"$Token`"") }
    $launcher = $python
    if (-not $Foreground -and (Test-Path $pythonw)) { $launcher = $pythonw }
    $commandLine = "`"$launcher`" " + ($cliArgs -join " ")

    if ($Foreground) {
        & $python $script --helper-root $HelperRoot --port $Port --log-file $logFile
        exit $LASTEXITCODE
    }
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $commandLine
        CurrentDirectory = (Split-Path -Parent $script)
    }
    if ($created.ReturnValue -ne 0) { throw "启动失败：ReturnValue=$($created.ReturnValue)" }
    Write-Host "已启动 pid=$($created.ProcessId)，日志 $logFile"
}

$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($health.ok) {
            Write-Host "就绪：wxid=$($health.self_wxid) 微信版本=$($health.wechat_version)"
            exit 0
        }
        Write-Host "等待微信登录/桥接就绪：$($health.last_error)"
    } catch {
        Write-Host "等待服务监听 127.0.0.1:$Port ..."
    }
    Start-Sleep -Seconds 3
}
Write-Warning "60 秒内未就绪，看 $logFile"
exit 1
