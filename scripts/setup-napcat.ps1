# Download and prepare a portable NapCat inside this project.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1 -DownloadOnly
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1 -Proxy http://127.0.0.1:10808
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1 -Force
# -DownloadOnly only downloads/verifies NapCat and skips the OneBot config, so it
# works before BOT_QQ is known; run again without it once BOT_QQ is filled in.
param(
    [string]$Version = "v4.18.41",
    [string]$Proxy = "",
    [switch]$Force,
    [switch]$DownloadOnly
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$vendorDir = Join-Path $root "vendor"
$napcatDir = Join-Path $vendorDir "napcat"
$zipPath = Join-Path $vendorDir "napcat.zip"
$napcatBat = Join-Path $napcatDir "napcat.bat"
$nodeExe = Join-Path $napcatDir "node.exe"

# The download stage counts as done only when the launcher and its bundled node
# both exist and node actually runs: an interrupted extraction must not be
# mistaken for a complete install, or it would be skipped forever.
function Test-NapCatRuntime {
    param([string]$BatPath, [string]$NodePath)
    if (-not (Test-Path $BatPath)) {
        return $false
    }
    if (-not (Test-Path $NodePath)) {
        return $false
    }
    try {
        & $NodePath --version 2>$null | Out-Null
    } catch {
        return $false
    }
    return ($LASTEXITCODE -eq 0)
}

if ((Test-NapCatRuntime -BatPath $napcatBat -NodePath $nodeExe) -and -not $Force) {
    Write-Host "NapCat already exists: $napcatDir" -ForegroundColor Green
    Write-Host "Use -Force to download it again." -ForegroundColor DarkGray
} else {
    New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null

    $url = "https://github.com/NapNeko/NapCatQQ/releases/download/$Version/NapCat.Shell.Windows.Node.zip"
    Write-Host "Downloading NapCat $Version ..." -ForegroundColor Cyan
    Write-Host "  $url" -ForegroundColor DarkGray

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $curlArgs = @("-L", "--fail", "--retry", "2", "--connect-timeout", "20", "-o", $zipPath, $url)
        if ($Proxy) {
            $curlArgs = @("-x", $Proxy) + $curlArgs
        }
        & $curl.Source @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "curl failed with exit code $LASTEXITCODE"
        }
    } else {
        $params = @{ Uri = $url; OutFile = $zipPath; UseBasicParsing = $true }
        if ($Proxy) {
            $params.Proxy = $Proxy
        }
        Invoke-WebRequest @params
    }

    if (Test-Path $napcatDir) {
        Remove-Item -LiteralPath $napcatDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $napcatDir | Out-Null
    Write-Host "Extracting ..." -ForegroundColor Cyan
    Expand-Archive -LiteralPath $zipPath -DestinationPath $napcatDir -Force

    # Probe once after extracting, so a broken download fails loudly instead of
    # being treated as installed on the next run.
    if (-not (Test-NapCatRuntime -BatPath $napcatBat -NodePath $nodeExe)) {
        throw "NapCat was extracted but node.exe cannot run: $nodeExe. Rerun: powershell -ExecutionPolicy Bypass -File scripts\setup-napcat.ps1 -Force"
    }
}

# Read only the values needed for the OneBot config; never print tokens.
$envMap = @{}
$envPath = Join-Path $root ".env"
if (Test-Path $envPath) {
    Get-Content -LiteralPath $envPath | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $envMap[$matches[1]] = $matches[2].Trim('"').Trim("'")
        }
    }
}

$botQQ = $envMap["BOT_QQ"]

# Stage 1 only: the download is done, the OneBot config is left for later, when
# BOT_QQ is known. Missing BOT_QQ is not an error here.
if ($DownloadOnly) {
    Write-Host "NapCat is ready: $napcatDir" -ForegroundColor Green
    if ($botQQ) {
        Write-Host "OneBot config skipped (-DownloadOnly). Run again without -DownloadOnly to write it." -ForegroundColor DarkGray
    } else {
        Write-Host "BOT_QQ is missing in .env: OneBot config skipped." -ForegroundColor Yellow
        Write-Host "After filling BOT_QQ, run this script again without -DownloadOnly to write the OneBot config." -ForegroundColor Yellow
    }
    return
}

if (-not $botQQ) {
    throw "BOT_QQ is missing in .env. Fill it before running this script (or add -DownloadOnly to install NapCat only)."
}
$port = $envMap["NAPCAT_WS_PORT"]
if (-not $port) {
    $port = "8080"
}
$token = $envMap["NAPCAT_WS_TOKEN"]
if ($null -eq $token) {
    $token = ""
}

$configDir = Join-Path $napcatDir "napcat\config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

# Stage 2 runs every time, independently of the download idempotency: the file is
# pure local state, and a config left over from an earlier run (or written before
# BOT_QQ was filled in) must not win over the current .env values.
$config = [ordered]@{
    network = [ordered]@{
        httpServers      = @()
        httpClients      = @()
        websocketServers = @()
        websocketClients = @(
            [ordered]@{
                name              = "dafeiyu-reverse"
                enable            = $true
                url               = "ws://127.0.0.1:$port"
                messagePostFormat = "array"
                reportSelfMessage = $false
                reconnectInterval = 5000
                token             = $token
                debug             = $false
                heartInterval     = 30000
            }
        )
    }
    musicSignUrl        = ""
    enableLocalFile2Url = $false
    parseMultMsg        = $false
}

$json = $config | ConvertTo-Json -Depth 8
$configFile = Join-Path $configDir "onebot11_$botQQ.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($configFile, $json, $utf8NoBom)

Write-Host "NapCat is ready: $napcatDir" -ForegroundColor Green
Write-Host "OneBot config: $configFile" -ForegroundColor Green
Write-Host "Reverse WS: ws://127.0.0.1:$port (array, token $(if ($token) { 'configured' } else { 'empty' }))" -ForegroundColor Green
Write-Host "Next: powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1" -ForegroundColor Cyan
