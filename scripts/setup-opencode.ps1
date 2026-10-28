# Download a single-file opencode.exe into vendor/opencode, so a release package
# does not need Node.js or npm.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/setup-opencode.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/setup-opencode.ps1 -Version 2.2.0
#   powershell -ExecutionPolicy Bypass -File scripts/setup-opencode.ps1 -Proxy http://127.0.0.1:10808
#   powershell -ExecutionPolicy Bypass -File scripts/setup-opencode.ps1 -Force
param(
    [string]$Version = "latest",
    [string]$Proxy = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$vendorDir = Join-Path $root "vendor\opencode"
$exe = Join-Path $vendorDir "opencode.exe"
$package = "@opencode/cli-windows-x64"
$manualHint = "可加 -Proxy，或手动下载后放到 vendor\opencode\opencode.exe 再重跑。"

# Returns "x.y.z" when the exe runs, "" when it is missing or broken.
function Get-OpenCodeVersion {
    param([string]$Path)
    try {
        $output = & $Path --version 2>$null
    } catch {
        return ""
    }
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    $text = ($output | Out-String).Trim()
    if ($text -match '(\d+\.\d+\.\d+)') {
        return $matches[1]
    }
    return ""
}

function Get-LatestOpenCodeVersion {
    $params = @{ Uri = "https://registry.npmjs.org/$package/latest"; UseBasicParsing = $true }
    if ($Proxy) {
        $params.Proxy = $Proxy
    }
    $meta = Invoke-RestMethod @params
    return [string]$meta.version
}

function Save-RemoteFile {
    param([string]$Url, [string]$OutFile)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $curlArgs = @("-L", "--fail", "--retry", "2", "--connect-timeout", "20", "-o", $OutFile, $Url)
        if ($Proxy) {
            $curlArgs = @("-x", $Proxy) + $curlArgs
        }
        & $curl.Source @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "curl failed with exit code $LASTEXITCODE ($Url)"
        }
    } else {
        $params = @{ Uri = $Url; OutFile = $OutFile; UseBasicParsing = $true }
        if ($Proxy) {
            $params.Proxy = $Proxy
        }
        Invoke-WebRequest @params
    }
}

$installed = ""
if (Test-Path $exe) {
    $installed = Get-OpenCodeVersion $exe
}

if ($installed -and -not $Force) {
    Write-Host "opencode v$installed 已就绪，跳过" -ForegroundColor Green
    Write-Host "Use -Force to download it again." -ForegroundColor DarkGray
} else {
    $tgz = Join-Path $env:TEMP "opencode-cli-windows-x64-$([guid]::NewGuid().ToString('N')).tgz"
    $workDir = Join-Path $env:TEMP "opencode-setup-$([guid]::NewGuid().ToString('N'))"

    try {
        $target = $Version.Trim()
        if ($target -eq "" -or $target -eq "latest") {
            Write-Host "Resolving the latest opencode version ..." -ForegroundColor Cyan
            $target = Get-LatestOpenCodeVersion
        }
        if ($target -notmatch '^\d+\.\d+\.\d+') {
            throw "无法解析 opencode 版本号：$target"
        }

        $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
        if (-not $tar) {
            throw "找不到 tar.exe（Windows 10 及以上自带），无法解压 .tgz"
        }

        $url = "https://registry.npmjs.org/$package/-/cli-windows-x64-$target.tgz"
        Write-Host "Downloading opencode $target ..." -ForegroundColor Cyan
        Write-Host "  $url" -ForegroundColor DarkGray
        Save-RemoteFile -Url $url -OutFile $tgz

        Write-Host "Extracting ..." -ForegroundColor Cyan
        New-Item -ItemType Directory -Force -Path $workDir | Out-Null
        & $tar.Source -xzf $tgz -C $workDir
        if ($LASTEXITCODE -ne 0) {
            throw "tar failed with exit code $LASTEXITCODE"
        }

        $extracted = Join-Path $workDir "package\bin\opencode.exe"
        if (-not (Test-Path $extracted)) {
            throw "压缩包里没有 package\bin\opencode.exe"
        }
        New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null
        Copy-Item -LiteralPath $extracted -Destination $exe -Force
    } catch {
        Write-Host "opencode 安装失败：$_" -ForegroundColor Red
        Write-Host $manualHint -ForegroundColor Yellow
        exit 1
    } finally {
        Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tgz -Force -ErrorAction SilentlyContinue
    }

    $installed = Get-OpenCodeVersion $exe
    if (-not $installed) {
        Write-Host "opencode.exe 已下载但无法运行：$exe" -ForegroundColor Red
        Write-Host $manualHint -ForegroundColor Yellow
        exit 1
    }
    Write-Host "opencode v$installed 已就绪" -ForegroundColor Green
    Write-Host "  $exe" -ForegroundColor DarkGray
}
