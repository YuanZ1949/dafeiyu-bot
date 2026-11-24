# 类型检查入口：把「只看得见、没人算」的类型债变成一条可重复执行的命令。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/typecheck.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/typecheck.ps1 --outputjson
#
# 前置：.venv 里装了 dev 依赖（basedpyright 在 pyproject 的 dev extra）。
#   .venv\Scripts\python -m pip install -e ".[dev]"
#
# 退出码：0 = 没有 error；非 0 = 有 error 或环境缺失（看提示）。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "先运行 scripts/setup.ps1 或手动创建 .venv。" -ForegroundColor Yellow
    exit 1
}

# 先探测 basedpyright 是否安装：缺包和「有类型错误」都是非 0，必须区分开。
$env:PYTHONIOENCODING = "utf-8"
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python -c "import basedpyright" 2>$null | Out-Null
$probe = $LASTEXITCODE
$ErrorActionPreference = $previous

if ($probe -ne 0) {
    Write-Host "未安装 basedpyright。" -ForegroundColor Yellow
    Write-Host '请先安装 dev 依赖：.venv\Scripts\python -m pip install -e ".[dev]"' -ForegroundColor Yellow
    exit 2
}

& $python -m basedpyright src @args
exit $LASTEXITCODE
