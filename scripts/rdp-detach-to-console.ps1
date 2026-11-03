<#
SYSTEM 计划任务调用的工作脚本：RDP 断开后，把“承载微信的那个会话”交回物理控制台。

为什么需要它
------------
微信桥的出站走 UIA，必须在**未锁定的交互桌面**上执行。Windows 在 RDP 断开时
会锁定会话，于是 bot 能收消息却发不出去。`tscon <id> /dest:console` 把会话
重新挂到控制台，桌面保持解锁——这正是“远程操作、机器无人值守”场景的标准做法。

安全设计
--------
1. 默认先等 20 秒再动手：网络抖动导致的短暂断线，如果用户 20 秒内重连，会话状态
   会变成 Active，本脚本直接跳过，不会把人踢下线。
2. 只处理“确实处于断开状态、且进程列表里有微信”的会话；不动 services / 监听会话。
3. 任何一步失败都只写日志，不抛异常（计划任务里抛异常没有意义）。

为什么不用 qwinsta 的 STATE 列
-----------------------------
`qwinsta` 的状态字符串是本地化的：交互 shell 里是 `Disc`，SYSTEM 计划任务里
在本机返回中文「断开」。曾经因此把「已断开」误判成「无需处理」，任务跑了却什么都没做。
现在改用 WTS API 的 WTSConnectState（数字，0=Active / 4=Disconnected），与语言无关；
只有在 WTS API 不可用时才回落到 qwinsta 文本匹配（同时接受中英文）。

手动调试：`powershell -File scripts\rdp-detach-to-console.ps1 -DryRun -DelaySeconds 0`
#>
[CmdletBinding()]
param(
    [int]$DelaySeconds = 20,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot 'logs'
$logPath = Join-Path $logDir 'rdp-detach.log'

# WTS_INFO_CLASS.WTSConnectState
$WTS_CONNECT_STATE_CLASS = 8
$STATE_NAMES = @{
    0 = 'Active'
    1 = 'Connected'
    2 = 'ConnectQuery'
    3 = 'Shadow'
    4 = 'Disconnected'
    5 = 'Idle'
    6 = 'Listen'
    7 = 'Reset'
    8 = 'Down'
    9 = 'Init'
}

function Write-Log {
    param([string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    try {
        if (-not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        }
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    } catch {
        # 日志失败不能影响主逻辑
    }
}

function Initialize-WtsApi {
    if ('Dafeiyu.Wts' -as [type]) { return $true }
    try {
        Add-Type -ErrorAction Stop -Namespace Dafeiyu -Name Wts -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("wtsapi32.dll", SetLastError = true)]
public static extern bool WTSQuerySessionInformationW(System.IntPtr hServer, int sessionId, int wtsInfoClass, out System.IntPtr ppBuffer, out int pBytesReturned);

[System.Runtime.InteropServices.DllImport("wtsapi32.dll")]
public static extern void WTSFreeMemory(System.IntPtr pMemory);
'@
        return $true
    } catch {
        Write-Log "WTS API 初始化失败，回落到 qwinsta 文本：$($_.Exception.Message)"
        return $false
    }
}

function Get-SessionConnectStateCode {
    param([int]$SessionId)
    $buffer = [IntPtr]::Zero
    $bytes = 0
    $ok = [Dafeiyu.Wts]::WTSQuerySessionInformationW(
        [IntPtr]::Zero, $SessionId, $script:WTS_CONNECT_STATE_CLASS, [ref]$buffer, [ref]$bytes)
    if (-not $ok) { return $null }
    try {
        return [int][System.Runtime.InteropServices.Marshal]::ReadInt32($buffer)
    } finally {
        [Dafeiyu.Wts]::WTSFreeMemory($buffer)
    }
}

function Get-SessionStateText {
    # 回落方案：qwinsta 的 STATE 列，同时接受英文与中文（其它语言请自行补充）。
    param([int]$SessionId)
    $rows = @(& qwinsta.exe 2>$null)
    foreach ($row in $rows) {
        if ($row -match "^\s*[> ]?\S+\s+\S*\s+$SessionId\s+(\S+)") {
            return $Matches[1]
        }
    }
    return ''
}

function Get-WeixinSessionIds {
    $ids = @()
    $procs = Get-Process -Name Weixin -ErrorAction SilentlyContinue
    if ($procs) {
        $ids = @($procs | Select-Object -ExpandProperty SessionId -Unique)
    }
    return @($ids | Where-Object { $_ -gt 0 } | ForEach-Object { [int]$_ })
}

if ($DelaySeconds -gt 0) {
    Start-Sleep -Seconds $DelaySeconds
}

$targets = Get-WeixinSessionIds
if (-not $targets) {
    Write-Log '没有找到微信进程，跳过'
    exit 0
}

$hasWts = Initialize-WtsApi
$disconnectedTokens = @('Disconnected', 'Disc', '断开', '已断开')
$acted = $false

foreach ($sid in $targets) {
    $stateName = ''
    if ($hasWts) {
        $code = Get-SessionConnectStateCode -SessionId $sid
        if ($null -ne $code) {
            $stateName = if ($STATE_NAMES.ContainsKey($code)) { $STATE_NAMES[$code] } else { "Code$code" }
        }
    }
    if (-not $stateName) {
        $stateName = Get-SessionStateText -SessionId $sid
    }

    if ($disconnectedTokens -notcontains $stateName) {
        Write-Log "会话 $sid 状态=$stateName，无需处理"
        continue
    }

    if ($DryRun) {
        Write-Log "DRYRUN 会话 $sid 处于 $stateName，本应执行 tscon $sid /dest:console"
        continue
    }

    try {
        $output = & "$env:SystemRoot\System32\tscon.exe" $sid /dest:console 2>&1
        foreach ($line in @($output)) {
            if ($line) { Write-Log "tscon($sid): $line" }
        }
        if ($LASTEXITCODE -eq 0) {
            Write-Log "会话 $sid 已交回 console"
            $acted = $true
        } else {
            Write-Log "tscon($sid) 失败，exit=$LASTEXITCODE"
        }
    } catch {
        Write-Log ("tscon 失败 {0}: {1}" -f $sid, $_.Exception.Message)
    }
}

if (-not $acted -and -not $DryRun) {
    Write-Log '本轮没有需要交回的会话'
}
exit 0
