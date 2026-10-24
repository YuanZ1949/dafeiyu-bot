# Download and prepare a portable NapCat inside this project.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1 -Proxy http://127.0.0.1:10808
#   powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1 -Force
param(
    [string]$Version = "v4.18.41",
    [string]$Proxy = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$vendorDir = Join-Path $root "vendor"
$napcatDir = Join-Path $vendorDir "napcat"
$zipPath = Join-Path $vendorDir "napcat.zip"
$napcatBat = Join-Path $napcatDir "napcat.bat"

if ((Test-Path $napcatBat) -and -not $Force) {
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
if (-not $botQQ) {
    throw "BOT_QQ is missing in .env. Fill it before running this script."
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
