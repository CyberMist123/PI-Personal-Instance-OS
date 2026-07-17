[CmdletBinding()]
param(
    [string]$BotId = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Admin = Join-Path $Root ".venv\Scripts\cmx-admin.exe"
$Mcp = Join-Path $Root ".venv\Scripts\cmx-mcp.exe"
$Db = Join-Path $Root "runtime\cmx.sqlite3"

$checks = @(
    @{ Name = "cmx-admin"; Path = $Admin },
    @{ Name = "cmx-mcp"; Path = $Mcp },
    @{ Name = "SQLite"; Path = $Db }
)

$failed = $false
foreach ($check in $checks) {
    if (Test-Path -LiteralPath $check.Path) {
        Write-Host ("[OK]   " + $check.Name + " -> " + $check.Path) -ForegroundColor Green
    }
    else {
        Write-Host ("[FAIL] " + $check.Name + " -> " + $check.Path) -ForegroundColor Red
        $failed = $true
    }
}

if ($failed) {
    throw "CMX MCP local file checks failed."
}

Write-Host ""
& $Admin list-bots
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read bot configuration."
}

if ($BotId) {
    Write-Host ""
    Write-Host "Testing bot: $BotId" -ForegroundColor Cyan
    & $Admin test --bot $BotId
    if ($LASTEXITCODE -ne 0) {
        throw "Bot test failed."
    }
}

Write-Host ""
Write-Host "CMX MCP status check passed." -ForegroundColor Green
