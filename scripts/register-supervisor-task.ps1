# 注册/更新「开机自启 supervisor」计划任务（无 cmd 黑框）。
#
# 为什么需要这个脚本：
#   旧任务的行动是 `cmd /c cd /d <repo> && .venv\Scripts\pythonw.exe -m dafeiyu.supervisor`。
#   pythonw 本身没有控制台，但外层 cmd.exe 是 console 程序，登录后会在桌面上留下一个
#   黑框，直到 supervisor 退出。这里把行动直接改成 pythonw.exe，并用 WorkingDirectory
#   指定仓库根目录，既不经过 cmd，也不需要 `cd`。
#
# 用法（必须管理员）：
#   powershell -ExecutionPolicy Bypass -File scripts\register-supervisor-task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register-supervisor-task.ps1 -DryRun
param(
    [string]$TaskName = "dafeiyu-supervisor",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonw)) {
    if (Test-Path -LiteralPath $python) {
        throw "找不到 $pythonw；不要退回 python.exe（会重新起黑框）。先重建 venv。"
    }
    throw "找不到虚拟环境：$pythonw"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$sid = $identity.User.Value

if (-not $DryRun -and -not $isAdmin) {
    throw "需要管理员权限：任务文件在 C:\Windows\System32\Tasks 下，普通用户只有读权限。`n" +
          "请右键「以管理员身份运行」PowerShell，再执行：`n" +
          "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
}

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m dafeiyu.supervisor" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
$runAs = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Hidden

Write-Host "任务名     : $TaskName" -ForegroundColor Cyan
Write-Host "执行       : $pythonw" -ForegroundColor Cyan
Write-Host "参数       : -m dafeiyu.supervisor" -ForegroundColor Cyan
Write-Host "工作目录   : $root" -ForegroundColor Cyan
Write-Host "触发       : 登录时（用户 SID $sid，交互会话）" -ForegroundColor Cyan

if ($DryRun) {
    Write-Host "[DRYRUN] 未注册任务。" -ForegroundColor Yellow
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $runAs -Settings $settings -Force | Out-Null

$check = Get-ScheduledTask -TaskName $TaskName
Write-Host "已注册：$($check.TaskName) / $($check.State)" -ForegroundColor Green
Write-Host "行动：$($check.Actions[0].Execute) $($check.Actions[0].Arguments)" -ForegroundColor Green
