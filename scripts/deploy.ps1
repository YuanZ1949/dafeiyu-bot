# 把当前仓库部署到 release 目录：备份 -> 停服 -> 拷贝 -> 校验 -> 启动 -> 健康检查。
# 任何一步失败都自动回滚到部署前的文件并重新拉起，避免留下半套代码。
#
# 只覆盖发布白名单（src/scripts/.opencode + 示例配置等），永远不碰目标目录里的
# config.yaml、.env、data/、logs/、vendor/、.venv —— 部署不该改用户数据与机密。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1 -Target D:\dafeiyu-release
#   powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1 -Plan        # 只列出将拷贝哪些文件
#   powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1 -NoRestart   # 只拷文件，不重启
param(
    [string]$Source = "",
    [string]$Target = "C:\MyFile\AICoding\dafeiyu-release",
    [switch]$Plan,
    [switch]$NoRestart,
    [int]$HealthTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

$env:PYTHONIOENCODING = "utf-8"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 某些宿主不允许改控制台编码，退回默认即可。
}

if (-not $Source) {
    $Source = Split-Path -Parent $PSScriptRoot
}
$Source = (Resolve-Path -LiteralPath $Source).Path

function Fail([string]$Message, [int]$Code = 2) {
    Write-Host "部署失败：$Message" -ForegroundColor Red
    exit $Code
}

if (-not (Test-Path -LiteralPath (Join-Path $Source "src\dafeiyu"))) {
    Fail "源目录不像大肥鱼仓库：$Source"
}

# 发布白名单是唯一真源：直接复用 package_manifest，别在脚本里再抄一份。
$env:PYTHONPATH = Join-Path $Source "src"
$sourcePython = Join-Path $Source ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $sourcePython)) {
    $sourcePython = "python"
}
$relText = ""
Push-Location $Source
try {
    $relText = & $sourcePython -c "from pathlib import Path; from dafeiyu.package_manifest import iter_payload; print('\n'.join(p.as_posix() for p in iter_payload(Path('.'))))"
} finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0 -or -not $relText) {
    Fail "读取发布白名单失败（检查源目录 .venv 与 src/dafeiyu/package_manifest.py）"
}
$files = @($relText -split "\r?\n" | Where-Object { $_.Trim() })
if ($files.Count -eq 0) {
    Fail "发布白名单为空，拒绝部署"
}

Write-Host "源目录：$Source" -ForegroundColor Cyan
Write-Host "目标目录：$Target" -ForegroundColor Cyan
Write-Host "待部署文件：$($files.Count) 个" -ForegroundColor Cyan

if ($Plan) {
    $files | ForEach-Object { Write-Host "  $_" }
    Write-Host "（-Plan 只列出文件，未做任何改动）" -ForegroundColor DarkGray
    exit 0
}

if (-not (Test-Path -LiteralPath $Target)) {
    Fail "目标目录不存在：$Target"
}
$targetScripts = Join-Path $Target "scripts"
$targetPython = Join-Path $Target ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath (Join-Path $Target "src\dafeiyu"))) {
    Fail "目标目录不是已安装的 release：$Target"
}
if (-not (Test-Path -LiteralPath $targetPython)) {
    Fail "目标目录缺少 .venv\Scripts\python.exe：$Target"
}
if (-not (Test-Path -LiteralPath (Join-Path $targetScripts "start-all.ps1"))) {
    Fail "目标目录缺少 scripts\start-all.ps1：$Target"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $env:TEMP "opencode\deploy-$stamp"
New-Item -ItemType Directory -Force -Path $backup | Out-Null

# ---- 1. 备份：只备份本次会被覆盖的文件，外加「原来不存在」的清单 ----
$existed = New-Object System.Collections.Generic.List[string]
foreach ($rel in $files) {
    $dst = Join-Path $Target $rel
    if (Test-Path -LiteralPath $dst) {
        $bak = Join-Path $backup $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $bak) | Out-Null
        Copy-Item -LiteralPath $dst -Destination $bak -Force
        $existed.Add($rel)
    }
}
$existed | Set-Content -LiteralPath (Join-Path $backup "_existed.txt") -Encoding utf8
$files | Set-Content -LiteralPath (Join-Path $backup "_files.txt") -Encoding utf8
Write-Host "已备份 $($existed.Count) 个文件到 $backup" -ForegroundColor DarkGray

function Get-Sha256([string]$Path) {
    # 用 .NET 直接算：Get-FileHash 在继承 pwsh 的 PSModulePath 时可能找不到模块。
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "")
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha.Dispose()
    }
}

function Invoke-TargetStop {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $targetScripts "stop.ps1") | Out-Host
}

function Invoke-TargetStart {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $targetScripts "start-all.ps1") -NoBrowser -WaitSeconds 120 | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Restore-Backup {
    # 删掉本次新建的文件，再把备份逐个放回：目标目录回到部署前的精确状态。
    foreach ($rel in $files) {
        if (-not ($existed -contains $rel)) {
            $dst = Join-Path $Target $rel
            if (Test-Path -LiteralPath $dst) {
                Remove-Item -LiteralPath $dst -Force
            }
        }
    }
    foreach ($rel in $existed) {
        $src = Join-Path $backup $rel
        $dst = Join-Path $Target $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    Write-Host "已从备份回滚：$backup" -ForegroundColor Yellow
}

$stopped = $false
$copied = $false
try {
    if (-not $NoRestart) {
        Write-Host "停止 release 当前进程..." -ForegroundColor Cyan
        Invoke-TargetStop
        $stopped = $true
    }

    # ---- 2. 拷贝 ----
    foreach ($rel in $files) {
        $src = Join-Path $Source $rel
        $dst = Join-Path $Target $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    $copied = $true

    # ---- 3. SHA256 校验：源与目标逐个比对，避免半截拷贝 ----
    $mismatch = New-Object System.Collections.Generic.List[string]
    foreach ($rel in $files) {
        $srcHash = Get-Sha256 (Join-Path $Source $rel)
        $dstHash = Get-Sha256 (Join-Path $Target $rel)
        if ($srcHash -ne $dstHash) {
            $mismatch.Add($rel)
        }
    }
    if ($mismatch.Count -gt 0) {
        throw "SHA256 不一致：$($mismatch -join '、')"
    }
    Write-Host "SHA256 校验通过：$($files.Count)/$($files.Count)" -ForegroundColor Green

    if (-not $NoRestart) {
        # ---- 4. 启动（start-all 自己会等面板 /login 就绪）----
        if (-not (Invoke-TargetStart)) {
            throw "start-all.ps1 未在 120 秒内拉起面板"
        }

        # ---- 5. 健康检查：bot 控制口的 /api/health 免鉴权，直接探 ----
        $env:PYTHONPATH = Join-Path $Target "src"
        $controlPort = 8788
        $printed = ""
        Push-Location $Target
        try {
            $printed = & $targetPython -c "from dafeiyu.config import load_settings; print(load_settings().supervisor.control_port)"
        } catch {
            $printed = ""
        } finally {
            Pop-Location
        }
        if ($printed) {
            $controlPort = [int](($printed | Select-Object -Last 1).Trim())
        }
        $healthUrl = "http://127.0.0.1:$controlPort/api/health"
        $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
        $healthy = $false
        $lastError = ""
        while ((Get-Date) -lt $deadline) {
            try {
                $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
                if ($resp.StatusCode -eq 200) {
                    $healthy = $true
                    break
                }
            } catch {
                $lastError = $_.Exception.Message
                Start-Sleep -Milliseconds 500
            }
        }
        if (-not $healthy) {
            throw "健康检查未通过：$healthUrl（$lastError）"
        }
        Write-Host "健康检查通过：$healthUrl" -ForegroundColor Green
    } else {
        Write-Host "（已传 -NoRestart，未重启服务）" -ForegroundColor DarkGray
    }

    Write-Host "部署完成，备份保留在：$backup" -ForegroundColor Green
    exit 0
} catch {
    Write-Host "部署中断：$_" -ForegroundColor Red
    if ($copied) {
        Write-Host "开始回滚..." -ForegroundColor Yellow
        if (-not $NoRestart -and $stopped) {
            try { Invoke-TargetStop } catch { }
        }
        Restore-Backup
        if (-not $NoRestart) {
            Write-Host "回滚后重新启动..." -ForegroundColor Yellow
            if (-not (Invoke-TargetStart)) {
                Write-Host "回滚后仍未能拉起，请手动检查 logs\supervisor\supervisor.log" -ForegroundColor Red
            }
        }
    }
    exit 1
}
