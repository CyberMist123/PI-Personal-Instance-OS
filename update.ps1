[CmdletBinding()]
param([string]$BotId = "gpt")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root

Write-Host "[1/6] Stopping the remote MCP..." -ForegroundColor Cyan
& (Join-Path $Root "mcp\http-stop.ps1")

Write-Host "[2/6] Pulling the latest main..." -ForegroundColor Cyan
& git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull failed; resolve the repository state first." }

Write-Host "[3/6] Installing the MCP..." -ForegroundColor Cyan
& (Join-Path $Root "mcp\install.ps1")

Write-Host "[4/6] Running the independent MCP smoke..." -ForegroundColor Cyan
try {
    & (Join-Path $Root "mcp\smoke.ps1") -BotId $BotId
} catch {
    # A failed smoke must not leave the remote MCP stopped: keep going so the
    # service restarts, and investigate the smoke separately.
    Write-Host "Smoke failed: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "Continuing so the remote MCP still restarts." -ForegroundColor Yellow
}

Write-Host "[5/6] Reloading Nginx (only if the stack is running)..." -ForegroundColor Cyan
& docker compose exec -T nginx nginx -s reload
if ($LASTEXITCODE -ne 0) {
    Write-Host "Nginx reload skipped; it will pick up config on the next stack start." -ForegroundColor Yellow
}

Write-Host "[6/6] Restarting the remote MCP (only if enabled)..." -ForegroundColor Cyan
$marker = Join-Path $Root "mcp\runtime\http-enabled"
if (Test-Path -LiteralPath $marker) {
    & (Join-Path $Root "mcp\http-start.ps1")
} else {
    Write-Host "Remote MCP autostart is not enabled; skipped." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "CMX one-click update completed." -ForegroundColor Green
