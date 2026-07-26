[CmdletBinding()]
param(
    [string]$BotId = "",
    [string]$DisplayName = "",
    [string]$Email = "",
    [ValidateSet("reader", "resident", "personal")][string]$Profile = "reader",
    [ValidateSet("disabled", "reader", "social", "social_plus")][string]$RemoteProfile = "reader",
    [switch]$RemoteBoosts,
    [switch]$RemoteNotifications,
    [switch]$UseExistingAccount,
    [switch]$Invite
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Repository = Split-Path -Parent $Root

if (-not $BotId) { $BotId = Read-Host "AI resident username (a-z, 0-9 or _)" }
$BotId = $BotId.Trim().ToLowerInvariant()
if ($BotId -notmatch '^[a-z0-9_]+$') {
    throw "BotId must be a valid Mastodon username: a-z, 0-9, and underscore only."
}
if (-not $DisplayName) { $DisplayName = Read-Host "Display name" }
$DisplayName = $DisplayName.Trim()
if (-not $DisplayName) { $DisplayName = $BotId }

if (-not $UseExistingAccount) {
    if (-not $Email) { $Email = Read-Host "Real reachable email for this AI resident" }
    $Email = $Email.Trim()
    if ($Email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$' -or $Email.EndsWith(".invalid")) {
        throw "A reachable email address is required; pi.invalid addresses are not accepted."
    }

    Set-Location -LiteralPath $Repository
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running." }

    Write-Host "Creating and approving Mastodon resident @$BotId..." -ForegroundColor Cyan
    $output = @(& docker compose exec -T web tootctl accounts create $BotId "--email=$Email" --confirmed --approve 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $safe = ($output | Select-Object -Last 5) -join " | "
        throw "Account creation failed: $safe"
    }
    Write-Host ""
    Write-Host "One-time Mastodon login details:" -ForegroundColor Yellow
    $output | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
    Write-Host "This password is not written to Git, MCP SQLite, or logs." -ForegroundColor DarkGray
    Write-Host "Use it in the browser window that opens next." -ForegroundColor DarkGray
    Write-Host ""
}

$authorize = Join-Path $Root "authorize-bot.ps1"
& $authorize -BotId $BotId -DisplayName $DisplayName -Profile $Profile -RemoteProfile $RemoteProfile -RemoteBoosts:$RemoteBoosts -RemoteNotifications:$RemoteNotifications
if ($LASTEXITCODE -ne 0) { throw "Resident browser authorization failed." }

$smoke = Join-Path $Root "smoke.ps1"
& $smoke -BotId $BotId
if ($LASTEXITCODE -ne 0) { throw "Resident was saved, but the independent MCP smoke failed." }

$httpMarker = Join-Path $Root "runtime\http-enabled"
if (Test-Path -LiteralPath $httpMarker) {
    Write-Host "Refreshing the profiled remote MCP resident map..." -ForegroundColor Cyan
    & (Join-Path $Root "http-stop.ps1")
    & (Join-Path $Root "http-start.ps1")
}

if ($Invite) {
    if ($RemoteProfile -eq "disabled") {
        Write-Host "RemoteProfile is disabled; no onboarding invite was minted." -ForegroundColor Yellow
    } else {
        $admin = Join-Path $Root ".venv\Scripts\cmx-admin.exe"
        $scopes = "read"
        if ($RemoteProfile -in @("social", "social_plus")) { $scopes = "read,social" }
        Write-Host "Minting a single-use onboarding invite..." -ForegroundColor Cyan
        & $admin invite-new --bot $BotId --scopes $scopes
        if ($LASTEXITCODE -ne 0) { throw "Invite creation failed." }
    }
}

Write-Host ""
Write-Host "AI resident setup completed." -ForegroundColor Green
Write-Host "Local Claude Code: run cmx-admin print-config --bot $BotId" -ForegroundColor Cyan
Write-Host "Remote MCP after enabling HTTP: https://<WEB_DOMAIN>/mcp/$BotId" -ForegroundColor Cyan
