[CmdletBinding()]
param(
  [ValidateSet("Prepare", "Switch", "Release")]
  [string]$Phase = "Prepare",
  [string]$NewDomain,
  [switch]$AcknowledgeWebAuthnReset,
  [switch]$DiscardPendingJobs
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$IdentityDomain = "pi.invalid"
$EnvPath = ".env.production"
$MarkerPath = ".pi-os-initialized"

function Write-Utf8NoBom {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string[]]$Lines
  )
  $encoding = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllLines([System.IO.Path]::GetFullPath($Path), $Lines, $encoding)
}

function Get-EnvValue {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$Key
  )
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  $escaped = [regex]::Escape($Key)
  $line = Get-Content -LiteralPath $Path | Where-Object { $_ -match "^$escaped=" } | Select-Object -Last 1
  if ($null -eq $line) { return $null }
  return ($line -replace "^$escaped=", "")
}

function Set-EnvValuesAtomic {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][hashtable]$Values
  )

  $lines = @(Get-Content -LiteralPath $Path)
  foreach ($key in $Values.Keys) {
    $escaped = [regex]::Escape([string]$key)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
      if ($lines[$i] -match "^$escaped=") {
        $lines[$i] = "$key=$($Values[$key])"
        $found = $true
      }
    }
    if (-not $found) {
      $lines += "$key=$($Values[$key])"
    }
  }

  $tempPath = "$Path.tmp"
  Write-Utf8NoBom -Path $tempPath -Lines $lines
  Move-Item -LiteralPath $tempPath -Destination $Path -Force
}

function Test-DomainName {
  param([Parameter(Mandatory)][string]$Value)
  return ($Value -notmatch "://" -and
          $Value -notmatch "[/\\:]" -and
          $Value -match "^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$")
}

function Get-DomainList {
  param([AllowEmptyString()][string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
  return @($Value.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
}

function Set-MarkerAccessDomain {
  param([Parameter(Mandatory)][string]$Domain)
  if (-not (Test-Path -LiteralPath $MarkerPath)) { return }
  $lines = @(Get-Content -LiteralPath $MarkerPath)
  $found = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "^access_domain=") {
      $lines[$i] = "access_domain=$Domain"
      $found = $true
    }
  }
  if (-not $found) { $lines += "access_domain=$Domain" }
  Write-Utf8NoBom -Path $MarkerPath -Lines $lines
}

function Invoke-Backup {
  Write-Host "Creating a verified backup before changing the access domain..." -ForegroundColor Cyan
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\backup.ps1"
  if ($LASTEXITCODE -ne 0) {
    throw "backup.ps1 failed. The domain was not changed."
  }
}

function Recreate-AppServices {
  & docker compose up -d --force-recreate web streaming sidekiq
  if ($LASTEXITCODE -ne 0) {
    throw "Could not recreate web, streaming and sidekiq."
  }
}

function Start-AppServices {
  & docker compose up -d web streaming sidekiq
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "Application services could not be restarted automatically. Run .\start.ps1."
  }
}

function Assert-PublicEndpoint {
  param(
    [Parameter(Mandatory)][string]$Domain,
    [Parameter(Mandatory)][string]$Path
  )
  $uri = "https://$Domain$Path"
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 20
    if ($response.StatusCode -ne 200) { throw "HTTP $($response.StatusCode)" }
    return $response
  } catch {
    throw "$uri is not ready: $($_.Exception.Message)"
  }
}

function Get-PublicInstance {
  param([Parameter(Mandatory)][string]$Domain)
  $response = Assert-PublicEndpoint -Domain $Domain -Path "/api/v2/instance"
  return ($response.Content | ConvertFrom-Json)
}

function Assert-IdentityAndStreaming {
  param(
    [Parameter(Mandatory)]$Instance,
    [Parameter(Mandatory)][string]$ExpectedWebDomain
  )
  if ($Instance.domain -ne $IdentityDomain) {
    throw "Instance identity mismatch: expected $IdentityDomain, received $($Instance.domain)."
  }
  $streamingUrl = [string]$Instance.configuration.urls.streaming
  if ($streamingUrl -ne "wss://$ExpectedWebDomain") {
    throw "Streaming URL mismatch: expected wss://$ExpectedWebDomain, received $streamingUrl."
  }
}

function Get-PendingSidekiqCount {
  # Count executable queues and retries only. Mastodon's recurring scheduler metadata
  # lives in Sidekiq's schedule set and is expected to be non-empty during normal operation.
  $script = "local n=0; for _,k in ipairs(redis.call('keys','queue:*')) do n=n+redis.call('llen',k) end; n=n+redis.call('zcard','retry'); return n"
  $result = & docker compose exec -T redis redis-cli --raw EVAL $script 0 2>$null
  if ($LASTEXITCODE -ne 0) { return -1 }
  $parsed = 0
  if ([int]::TryParse(("$result").Trim(), [ref]$parsed)) { return $parsed }
  return -1
}

function Get-WebAuthnCredentialCount {
  $result = & docker compose exec -T db psql -U mastodon -d mastodon_production -tAc "SELECT count(*) FROM webauthn_credentials;" 2>$null
  if ($LASTEXITCODE -ne 0) { return -1 }
  $parsed = 0
  if ([int]::TryParse(("$result").Trim(), [ref]$parsed)) { return $parsed }
  return -1
}

if (-not (Test-Path -LiteralPath $EnvPath)) {
  throw ".env.production does not exist. Run setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $MarkerPath)) {
  throw "PI OS is not marked initialized. Do not use the domain switch script before setup.ps1 completes."
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running." }

$localDomain = Get-EnvValue -Path $EnvPath -Key "LOCAL_DOMAIN"
$currentWebDomain = Get-EnvValue -Path $EnvPath -Key "WEB_DOMAIN"
$currentStreamingUrl = Get-EnvValue -Path $EnvPath -Key "STREAMING_API_BASE_URL"
$currentAlternateValue = Get-EnvValue -Path $EnvPath -Key "ALTERNATE_DOMAINS"
$currentAlternates = @(Get-DomainList -Value $currentAlternateValue)

if ($localDomain -ne $IdentityDomain) {
  throw "Refusing domain switch: LOCAL_DOMAIN must remain $IdentityDomain, but is $localDomain."
}
if ([string]::IsNullOrWhiteSpace($currentWebDomain)) {
  throw "WEB_DOMAIN is missing."
}
if ($currentStreamingUrl -ne "wss://$currentWebDomain") {
  throw "STREAMING_API_BASE_URL must equal wss://WEB_DOMAIN before switching. Current value: $currentStreamingUrl"
}

if ($Phase -ne "Release") {
  if ([string]::IsNullOrWhiteSpace($NewDomain)) {
    throw "-NewDomain is required for Phase $Phase."
  }
  $NewDomain = $NewDomain.Trim().ToLowerInvariant()
  if (-not (Test-DomainName -Value $NewDomain)) {
    throw "Invalid new domain: $NewDomain. Enter only a hostname such as pi.example.com."
  }
  if ($NewDomain -eq $IdentityDomain) {
    throw "The public WEB_DOMAIN cannot be $IdentityDomain."
  }
}

switch ($Phase) {
  "Prepare" {
    if ($NewDomain -eq $currentWebDomain) {
      throw "$NewDomain is already the active WEB_DOMAIN."
    }

    Write-Host "Checking the new Cloudflare route before changing Mastodon..." -ForegroundColor Cyan
    Assert-PublicEndpoint -Domain $NewDomain -Path "/_pi/health" | Out-Null
    Invoke-Backup

    $newAlternates = @($currentAlternates + $NewDomain | Select-Object -Unique)
    try {
      Set-EnvValuesAtomic -Path $EnvPath -Values @{
        "LOCAL_DOMAIN" = $IdentityDomain
        "ALTERNATE_DOMAINS" = ($newAlternates -join ",")
      }
      Recreate-AppServices
      $instance = Get-PublicInstance -Domain $NewDomain
      if ($instance.domain -ne $IdentityDomain) {
        throw "Prepare preflight returned unexpected instance domain $($instance.domain)."
      }
    } catch {
      Write-Warning "Prepare failed. Restoring the previous ALTERNATE_DOMAINS value."
      Set-EnvValuesAtomic -Path $EnvPath -Values @{
        "LOCAL_DOMAIN" = $IdentityDomain
        "ALTERNATE_DOMAINS" = "$currentAlternateValue"
      }
      & docker compose up -d --force-recreate web streaming sidekiq
      throw
    }

    Write-Host ""
    Write-Host "Prepare phase passed." -ForegroundColor Green
    Write-Host "The new Host/TLS/basic GET path works, but the active WEB_DOMAIN is still $currentWebDomain." -ForegroundColor Yellow
    Write-Host "Streaming, canonical URLs, cookies, WebAuthn and Service Worker still belong to the old origin during this phase."
    Write-Host "Next command:"
    Write-Host ".\change-access-domain.ps1 -Phase Switch -NewDomain `"$NewDomain`"" -ForegroundColor Cyan
  }

  "Switch" {
    if ($NewDomain -eq $currentWebDomain) {
      throw "$NewDomain is already the active WEB_DOMAIN."
    }
    if ($currentAlternates -notcontains $NewDomain) {
      throw "$NewDomain is not in ALTERNATE_DOMAINS. Run Phase Prepare first."
    }

    $webauthnCount = Get-WebAuthnCredentialCount
    if ($webauthnCount -gt 0 -and -not $AcknowledgeWebAuthnReset) {
      throw "Found $webauthnCount WebAuthn/passkey credential(s). They will not work on the new origin. Remove/recover them first, or rerun with -AcknowledgeWebAuthnReset after confirming password/TOTP/recovery access."
    }

    $pendingJobs = Get-PendingSidekiqCount
    if ($pendingJobs -gt 0 -and -not $DiscardPendingJobs) {
      throw "Found $pendingJobs queued/retry Sidekiq job(s). Wait for the queues to drain, or rerun with -DiscardPendingJobs to accept dropping them when Redis is flushed."
    }

    Write-Host "Checking the new public route before the formal switch..." -ForegroundColor Cyan
    Assert-PublicEndpoint -Domain $NewDomain -Path "/_pi/health" | Out-Null
    Invoke-Backup

    Write-Host "Stopping application writers..." -ForegroundColor Cyan
    & docker compose stop web streaming sidekiq
    if ($LASTEXITCODE -ne 0) { throw "Could not stop application services." }

    $pendingAfterStop = Get-PendingSidekiqCount
    if ($pendingAfterStop -gt 0 -and -not $DiscardPendingJobs) {
      Start-AppServices
      throw "Found $pendingAfterStop queued/retry job(s) after the backup window. Services were restarted; wait for them to drain or explicitly use -DiscardPendingJobs."
    }

    $oldWebDomain = $currentWebDomain
    try {
      Set-EnvValuesAtomic -Path $EnvPath -Values @{
        "LOCAL_DOMAIN" = $IdentityDomain
        "WEB_DOMAIN" = $NewDomain
        "STREAMING_API_BASE_URL" = "wss://$NewDomain"
        "ALTERNATE_DOMAINS" = $oldWebDomain
      }

      Write-Host "Clearing Redis cache and old Sidekiq/origin state..." -ForegroundColor Cyan
      & docker compose exec -T redis redis-cli FLUSHDB *> $null
      if ($LASTEXITCODE -ne 0) { throw "Redis FLUSHDB failed." }

      Recreate-AppServices
      $instance = Get-PublicInstance -Domain $NewDomain
      Assert-IdentityAndStreaming -Instance $instance -ExpectedWebDomain $NewDomain
      Assert-PublicEndpoint -Domain $NewDomain -Path "/api/v1/streaming/health" | Out-Null
      Set-MarkerAccessDomain -Domain $NewDomain
    } catch {
      Write-Warning "Formal switch failed. Restoring the previous WEB_DOMAIN=$oldWebDomain."
      Set-EnvValuesAtomic -Path $EnvPath -Values @{
        "LOCAL_DOMAIN" = $IdentityDomain
        "WEB_DOMAIN" = $oldWebDomain
        "STREAMING_API_BASE_URL" = "wss://$oldWebDomain"
        "ALTERNATE_DOMAINS" = $NewDomain
      }
      & docker compose exec -T redis redis-cli FLUSHDB *> $null
      & docker compose up -d --force-recreate web streaming sidekiq
      throw
    }

    Write-Host ""
    Write-Host "WEB_DOMAIN switched to $NewDomain." -ForegroundColor Green
    Write-Host "Now complete the browser smoke on the new origin:" -ForegroundColor Yellow
    Write-Host "- sign in with password/TOTP (not an old passkey)"
    Write-Host "- read an old status and old media"
    Write-Host "- publish text and an image"
    Write-Host "- confirm timeline and streaming update"
    Write-Host "- inspect browser console for CSP/CSRF/mixed-content errors"
    Write-Host "- temporarily disable/block the old domain route and repeat media + streaming checks"
    Write-Host "- re-enable Web Push on the new origin if used"
    Write-Host "After the transition period:"
    Write-Host ".\change-access-domain.ps1 -Phase Release" -ForegroundColor Cyan
  }

  "Release" {
    if ($currentAlternates.Count -eq 0) {
      Write-Host "ALTERNATE_DOMAINS is already empty. Nothing to release." -ForegroundColor Green
      exit 0
    }

    Invoke-Backup
    $released = $currentAlternates -join ","
    try {
      Set-EnvValuesAtomic -Path $EnvPath -Values @{
        "LOCAL_DOMAIN" = $IdentityDomain
        "ALTERNATE_DOMAINS" = ""
      }
      Recreate-AppServices
    } catch {
      Write-Warning "Release failed. Restoring the previous ALTERNATE_DOMAINS value."
      Set-EnvValuesAtomic -Path $EnvPath -Values @{
        "LOCAL_DOMAIN" = $IdentityDomain
        "ALTERNATE_DOMAINS" = "$currentAlternateValue"
      }
      & docker compose up -d --force-recreate web streaming sidekiq
      throw
    }

    Write-Host "Old alternate domain(s) released: $released" -ForegroundColor Green
    Write-Host "Delete their Cloudflare Public Hostname routes after confirming no browser/tab still depends on them." -ForegroundColor Yellow
  }
}