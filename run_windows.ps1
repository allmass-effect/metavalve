param(
    [string]$ItemId = "propellant-valve-assembly",
    [ValidateSet("buy", "sell")]
    [string]$ListingType = "sell",
    [ValidateRange(1, 2147483647)]
    [int]$Interval = 60
)

$ErrorActionPreference = "Stop"
$credentialsFile = Join-Path $env:USERPROFILE ".config\metavalve\credentials.env"
if (-not (Test-Path -LiteralPath $credentialsFile -PathType Leaf)) {
    Write-Error "Credentials file not found: $credentialsFile"
    exit 1
}

foreach ($line in Get-Content -LiteralPath $credentialsFile) {
    $entry = $line.Trim()
    if ($entry -match '^export\s+') {
        $entry = $entry -replace '^export\s+', ''
    }
    if ($entry -notmatch '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_PROXY)\s*=\s*(.*)$') {
        continue
    }

    $name = $Matches[1]
    $value = $Matches[2].Trim()
    if ($value.Length -ge 2) {
        $first = $value[0]
        $last = $value[$value.Length - 1]
        if (($first -eq "'" -or $first -eq '"') -and $last -eq $first) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

if (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:TELEGRAM_CHAT_ID) {
    Write-Error "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in $credentialsFile"
    exit 1
}

$scriptPath = Join-Path $PSScriptRoot "trade_alerts.py"
$python = Get-Command python -ErrorAction Stop
$env:MARKET_BASE_URL = "https://metaforge.app/arc-raiders/market"
$env:MARKET_ITEM_ID = $ItemId
$env:MARKET_LISTING_TYPE = $ListingType
$env:POLL_INTERVAL_SECONDS = "$Interval"
$env:HTTP_TIMEOUT_SECONDS = "20"
$env:TRADE_ALERTS_DB = Join-Path (Get-Location).Path "trade-alerts.sqlite3"
$env:TRADE_ALERTS_LOG_FILE = Join-Path (Get-Location).Path "valve.log"

& $python.Source $scriptPath --watch --item-id $ItemId --listing-type $ListingType --interval $Interval
exit $LASTEXITCODE
