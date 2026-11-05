# 从 git 拉取代码并刷新依赖；不改 data/、config.yaml、.env，也不自动重启服务。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/update.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File scripts/update.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/update.ps1 -NoPull
#   powershell -ExecutionPolicy Bypass -File scripts/update.ps1 -Branch main
param(
    [switch]$DryRun,
    [switch]$NoPull,
    [string]$Branch = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 某些宿主不允许改控制台编码，退回默认即可。
}

function Fail([string]$Message, [string]$Hint = "") {
    Write-Host "升级失败：$Message" -ForegroundColor Red
    if ($Hint) {
        Write-Host "        $Hint" -ForegroundColor Yellow
    }
    exit 1
}

# 直接读 .git/HEAD，不调用 git：-DryRun 才能做到零副作用。
function Get-BranchFromHead([string]$Repo) {
    $gitPath = Join-Path $Repo ".git"
    if (-not (Test-Path -LiteralPath $gitPath)) {
        return ""
    }
    $headFile = ""
    if (Test-Path -LiteralPath $gitPath -PathType Leaf) {
        $line = (Get-Content -LiteralPath $gitPath -TotalCount 1 -Encoding UTF8).Trim()
        if ($line -notmatch '^gitdir:\s*(.+)$') {
            return ""
        }
        $gitDir = $matches[1]
        if (-not [System.IO.Path]::IsPathRooted($gitDir)) {
            $gitDir = Join-Path $Repo $gitDir
        }
        $headFile = Join-Path $gitDir "HEAD"
    } else {
        $headFile = Join-Path $gitPath "HEAD"
    }
    if (-not (Test-Path -LiteralPath $headFile)) {
        return ""
    }
    $head = (Get-Content -LiteralPath $headFile -TotalCount 1 -Encoding UTF8).Trim()
    if ($head -match '^ref:\s*refs/heads/(.+)$') {
        return $matches[1]
    }
    return ""
}

if (-not (Test-Path -LiteralPath (Join-Path $root "pyproject.toml"))) {
    Fail "当前目录不像 dafeiyu 仓库：$root" "请在仓库根目录运行；发行包请用 zip 覆盖安装。"
}

$branch = $Branch.Trim()
if (-not $branch) {
    $branch = Get-BranchFromHead $root
}
if (-not $branch) {
    if ($DryRun) {
        $branch = "<当前分支>"
    } else {
        Fail "无法识别当前分支（可能是 detached HEAD）。" "用 -Branch <name> 指定分支后重试。"
    }
}

$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonReady = Test-Path -LiteralPath $python
if (-not $pythonReady -and -not $DryRun) {
    Fail ".venv 不存在。" "先运行 scripts\setup.ps1 完成安装。"
}

if ($DryRun) {
    Write-Host "升级计划（-DryRun：不执行任何 git / pip 命令，不写文件）：" -ForegroundColor Cyan
    Write-Host "  仓库：$root"
    Write-Host "  分支：$branch"
    Write-Host "  1. git fetch --all --prune"
    Write-Host "  2. git rev-list --left-right --count HEAD...origin/$branch  # 显示 ahead / behind"
    if ($NoPull) {
        Write-Host "  3. 跳过 git pull（-NoPull）"
    } else {
        Write-Host "  3. git pull --ff-only"
    }
    if ($pythonReady) {
        Write-Host ('  4. {0} -m pip install -e ".[dev]"' -f $python)
    } else {
        Write-Host "  4. 未找到 .venv；实际执行前先运行 scripts\setup.ps1"
    }
    Write-Host "  5. 不自动重启服务；完成后执行 scripts\stop.ps1 再 scripts\start-all.ps1"
    Write-Host "提示：data/、config.yaml、.env、vendor/ 不会被改动。" -ForegroundColor DarkGray
    exit 0
}

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Fail "找不到 git。" "发行包请用 zip 覆盖安装；或安装 Git for Windows 后重试。"
}

Push-Location $root
try {
    Write-Host "[1/4] git fetch --all --prune ..." -ForegroundColor Cyan
    & git fetch --all --prune
    if ($LASTEXITCODE -ne 0) {
        Fail "git fetch 失败（退出码 $LASTEXITCODE）。" "检查网络/代理后重试；工作区没有被改动。"
    }

    Write-Host "[2/4] 比较本地与 origin/$branch ..." -ForegroundColor Cyan
    $counts = & git rev-list --left-right --count "HEAD...origin/$branch"
    if ($LASTEXITCODE -ne 0) {
        Fail "无法比较 HEAD 与 origin/$branch。" "确认分支存在（git branch -r），或加 -Branch <name>。"
    }
    $parts = @(($counts | Out-String).Trim() -split '\s+' | Where-Object { $_ })
    if ($parts.Count -ge 2) {
        Write-Host "      ahead $($parts[0]) / behind $($parts[1])"
    }

    if ($NoPull) {
        Write-Host "[3/4] -NoPull：跳过 git pull。" -ForegroundColor Yellow
    } else {
        Write-Host "[3/4] git pull --ff-only ..." -ForegroundColor Cyan
        & git pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Fail "git pull --ff-only 失败（退出码 $LASTEXITCODE）。" "本地有分叉提交或未提交改动：先 git status / git log 处理；不想拉取可加 -NoPull。"
        }
    }

    Write-Host "[4/4] 刷新依赖 ..." -ForegroundColor Cyan
    & $python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) {
        Fail "pip install 失败（退出码 $LASTEXITCODE）。" "检查网络/代理；必要时先 .venv\Scripts\python -m pip install -U pip 再重试。"
    }
} catch {
    Fail "脚本中断：$_" "修好上面的报错后重跑；本次不会自动重启服务。"
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "升级完成。下一步：先 scripts\stop.ps1 停止旧进程，再 scripts\start-all.ps1 启动。" -ForegroundColor Green
Write-Host "（本脚本不会自动重启服务。）" -ForegroundColor DarkGray
