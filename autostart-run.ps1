[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "logs"
$logPath = Join-Path $logDir "autostart.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (Test-Path -LiteralPath $logPath) {
  $logFile = Get-Item -LiteralPath $logPath
  if ($logFile.Length -gt 2MB) {
    Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
  }
}

function Write-Log {
  param([Parameter(Mandatory)][string]$Message)
  $line = "{0} {1}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")), $Message
  Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Find-DockerCli {
  $command = Get-Command docker.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }

  $candidates = @(
    (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"),
    (Join-Path $env:LOCALAPPDATA "Docker\resources\bin\docker.exe")
  )

  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }

  return $null
}

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

function Test-DockerDaemon {
  param([Parameter(Mandatory)][string]$DockerExe)
  try {
    $result = Invoke-NativeProcess -FilePath $DockerExe -ArgumentList @("info")
    return ($result.ExitCode -eq 0)
  } catch {
    return $false
  }
}

try {
  Write-Log "PI OS autostart begin."

  if (-not (Test-Path -LiteralPath ".pi-os-initialized") -or
      -not (Test-Path -LiteralPath ".env") -or
      -not (Test-Path -LiteralPath ".env.production")) {
    throw "PI OS is not initialized. Run setup.ps1 before installing autostart."
  }

  $dockerExe = Find-DockerCli
  if (-not $dockerExe) {
    throw "Docker CLI was not found. Install or repair Docker Desktop."
  }

  $dockerBin = Split-Path -Parent $dockerExe
  if ($env:Path -notlike "*$dockerBin*") {
    $env:Path = "$dockerBin;$env:Path"
  }

  if (-not (Test-DockerDaemon -DockerExe $dockerExe)) {
    $desktopCandidates = @(
      (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
      (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    $desktopExe = $desktopCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if (-not $desktopExe) {
      throw "Docker Desktop executable was not found."
    }

    if (-not (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
      Write-Log "Starting Docker Desktop."
      Start-Process -FilePath $desktopExe -WindowStyle Hidden | Out-Null
    } else {
      Write-Log "Docker Desktop process exists; waiting for the Linux engine."
    }

    $dockerReady = $false
    for ($attempt = 1; $attempt -le 60; $attempt++) {
      Start-Sleep -Seconds 5
      if (Test-DockerDaemon -DockerExe $dockerExe) {
        $dockerReady = $true
        break
      }
    }

    if (-not $dockerReady) {
      throw "Docker Desktop did not become ready within 5 minutes."
    }
  }

  Write-Log "Docker is ready; starting PI OS."
  & (Join-Path $PSScriptRoot "start.ps1")

  $healthy = $false
  for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/_pi/health" -TimeoutSec 3
      if ($response.StatusCode -eq 200) {
        $healthy = $true
        break
      }
    } catch {
      Start-Sleep -Seconds 3
    }
  }

  if (-not $healthy) {
    throw "PI OS did not become healthy within 3 minutes."
  }

  Write-Log "PI OS autostart completed successfully."
  exit 0
} catch {
  Write-Log "PI OS autostart failed: $($_.Exception.Message)"
  exit 1
}
