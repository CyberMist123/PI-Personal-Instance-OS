[CmdletBinding()]
param([string]$BotId = "gpt")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$PidFile = Join-Path $PSScriptRoot "runtime\cmx-worker-$BotId.pid"
if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "CMX worker '$BotId': stopped (no PID file)." -ForegroundColor Yellow
    exit 1
}
$pidValue = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$pidValue)) {
    Write-Host "CMX worker '$BotId': stopped (invalid PID file)." -ForegroundColor Yellow
    exit 1
}
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
$expected = Join-Path $PSScriptRoot ".venv\Scripts\cmx-worker.exe"
$related = $process -and (
    $process.ExecutablePath -eq $expected `
    -or $process.CommandLine -like "*cmx_mcp.workers*" `
    -or $process.Name -eq "cmx-worker.exe"
)
if ($related) {
    Write-Host "CMX worker '$BotId': running (PID $pidValue)." -ForegroundColor Green
    exit 0
}
if ($process) {
    Write-Host "CMX worker '$BotId': stopped (stale PID $pidValue belongs to an unrelated process)." -ForegroundColor Yellow
} else {
    Write-Host "CMX worker '$BotId': stopped (PID $pidValue is gone)." -ForegroundColor Yellow
}
exit 1
