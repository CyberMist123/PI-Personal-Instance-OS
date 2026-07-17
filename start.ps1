[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Invoke-NativeProcess {
  param(
    [Parameter(Mandatory)][string]$FilePath,
    [Parameter(Mandatory)][string[]]$ArgumentList
  )

  $stdoutPath = [System.IO.Path]::GetTempFileName()
  $stderrPath = [System.IO.Path]::GetTempFileName()
  try {
    $process = Start-Process `
      -FilePath $FilePath `
      -ArgumentList $ArgumentList `
      -WorkingDirectory $PSScriptRoot `
      -NoNewWindow `
      -Wait `
      -PassThru `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError $stderrPath

    $stdout = if (Test-Path -LiteralPath $stdoutPath) { @(Get-Content -LiteralPath $stdoutPath) } else { @() }
    $stderr = if (Test-Path -LiteralPath $stderrPath) { @(Get-Content -LiteralPath $stderrPath) } else { @() }

    return [pscustomobject]@{
      ExitCode = $process.ExitCode
      StdOut   = $stdout
      StdErr   = $stderr
    }
  } finally {
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
  }
}

function Get-NativeFailureText {
  param([Parameter(Mandatory)]$Result)
  $lines = @($Result.StdErr) + @($Result.StdOut)
  $text = ($lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Last 5) -join " | "
  if ([string]::IsNullOrWhiteSpace($text)) { return "exit code $($Result.ExitCode)" }
  return $text
}

if (-not (Test-Path -LiteralPath ".env") -or -not (Test-Path -LiteralPath ".env.production")) {
  throw "PI OS is not configured. Run .\setup.ps1 first."
}

$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $dockerCommand) {
  throw "Docker CLI was not found."
}
$dockerExe = $dockerCommand.Source

$infoResult = Invoke-NativeProcess -FilePath $dockerExe -ArgumentList @("info")
if ($infoResult.ExitCode -ne 0) {
  throw "Docker Desktop is not running: $(Get-NativeFailureText -Result $infoResult)"
}

$tokenLine = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^CLOUDFLARE_TUNNEL_TOKEN=" } | Select-Object -Last 1
$token = if ($tokenLine) { $tokenLine -replace "^CLOUDFLARE_TUNNEL_TOKEN=", "" } else { "" }

if ([string]::IsNullOrWhiteSpace($token) -or $token -eq "MISSING") {
  $null = Invoke-NativeProcess -FilePath $dockerExe -ArgumentList @("compose", "--profile", "tunnel", "stop", "cloudflared")
  $startResult = Invoke-NativeProcess -FilePath $dockerExe -ArgumentList @("compose", "up", "-d")
} else {
  $startResult = Invoke-NativeProcess -FilePath $dockerExe -ArgumentList @("compose", "--profile", "tunnel", "up", "-d")
}

if ($startResult.ExitCode -ne 0) {
  throw "Failed to start PI OS: $(Get-NativeFailureText -Result $startResult)"
}

Write-Host "PI OS started." -ForegroundColor Green
Write-Host "Local health: http://127.0.0.1:8080/_pi/health"
