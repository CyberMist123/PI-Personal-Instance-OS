[CmdletBinding()]
param([string]$BotId = "gpt")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$PidFile = Join-Path $PSScriptRoot "runtime\cmx-worker-$BotId.pid"
if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "CMX worker '$BotId' is not running."
    exit 0
}
$pidValue = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$pidValue)) {
    throw "Worker PID file is invalid: $PidFile"
}
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
if ($process) {
    $expected = Join-Path $PSScriptRoot ".venv\Scripts\cmx-worker.exe"
    if (
        $process.ExecutablePath -ne $expected `
        -and $process.CommandLine -notlike "*cmx_mcp.workers*" `
        -and $process.Name -ne "cmx-worker.exe"
    ) {
        # PID reuse after a reboot or crash: never kill the stranger, but a
        # stale PID file must not block stop/update flows either.
        Write-Host "Stale PID file: PID $pidValue now belongs to an unrelated process; leaving it alone." -ForegroundColor Yellow
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "CMX worker '$BotId' stopped." -ForegroundColor Green
        exit 0
    }
    Stop-Process -Id $pidValue -Force
    Wait-Process -Id $pidValue -Timeout 10 -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Write-Host "CMX worker '$BotId' stopped." -ForegroundColor Green
