# 按白名单把仓库打成发布 zip（src/scripts/.opencode + 示例配置等）。
# 机密与运行数据永远不进包：config.yaml、.env、data/ 由 package_manifest 过滤；
# vendor/、tests/、docs/ 也不进包（vendor 由安装时的 setup.bat 下载）。
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/package.ps1 -Out dist\dafeiyu.zip
param(
    [Parameter(Mandatory = $true)]
    [string]$Out
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

# 统一按 UTF-8 收子进程输出：非中文区域的 Windows 重定向到文件时也不会编码失败。
$env:PYTHONIOENCODING = "utf-8"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 某些宿主不允许改控制台编码，退回默认即可。
}

# 仓库是 src 布局，没 pip install -e 也要能 import dafeiyu。
$env:PYTHONPATH = Join-Path $root "src"

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $python = "python"
}

# -Out 支持相对当前目录或绝对路径；目录不存在就建。
$outPath = $Out
if (-not [System.IO.Path]::IsPathRooted($outPath)) {
    $outPath = Join-Path (Get-Location).Path $outPath
}
$outDir = Split-Path -Parent $outPath
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

Push-Location $root
try {
    & $python -m dafeiyu.package_manifest --root $root --out $outPath
    if ($LASTEXITCODE -ne 0) {
        throw "打包失败：python 退出码 $LASTEXITCODE"
    }
} catch {
    Write-Host "打包失败：$_" -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}

Write-Host "已生成 $outPath" -ForegroundColor Green
