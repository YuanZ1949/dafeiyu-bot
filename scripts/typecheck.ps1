# 类型检查入口：把「只看得见、没人算」的类型债变成一条可重复执行的命令。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/typecheck.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/typecheck.ps1 --outputjson
#
# 前置：.venv 里装了 dev 依赖（basedpyright 在 pyproject 的 dev extra）。
#   .venv\Scripts\python -m pip install -e ".[dev]"
#
# 退出码：0 = 没有 error；1 = 有 error 或输出无法解析；2 = 环境缺失（看提示）。
#
# 只按 summary.errorCount 判定：recommended 模式有 6000+ 条存量 warning，
# 直接用 basedpyright 的退出码会把 warning 也算失败，这条命令就永远「红」。

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

$raw = (& $python -m basedpyright --outputjson --level error src @args 2>$null | Out-String)
try {
    $data = $raw | ConvertFrom-Json
} catch {
    Write-Host "basedpyright 输出不是合法 JSON，原始输出如下：" -ForegroundColor Red
    Write-Host $raw
    exit 1
}

$summary = $data.summary
Write-Host ("basedpyright: {0} files, {1} errors, {2} warnings" -f `
    $summary.filesAnalyzed, $summary.errorCount, $summary.warningCount)
if ($summary.errorCount -gt 0) {
    foreach ($d in $data.generalDiagnostics) {
        if ($d.severity -ne "error") { continue }
        $pos = $d.range.start
        Write-Host ("{0}:{1}:{2} {3} ({4})" -f `
            $d.file, ($pos.line + 1), ($pos.character + 1), $d.message, $d.rule)
    }
    exit 1
}
exit 0
