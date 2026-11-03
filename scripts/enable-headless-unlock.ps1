<#
.SYNOPSIS
    无人值守防锁屏安装器（需管理员）：让微信桥在没人看着的时候也能发消息。

.DESCRIPTION
    微信桥的出站走 UIA，必须在**未锁定的交互桌面**上执行。Windows 有四种情况会把桌面锁掉，
    导致 bot「能收消息却发不出去」：

      1. 合盖 / 空闲进入 S0 连接待机 —— 进待机即锁会话（本机 12:06:28 Kernel-Power 506 就是这个）
      2. 联想 SmartSense 人体存在检测（HPD）—— 人走锁屏
      3. RDP 连接断开 —— 系统行为，必定锁会话
      4. 唤醒后要求重新登录（CONSOLELOCK）

    本脚本一次处理这四件事：

      * 合盖（交流电）= 不采取任何操作；交流电永不休眠/待机；唤醒不要求密码
      * 关闭联想 HPD 的「人走锁屏」（Status=0）并重启 SmartSense 让设置生效
      * 注册 SYSTEM 计划任务：RDP 断开（事件 24）后延迟若干秒，把承载微信的会话
        交回物理控制台（tscon），桌面保持解锁

    改动前的原值会备份到 data\headless-unlock-backup.json，随时可用 -Rollback 还原。

    电池供电时**不**改动合盖行为（合盖仍睡眠），这是有意的安全选择：笔记本合盖塞包里
    继续跑会过热。请保持机器插电使用。

.PARAMETER Rollback
    删除计划任务，并把电源 / 联想 HPD / 组策略设置还原成安装前的值。

.PARAMETER DryRun
    只打印将要执行的改动，不写任何东西，也不需要管理员权限。

.PARAMETER NoElevate
    非管理员时不尝试自动提权，直接报错退出（给已经有管理员窗口的场景用）。

.PARAMETER DisableLockWorkstation
    额外禁止 Win+L 手动锁屏（HKLM 组策略 DisableLockWorkstation=1）。默认关闭。

.PARAMETER PowerButtonNoAction
    额外把电源按钮改成「不采取任何操作」。默认关闭。
    本机当前电源按钮 = 关机（3），误按会直接关掉 bot，建议开启。

.PARAMETER TaskName
    计划任务名，默认 DafeiyuRdpDetachToConsole。

.PARAMETER DisconnectDelaySeconds
    RDP 断开后等多少秒再交接会话，默认 20 秒（避免网络抖动时来回切换把人踢下线）。

.PARAMETER EmitTaskXml
    只把生成的任务 XML 写到指定路径（UTF-16）然后退出，方便排查 schtasks 注册失败。
    不修改任何系统设置。

.EXAMPLE
    # 先看看会改什么（不需要管理员）
    powershell -ExecutionPolicy Bypass -File scripts\enable-headless-unlock.ps1 -DryRun

.EXAMPLE
    # 正式安装（会弹 UAC，点「是」）
    powershell -ExecutionPolicy Bypass -File scripts\enable-headless-unlock.ps1 -PowerButtonNoAction

.EXAMPLE
    # 全部还原
    powershell -ExecutionPolicy Bypass -File scripts\enable-headless-unlock.ps1 -Rollback
#>
[CmdletBinding()]
param(
    [switch]$Rollback,
    [switch]$DryRun,
    [switch]$NoElevate,
    [switch]$DisableLockWorkstation,
    [switch]$PowerButtonNoAction,
    [string]$TaskName = 'DafeiyuRdpDetachToConsole',
    [int]$DisconnectDelaySeconds = 20,
    [string]$EmitTaskXml = ''
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$workerScript = Join-Path $PSScriptRoot 'rdp-detach-to-console.ps1'
$logDir = Join-Path $repoRoot 'logs'
$transcriptPath = Join-Path $logDir 'enable-headless-unlock.log'
$backupPath = Join-Path $repoRoot 'data\headless-unlock-backup.json'

# 电源子组 / 设置 GUID（用 GUID 而不是别名，兼容中英文系统）
$SubButtons = '4F971E89-EEBD-4455-A8DE-9E59040E7347'
$SubSleep = '238C9FA8-0AAD-41ED-83F4-97BE242C8F20'
$LidAction = '96996BC0-AD50-47EC-923B-6F41874DD9EB'
$PowerButtonAction = '7648EFA3-DD9C-4E3E-B566-50F929386280'
$StandbyIdle = '29F6C1DB-86DA-48C5-9FDB-F2B67B1F44DA'
$HibernateIdle = '9D7815A6-7EE4-497E-8888-515A05F02364'

$SmartSenseService = 'SmartSense'
$HpdKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\SmartSense\Parameters\HPD\AWARE'
$LockPolicyKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "== $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "   [OK]  $Message" -ForegroundColor Green
}

function Write-Skip2 {
    param([string]$Message)
    Write-Host "   [跳过] $Message" -ForegroundColor DarkGray
}

function Write-Warn2 {
    param([string]$Message)
    Write-Host "   [注意] $Message" -ForegroundColor Yellow
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# 原生命令（powercfg / schtasks）往 stderr 写内容时，$ErrorActionPreference='Stop' 会把它
# 变成 NativeCommandError 终止整个脚本。统一用这个包装：只关心退出码和输出。
function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @ArgumentList 2>&1
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        return [pscustomobject]@{
            ExitCode = $code
            Output   = (($output | Out-String).Trim())
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# ---------------------------------------------------------------------------
# 提权：非管理员时用 UAC 重新拉起自己，等它跑完再把日志尾巴打出来
# ---------------------------------------------------------------------------
if (-not (Test-Admin) -and -not $DryRun -and -not $EmitTaskXml) {
    if ($NoElevate) {
        Write-Host '需要管理员权限：请以管理员身份重新运行本脚本。' -ForegroundColor Red
        exit 1
    }

    $relaunchArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Rollback) { $relaunchArgs += '-Rollback' }
    if ($DisableLockWorkstation) { $relaunchArgs += '-DisableLockWorkstation' }
    if ($PowerButtonNoAction) { $relaunchArgs += '-PowerButtonNoAction' }
    $relaunchArgs += @('-TaskName', "`"$TaskName`"", '-DisconnectDelaySeconds', "$DisconnectDelaySeconds", '-NoElevate')

    Write-Host '需要管理员权限，正在弹出 UAC，请在弹窗里点「是」……' -ForegroundColor Yellow
    try {
        $child = Start-Process -FilePath (Get-Process -Id $PID).Path `
            -ArgumentList $relaunchArgs -Verb RunAs -Wait -PassThru -WorkingDirectory $repoRoot
    } catch {
        Write-Host "提权失败（可能取消了 UAC）：$($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }

    Write-Host ''
    Write-Host "提权进程已结束（exit=$($child.ExitCode)）。完整日志：$transcriptPath" -ForegroundColor Cyan
    if (Test-Path -LiteralPath $transcriptPath) {
        Write-Host '---------------- 日志末尾 ----------------' -ForegroundColor DarkGray
        Get-Content -LiteralPath $transcriptPath -Tail 60 | ForEach-Object { Write-Host $_ }
    }
    exit $child.ExitCode
}

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$transcriptStarted = $false
if (-not $DryRun) {
    try {
        Start-Transcript -Path $transcriptPath -Append -ErrorAction Stop | Out-Null
        $transcriptStarted = $true
    } catch {
        $transcriptStarted = $false
    }
}

$exitCode = 0
try {
    if (-not (Test-Path -LiteralPath $workerScript)) {
        throw "找不到工作脚本：$workerScript（缺少它就无法注册计划任务）"
    }

    $schemeRoot = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes'
    $activeScheme = (Get-ItemProperty -Path $schemeRoot).ActivePowerScheme
    if (-not $activeScheme) { throw '读不到当前电源方案 GUID' }
    $schemeKey = Join-Path $schemeRoot $activeScheme

    function Get-PowerIndexAt {
        param(
            [Parameter(Mandatory)][string]$Sub,
            [Parameter(Mandatory)][string]$Setting,
            [ValidateSet('AC', 'DC')][string]$Kind = 'AC'
        )
        $key = Join-Path (Join-Path $schemeKey $Sub) $Setting
        $name = if ($Kind -eq 'AC') { 'ACSettingIndex' } else { 'DCSettingIndex' }
        try {
            return (Get-ItemProperty -Path $key -Name $name -ErrorAction Stop).$name
        } catch {
            return $null
        }
    }

    function Set-PowerIndexAt {
        param(
            [Parameter(Mandatory)][string]$Sub,
            [Parameter(Mandatory)][string]$Setting,
            [Parameter(Mandatory)][int]$Value,
            [ValidateSet('AC', 'DC')][string]$Kind = 'AC'
        )
        $flag = if ($Kind -eq 'AC') { '/setacvalueindex' } else { '/setdcvalueindex' }
        if ($DryRun) {
            Write-Host "   [DRYRUN] powercfg $flag SCHEME_CURRENT $Sub $Setting $Value"
            return $true
        }
        $result = Invoke-NativeCommand -FilePath 'powercfg.exe' -ArgumentList @($flag, 'SCHEME_CURRENT', $Sub, $Setting, "$Value")
        if ($result.ExitCode -ne 0) {
            Write-Warn2 ("powercfg {0} {1} 失败：{2}" -f $flag, $Setting, $result.Output)
            return $false
        }
        return $true
    }

    function Get-TaskExists {
        param([string]$Name)
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($null -ne $task) { return $true }
        $result = Invoke-NativeCommand -FilePath 'schtasks.exe' -ArgumentList @('/query', '/tn', $Name)
        return ($result.ExitCode -eq 0)
    }

    function Test-EventQuery {
        # 校验触发器 XPath 是否合法；「没有事件」也算 XPath 正确。
        param([string]$Channel, [string]$XPath)
        try {
            Get-WinEvent -LogName $Channel -FilterXPath $XPath -MaxEvents 1 -ErrorAction Stop | Out-Null
            return $true
        } catch {
            $message = $_.Exception.Message
            if ($message -match 'No events|没有|未找到') { return $true }
            Write-Warn2 "事件查询校验失败：$message"
            return $false
        }
    }

    function New-DetachTaskXml {
        param([int]$DelaySeconds)

        $channel = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'
        $select = '*[System[(EventID=24)]]'
        $subscription = '<QueryList><Query Id="0" Path="{0}"><Select Path="{0}">{1}</Select></Query></QueryList>' -f `
            $channel, [System.Security.SecurityElement]::Escape($select)
        $command = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $taskArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -DelaySeconds 0' -f $workerScript

        $template = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>dafeiyu-bot</Author>
    <Description>RDP 断开后把承载微信的会话交回物理控制台，保持桌面解锁（scripts\enable-headless-unlock.ps1 安装）。</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>__SUBSCRIPTION__</Subscription>
      <Delay>PT__DELAY__S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>__COMMAND__</Command>
      <Arguments>__ARGUMENTS__</Arguments>
    </Exec>
  </Actions>
</Task>
'@

        return $template.Replace('__SUBSCRIPTION__', [System.Security.SecurityElement]::Escape($subscription)).
            Replace('__DELAY__', "$DelaySeconds").
            Replace('__COMMAND__', [System.Security.SecurityElement]::Escape($command)).
            Replace('__ARGUMENTS__', [System.Security.SecurityElement]::Escape($taskArgs))
    }

    # =======================================================================
    # 只导出任务 XML（排查用）
    # =======================================================================
    if ($EmitTaskXml) {
        $xmlText = New-DetachTaskXml -DelaySeconds $DisconnectDelaySeconds
        $null = [xml]$xmlText
        $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EmitTaskXml)
        [System.IO.File]::WriteAllText($target, $xmlText, (New-Object System.Text.UnicodeEncoding($false, $true)))
        Write-Ok "任务 XML 已写出：$target"
        return
    }

    # =======================================================================
    # 回滚
    # =======================================================================
    if ($Rollback) {
        Write-Step '回滚：还原设置并删除计划任务'

        $backup = $null
        if (Test-Path -LiteralPath $backupPath) {
            try {
                $backup = Get-Content -LiteralPath $backupPath -Raw -Encoding UTF8 | ConvertFrom-Json
                Write-Ok "读到备份：$backupPath（$($backup.created_at)）"
            } catch {
                Write-Warn2 "备份文件损坏，按默认值还原：$($_.Exception.Message)"
            }
        } else {
            Write-Warn2 "没有备份文件（$backupPath），按 Windows 默认值还原"
        }

        if (Get-TaskExists -Name $TaskName) {
            if ($DryRun) {
                Write-Host "   [DRYRUN] schtasks /delete /tn $TaskName /f"
            } else {
                $deleteResult = Invoke-NativeCommand -FilePath 'schtasks.exe' -ArgumentList @('/delete', '/tn', $TaskName, '/f')
                if ($deleteResult.ExitCode -eq 0) {
                    Write-Ok "计划任务已删除：$TaskName"
                } else {
                    Write-Warn2 "删除计划任务失败（exit=$($deleteResult.ExitCode)）：$($deleteResult.Output)"
                }
            }
        } else {
            Write-Skip2 "计划任务不存在：$TaskName"
        }

        # 合盖：默认是睡眠（1）
        $lidAc = if ($null -ne $backup -and $null -ne $backup.lid_action_ac) { [int]$backup.lid_action_ac } else { 1 }
        if (Set-PowerIndexAt -Sub $SubButtons -Setting $LidAction -Value $lidAc -Kind AC) {
            Write-Ok "合盖（交流电）还原为 $lidAc（1=睡眠）"
        }

        $standbyAc = if ($null -ne $backup -and $null -ne $backup.standby_idle_ac) { [int]$backup.standby_idle_ac } else { 0 }
        if (Set-PowerIndexAt -Sub $SubSleep -Setting $StandbyIdle -Value $standbyAc -Kind AC) {
            Write-Ok "睡眠超时（交流电）还原为 $standbyAc 秒"
        }

        $hibernateAc = if ($null -ne $backup -and $null -ne $backup.hibernate_idle_ac) { [int]$backup.hibernate_idle_ac } else { 0 }
        if (Set-PowerIndexAt -Sub $SubSleep -Setting $HibernateIdle -Value $hibernateAc -Kind AC) {
            Write-Ok "休眠超时（交流电）还原为 $hibernateAc 秒"
        }

        # 唤醒后要求密码：默认 1
        $null = Set-PowerIndexAt -Sub 'SUB_NONE' -Setting 'CONSOLELOCK' -Value 1 -Kind AC
        $null = Set-PowerIndexAt -Sub 'SUB_NONE' -Setting 'CONSOLELOCK' -Value 1 -Kind DC
        Write-Ok '唤醒后要求密码：还原为「是」'

        if ($null -ne $backup -and $null -ne $backup.power_button_ac) {
            $pbAc = [int]$backup.power_button_ac
            $pbDc = if ($null -ne $backup.power_button_dc) { [int]$backup.power_button_dc } else { $pbAc }
            $null = Set-PowerIndexAt -Sub $SubButtons -Setting $PowerButtonAction -Value $pbAc -Kind AC
            $null = Set-PowerIndexAt -Sub $SubButtons -Setting $PowerButtonAction -Value $pbDc -Kind DC
            Write-Ok "电源按钮还原为 AC=$pbAc DC=$pbDc（3=关机）"
        } else {
            Write-Skip2 '电源按钮未改动过，跳过'
        }

        if ($null -ne $backup -and $backup.disable_lock_workstation_applied) {
            if ($DryRun) {
                Write-Host "   [DRYRUN] 删除 $LockPolicyKey\DisableLockWorkstation"
            } else {
                Remove-ItemProperty -Path $LockPolicyKey -Name 'DisableLockWorkstation' -ErrorAction SilentlyContinue
                Write-Ok '已移除 Win+L 屏蔽'
            }
        }

        if ($null -ne $backup -and $null -ne $backup.hpd_status) {
            if ($DryRun) {
                Write-Host "   [DRYRUN] 还原 HPD Status=$($backup.hpd_status) 并重启 $SmartSenseService"
            } else {
                Set-ItemProperty -Path $HpdKey -Name 'Status' -Value ([int]$backup.hpd_status) -Type DWord
                Write-Ok "联想 HPD Status 还原为 $($backup.hpd_status)（1=人走锁屏开启）"
                $svc = Get-Service -Name $SmartSenseService -ErrorAction SilentlyContinue
                if ($svc) {
                    try {
                        Restart-Service -Name $SmartSenseService -Force
                        Write-Ok "$SmartSenseService 已重启"
                    } catch {
                        Write-Warn2 "重启 $SmartSenseService 失败：$($_.Exception.Message)"
                    }
                }
            }
        }

        if (-not $DryRun) {
            $null = Invoke-NativeCommand -FilePath 'powercfg.exe' -ArgumentList @('/setactive', 'SCHEME_CURRENT')
        }
        Write-Step '回滚完成'
        Write-Host '   合盖/休眠/唤醒密码已还原；若之前开着 HPD，人走锁屏也回来了。' -ForegroundColor DarkGray
        return
    }

    # =======================================================================
    # 安装
    # =======================================================================
    Write-Step '检查前置条件'
    Write-Ok "当前电源方案：$activeScheme"
    if (-not (Test-Admin) -and -not $DryRun) {
        throw '需要管理员权限'
    }
    if ($DryRun) {
        Write-Warn2 'DRYRUN 模式：只打印，不修改任何设置'
    }

    $lidBefore = Get-PowerIndexAt -Sub $SubButtons -Setting $LidAction -Kind AC
    $standbyBefore = Get-PowerIndexAt -Sub $SubSleep -Setting $StandbyIdle -Kind AC
    $hibernateBefore = Get-PowerIndexAt -Sub $SubSleep -Setting $HibernateIdle -Kind AC
    $powerButtonAcBefore = Get-PowerIndexAt -Sub $SubButtons -Setting $PowerButtonAction -Kind AC
    $powerButtonDcBefore = Get-PowerIndexAt -Sub $SubButtons -Setting $PowerButtonAction -Kind DC
    $hpdBefore = $null
    try { $hpdBefore = (Get-ItemProperty -Path $HpdKey -Name 'Status' -ErrorAction Stop).Status } catch { }
    $taskExistedBefore = Get-TaskExists -Name $TaskName

    if (-not (Test-Path -LiteralPath $backupPath)) {
        $backupDir = Split-Path -Parent $backupPath
        if (-not (Test-Path -LiteralPath $backupDir)) {
            New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
        }
        $backup = [ordered]@{
            created_at                    = (Get-Date).ToString('o')
            active_scheme                 = $activeScheme
            lid_action_ac                 = $lidBefore
            standby_idle_ac               = $standbyBefore
            hibernate_idle_ac             = $hibernateBefore
            power_button_ac               = $powerButtonAcBefore
            power_button_dc               = $powerButtonDcBefore
            hpd_status                    = $hpdBefore
            disable_lock_workstation_applied = $false
            task_existed                  = $taskExistedBefore
        }
        if ($DryRun) {
            Write-Host "   [DRYRUN] 会写备份：$backupPath"
        } else {
            ($backup | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $backupPath -Encoding UTF8
            Write-Ok "已备份原值：$backupPath"
        }
    } else {
        Write-Skip2 "备份已存在，保留最早的原始值：$backupPath"
    }

    Write-Step '1/5 合盖不睡眠（仅交流电；电池仍睡眠，避免放包里过热）'
    if (Set-PowerIndexAt -Sub $SubButtons -Setting $LidAction -Value 0 -Kind AC) {
        Write-Ok '合盖（交流电）= 不采取任何操作'
    }

    Write-Step '2/5 交流电永不休眠 / 不待机'
    $null = Set-PowerIndexAt -Sub $SubSleep -Setting $StandbyIdle -Value 0 -Kind AC
    $null = Set-PowerIndexAt -Sub $SubSleep -Setting $HibernateIdle -Value 0 -Kind AC
    Write-Ok '睡眠超时 = 0（永不）、休眠超时 = 0（永不）'
    Write-Warn2 '电池供电时仍按原设置睡眠；本机请保持插电'

    Write-Step '3/5 唤醒后不要求重新登录'
    $consoleAcOk = Set-PowerIndexAt -Sub 'SUB_NONE' -Setting 'CONSOLELOCK' -Value 0 -Kind AC
    $consoleDcOk = Set-PowerIndexAt -Sub 'SUB_NONE' -Setting 'CONSOLELOCK' -Value 0 -Kind DC
    if ($consoleAcOk -and $consoleDcOk) {
        Write-Ok '唤醒后要求密码 = 否'
    } else {
        Write-Warn2 '这台机器可能不支持该设置（Modern Standby 常见），可忽略：真正的锁屏来源是待机/HPD/断线'
    }

    if ($PowerButtonNoAction) {
        Write-Step '3.5 电源按钮 = 不采取任何操作'
        $null = Set-PowerIndexAt -Sub $SubButtons -Setting $PowerButtonAction -Value 0 -Kind AC
        $null = Set-PowerIndexAt -Sub $SubButtons -Setting $PowerButtonAction -Value 0 -Kind DC
        Write-Ok "电源按钮已从 $powerButtonAcBefore（3=关机）改为 0（不采取任何操作）"
    }

    if (-not $DryRun) {
        $null = Invoke-NativeCommand -FilePath 'powercfg.exe' -ArgumentList @('/setactive', 'SCHEME_CURRENT')
    }

    Write-Step '4/5 关闭联想「人走锁屏」（SmartSense HPD）'
    $smartSense = Get-Service -Name $SmartSenseService -ErrorAction SilentlyContinue
    if (-not $smartSense) {
        Write-Skip2 "未安装 $SmartSenseService，跳过"
    } elseif (-not (Test-Path -LiteralPath $HpdKey)) {
        Write-Skip2 "没有 HPD 注册表项：$HpdKey，跳过"
    } else {
        Write-Host "   当前 HPD Status = $hpdBefore（1=开启人走锁屏）"
        if ($DryRun) {
            Write-Host "   [DRYRUN] Set-ItemProperty '$HpdKey' Status=0; Restart-Service $SmartSenseService"
        } else {
            Set-ItemProperty -Path $HpdKey -Name 'Status' -Value 0 -Type DWord
            Write-Ok 'HPD Status = 0'
            try {
                Restart-Service -Name $SmartSenseService -Force
                Write-Ok "$SmartSenseService 已重启，新设置生效"
            } catch {
                Write-Warn2 "重启 $SmartSenseService 失败：$($_.Exception.Message)"
            }
            Start-Sleep -Seconds 2
            $hpdAfter = (Get-ItemProperty -Path $HpdKey -Name 'Status').Status
            if ($hpdAfter -eq 0) {
                Write-Ok '复查 HPD Status 仍为 0'
            } else {
                Write-Warn2 "HPD Status 被改回 $hpdAfter（固件/联想软件覆盖）。兜底：sc config $SmartSenseService start= disabled"
            }
        }
    }

    if ($DisableLockWorkstation) {
        Write-Step '4.5 禁止 Win+L 手动锁屏'
        if ($DryRun) {
            Write-Host "   [DRYRUN] Set-ItemProperty '$LockPolicyKey' DisableLockWorkstation=1"
        } else {
            if (-not (Test-Path -LiteralPath $LockPolicyKey)) {
                New-Item -Path $LockPolicyKey -Force | Out-Null
            }
            Set-ItemProperty -Path $LockPolicyKey -Name 'DisableLockWorkstation' -Value 1 -Type DWord
            Write-Ok 'DisableLockWorkstation = 1'
            if (Test-Path -LiteralPath $backupPath) {
                $onDisk = Get-Content -LiteralPath $backupPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $onDisk.disable_lock_workstation_applied = $true
                ($onDisk | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $backupPath -Encoding UTF8
            }
        }
    }

    Write-Step "5/5 注册计划任务：RDP 断开后自动把会话交回控制台（$TaskName）"
    $channel = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'
    $xpath = '*[System[(EventID=24)]]'
    if (-not (Test-EventQuery -Channel $channel -XPath $xpath)) {
        Write-Warn2 '事件查询校验没通过，仍会尝试注册（失败会报错）'
    } else {
        Write-Ok "事件订阅有效：$channel / EventID=24"
    }

    $xml = New-DetachTaskXml -DelaySeconds $DisconnectDelaySeconds
    try {
        $null = [xml]$xml
        Write-Ok '任务 XML 格式合法'
    } catch {
        throw "生成的任务 XML 不合法：$($_.Exception.Message)"
    }

    if ($DryRun) {
        Write-Host "   [DRYRUN] schtasks /create /tn $TaskName /xml <临时文件> /f"
        Write-Host "   触发后执行：powershell -File `"$workerScript`" -DelaySeconds 0"
        Write-Host "   运行身份：SYSTEM（S-1-5-18），延迟 ${DisconnectDelaySeconds}s"
    } else {
        $tempXml = Join-Path $env:TEMP ("dafeiyu-detach-{0}.xml" -f ([guid]::NewGuid().ToString('N')))
        $utf16 = New-Object System.Text.UnicodeEncoding($false, $true)
        [System.IO.File]::WriteAllText($tempXml, $xml, $utf16)
        try {
            $createResult = Invoke-NativeCommand -FilePath 'schtasks.exe' -ArgumentList @('/create', '/tn', $TaskName, '/xml', $tempXml, '/f')
            if ($createResult.ExitCode -ne 0) {
                throw ("schtasks 注册失败（exit={0}）：{1}" -f $createResult.ExitCode, $createResult.Output)
            }
            Write-Ok "计划任务已注册：$TaskName"
        } finally {
            Remove-Item -LiteralPath $tempXml -Force -ErrorAction SilentlyContinue
        }

        if (Get-TaskExists -Name $TaskName) {
            Write-Ok '复查：任务存在'
        } else {
            throw '任务注册后复查不到，安装失败'
        }

        $registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($registered) {
            Write-Ok ("运行身份：{0}；状态：{1}" -f $registered.Principal.UserId, $registered.State)
        }
    }

    if ($DryRun) {
        Write-Step 'DRYRUN 结束'
        Write-Host '   以上全部是「将会做」的改动，系统没有被修改。去掉 -DryRun 即可正式安装。' -ForegroundColor Yellow
        return
    }

    Write-Step '安装完成'
    Write-Host '   已生效：合盖不睡眠（交流电）、不休眠/待机、唤醒免登录、联想人走锁屏关闭、RDP 断开自动 tscon。' -ForegroundColor Green
    Write-Host ''
    Write-Host '   接下来：' -ForegroundColor Cyan
    Write-Host '   1. 断开 RDP（直接关闭窗口即可），20 秒后会看到 logs\rdp-detach.log 出现「已交回 console」'
    Write-Host '   2. 之后桌面保持解锁，微信桥的出站 UIA 恢复正常；待发队列里的消息会在大约 15 秒内补发'
    Write-Host '   3. 需要全部还原：scripts\enable-headless-unlock.ps1 -Rollback'
    Write-Host ''
    Write-Host '   代价（必须知道）：机器会长期保持解锁，任何能碰到键盘的人都能直接操作微信/QQ；' -ForegroundColor Yellow
    Write-Host '   笔记本合盖后仍在运行，请务必插电并放在通风处。' -ForegroundColor Yellow
}
catch {
    Write-Host ''
    Write-Host "失败：$($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
finally {
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}

exit $exitCode
