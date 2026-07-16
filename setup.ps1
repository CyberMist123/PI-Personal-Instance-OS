[CmdletBinding()]
param(
  [string]$Domain,
  [string]$AdminUsername = "owner",
  [string]$AdminEmail,
  [string]$TunnelToken,
  [switch]$ResetSecrets
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Require-Command {
  param([Parameter(Mandatory)][string]$Name)
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Required command not found: $Name"
  }
}

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

function Set-EnvValue {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$Key,
    [AllowEmptyString()][string]$Value
  )
  $lines = @(Get-Content -LiteralPath $Path)
  $escaped = [regex]::Escape($Key)
  $found = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "^$escaped=") {
      $lines[$i] = "$Key=$Value"
      $found = $true
    }
  }
  if (-not $found) {
    $lines += "$Key=$Value"
  }
  Write-Utf8NoBom -Path $Path -Lines $lines
}

function New-RandomHex {
  param([int]$Count = 32)
  $bytes = New-Object byte[] $Count
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function New-RandomBase64 {
  param([int]$Count = 32)
  $bytes = New-Object byte[] $Count
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  return [Convert]::ToBase64String($bytes)
}

function Invoke-Docker {
  param([Parameter(Mandatory)][string[]]$Arguments)
  & docker @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
  }
}

Require-Command docker

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Docker Desktop is not running or the current shell cannot access Docker."
}

& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Docker Compose v2 is required."
}

if (-not (Test-Path -LiteralPath ".env")) {
  Copy-Item -LiteralPath ".env.example" -Destination ".env"
}
if (-not (Test-Path -LiteralPath ".env.production")) {
  Copy-Item -LiteralPath ".env.production.example" -Destination ".env.production"
}

$initialized = Test-Path -LiteralPath ".pi-os-initialized"
$existingDomain = Get-EnvValue -Path ".env.production" -Key "LOCAL_DOMAIN"

if ([string]::IsNullOrWhiteSpace($Domain)) {
  if ($initialized -and -not [string]::IsNullOrWhiteSpace($existingDomain)) {
    $Domain = $existingDomain
  } else {
    $Domain = Read-Host "Final Mastodon domain, without https://"
  }
}
$Domain = $Domain.Trim().ToLowerInvariant()
if ($Domain -match "://" -or $Domain -match "/" -or $Domain -notmatch "^[a-z0-9.-]+\.[a-z]{2,}$") {
  throw "Invalid domain: $Domain. Enter only a hostname such as pi.example.com."
}

if ($initialized -and $ResetSecrets) {
  throw "PI OS is already initialized. Resetting encryption secrets can destroy access to stored data. Restore from backup instead."
}
if ($initialized -and -not [string]::IsNullOrWhiteSpace($existingDomain) -and $existingDomain -ne $Domain) {
  throw "PI OS is already initialized as $existingDomain. LOCAL_DOMAIN cannot be changed safely to $Domain."
}

$AdminUsername = $AdminUsername.Trim().ToLowerInvariant()
if ($AdminUsername -notmatch "^[a-z0-9_]+$") {
  throw "Admin username may contain only lowercase letters, digits, and underscores."
}

if ([string]::IsNullOrWhiteSpace($AdminEmail)) {
  $AdminEmail = Read-Host "Owner email used for login (no confirmation email is sent)"
}
$AdminEmail = $AdminEmail.Trim()
if ($AdminEmail -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") {
  throw "Invalid email address: $AdminEmail"
}

$existingTunnelToken = Get-EnvValue -Path ".env" -Key "CLOUDFLARE_TUNNEL_TOKEN"
if ($null -eq $TunnelToken) {
  if (-not [string]::IsNullOrWhiteSpace($existingTunnelToken) -and $existingTunnelToken -ne "MISSING") {
    $TunnelToken = $existingTunnelToken
  } else {
    $TunnelToken = Read-Host "Cloudflare Tunnel token (press Enter to configure later)"
  }
}
$TunnelToken = $TunnelToken.Trim()

$dbPassword = Get-EnvValue -Path ".env" -Key "POSTGRES_PASSWORD"
if ($ResetSecrets -or [string]::IsNullOrWhiteSpace($dbPassword) -or $dbPassword -like "CHANGE_ME*") {
  $dbPassword = New-RandomHex -Count 32
}

Set-EnvValue -Path ".env" -Key "POSTGRES_DB" -Value "mastodon_production"
Set-EnvValue -Path ".env" -Key "POSTGRES_USER" -Value "mastodon"
Set-EnvValue -Path ".env" -Key "POSTGRES_PASSWORD" -Value $dbPassword
Set-EnvValue -Path ".env" -Key "CLOUDFLARE_TUNNEL_TOKEN" -Value $TunnelToken

Set-EnvValue -Path ".env.production" -Key "LOCAL_DOMAIN" -Value $Domain
Set-EnvValue -Path ".env.production" -Key "DB_HOST" -Value "db"
Set-EnvValue -Path ".env.production" -Key "DB_PORT" -Value "5432"
Set-EnvValue -Path ".env.production" -Key "DB_NAME" -Value "mastodon_production"
Set-EnvValue -Path ".env.production" -Key "DB_USER" -Value "mastodon"
Set-EnvValue -Path ".env.production" -Key "DB_PASS" -Value $dbPassword
Set-EnvValue -Path ".env.production" -Key "REDIS_HOST" -Value "redis"
Set-EnvValue -Path ".env.production" -Key "REDIS_PORT" -Value "6379"

$secretSpecs = @(
  @{ Key = "SECRET_KEY_BASE"; Kind = "hex"; Bytes = 64 },
  @{ Key = "OTP_SECRET"; Kind = "hex"; Bytes = 64 },
  @{ Key = "ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY"; Kind = "base64"; Bytes = 32 },
  @{ Key = "ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY"; Kind = "base64"; Bytes = 32 },
  @{ Key = "ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT"; Kind = "base64"; Bytes = 32 }
)

foreach ($spec in $secretSpecs) {
  $current = Get-EnvValue -Path ".env.production" -Key $spec.Key
  if ($ResetSecrets -or [string]::IsNullOrWhiteSpace($current)) {
    $value = if ($spec.Kind -eq "hex") {
      New-RandomHex -Count $spec.Bytes
    } else {
      New-RandomBase64 -Count $spec.Bytes
    }
    Set-EnvValue -Path ".env.production" -Key $spec.Key -Value $value
  }
}

New-Item -ItemType Directory -Force -Path ".\data\media", ".\backups" | Out-Null

Write-Host "Validating Docker Compose configuration..." -ForegroundColor Cyan
& docker compose config --quiet
if ($LASTEXITCODE -ne 0) {
  throw "Docker Compose configuration is invalid."
}

Write-Host "Pulling container images..." -ForegroundColor Cyan
Invoke-Docker -Arguments @("compose", "--profile", "tunnel", "pull")

Write-Host "Starting PostgreSQL and Redis..." -ForegroundColor Cyan
Invoke-Docker -Arguments @("compose", "up", "-d", "db", "redis")

$dbReady = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
  & docker compose exec -T db pg_isready -U mastodon -d mastodon_production *> $null
  if ($LASTEXITCODE -eq 0) {
    $dbReady = $true
    break
  }
  Start-Sleep -Seconds 2
}
if (-not $dbReady) {
  throw "PostgreSQL did not become ready within 60 seconds."
}

$vapidPrivate = Get-EnvValue -Path ".env.production" -Key "VAPID_PRIVATE_KEY"
$vapidPublic = Get-EnvValue -Path ".env.production" -Key "VAPID_PUBLIC_KEY"
if ($ResetSecrets -or [string]::IsNullOrWhiteSpace($vapidPrivate) -or [string]::IsNullOrWhiteSpace($vapidPublic)) {
  Write-Host "Generating Web Push keys..." -ForegroundColor Cyan
  $vapidOutput = & docker compose run --rm --no-deps web bundle exec rake mastodon:webpush:generate_vapid_key 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "VAPID key generation failed:`n$($vapidOutput -join "`n")"
  }

  foreach ($line in $vapidOutput) {
    $text = "$line".Trim()
    if ($text -match "^(VAPID_PRIVATE_KEY|VAPID_PUBLIC_KEY)=(.+)$") {
      Set-EnvValue -Path ".env.production" -Key $Matches[1] -Value $Matches[2]
    }
  }

  if ([string]::IsNullOrWhiteSpace((Get-EnvValue ".env.production" "VAPID_PRIVATE_KEY")) -or
      [string]::IsNullOrWhiteSpace((Get-EnvValue ".env.production" "VAPID_PUBLIC_KEY"))) {
    throw "Could not parse VAPID keys from Mastodon output:`n$($vapidOutput -join "`n")"
  }
}

Write-Host "Preparing Mastodon database..." -ForegroundColor Cyan
Invoke-Docker -Arguments @("compose", "run", "--rm", "--no-deps", "web", "bundle", "exec", "rails", "db:prepare")

Write-Host "Creating owner account..." -ForegroundColor Cyan
$accountOutput = & docker compose run --rm --no-deps web bin/tootctl accounts create $AdminUsername --email $AdminEmail --confirmed --role Owner 2>&1
$accountExit = $LASTEXITCODE
$accountText = $accountOutput -join "`n"
if ($accountExit -ne 0 -and $accountText -notmatch "already exists|has already been taken") {
  throw "Owner account creation failed:`n$accountText"
}
if ($accountExit -eq 0) {
  Write-Host $accountText -ForegroundColor Green
  Write-Host "Save the generated owner password now." -ForegroundColor Yellow
} else {
  Write-Warning "Owner account already exists; keeping it and continuing."
  Invoke-Docker -Arguments @("compose", "run", "--rm", "--no-deps", "web", "bin/tootctl", "accounts", "modify", $AdminUsername, "--role", "Owner", "--enable")
}

Write-Host "Closing public registrations..." -ForegroundColor Cyan
Invoke-Docker -Arguments @("compose", "run", "--rm", "--no-deps", "web", "bin/tootctl", "settings", "registrations", "close")

if ([string]::IsNullOrWhiteSpace($TunnelToken)) {
  Write-Host "Starting local stack without Cloudflare Tunnel..." -ForegroundColor Cyan
  Invoke-Docker -Arguments @("compose", "up", "-d")
} else {
  Write-Host "Starting stack with Cloudflare Tunnel..." -ForegroundColor Cyan
  Invoke-Docker -Arguments @("compose", "--profile", "tunnel", "up", "-d")
}

$healthy = $false
for ($attempt = 1; $attempt -le 40; $attempt++) {
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
  throw "Nginx did not become healthy. Run .\status.ps1 and inspect container logs."
}

@(
  "initialized_at=$([DateTime]::UtcNow.ToString('o'))",
  "domain=$Domain",
  "admin_username=$AdminUsername"
) | Set-Content -LiteralPath ".pi-os-initialized" -Encoding UTF8

Write-Host ""
Write-Host "PI OS base deployment is running." -ForegroundColor Green
Write-Host "Local health: http://127.0.0.1:8080/_pi/health"
if ([string]::IsNullOrWhiteSpace($TunnelToken)) {
  Write-Host "Cloudflare is not enabled yet. Follow docs/CLOUDFLARE.md, add the token to .env, then run .\start.ps1." -ForegroundColor Yellow
} else {
  Write-Host "Cloudflare container is enabled. Ensure the Tunnel public hostname points to http://nginx:80." -ForegroundColor Yellow
}
Write-Host "Run .\status.ps1 for the single final smoke check."
