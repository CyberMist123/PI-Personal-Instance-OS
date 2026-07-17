[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BotId,
    [Parameter(Mandatory = $true)][string]$DisplayName,
    [ValidateSet("reader", "resident", "personal")][string]$Profile = "resident",
    [string]$MediaRoot = "",
    [ValidateSet("residents", "direct", "public_explicit")][string]$DefaultAudience = "residents",
    [switch]$AllowPublic
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Admin = Join-Path $Root ".venv\Scripts\cmx-admin.exe"
if (-not (Test-Path -LiteralPath $Admin)) {
    throw "CMX MCP is not installed. Run install.ps1 first."
}
if (-not $MediaRoot) {
    $MediaRoot = Join-Path $Root ("spool\" + $BotId)
}

$arguments = @(
    "add-bot",
    "--id", $BotId,
    "--display-name", $DisplayName,
    "--profile", $Profile,
    "--media-root", $MediaRoot,
    "--default-audience", $DefaultAudience
)
if ($AllowPublic) {
    $arguments += "--allow-public"
}

& $Admin @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Adding bot failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Bot saved. Testing the resident token..." -ForegroundColor Cyan
& $Admin test --bot $BotId
if ($LASTEXITCODE -ne 0) {
    throw "Bot token test failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "MCP client configuration:" -ForegroundColor Cyan
& $Admin print-config --bot $BotId
