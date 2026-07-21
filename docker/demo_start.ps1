<#
.SYNOPSIS
    Starts the isolated "production-like local demo" stack (compose.demo.yaml):
    demo PostgreSQL -> wait healthy -> explicit Alembic migration -> demo
    FastAPI app -> wait for /ready -> print the Web UI URL.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior, matching
    docker/smoke_test.ps1's conventions.

    Idempotent and safe to re-run at any time - including as the "restart
    after a stop" command, or immediately after a fresh `docker/demo_cleanup.ps1`
    teardown. Never prints any API key or the contents of .env.demo.

    Every `docker compose` call in this script uses the exact same explicit
    command base - `--env-file <repo root>\.env.demo -f <repo root>\compose.demo.yaml
    -p fastapi-ai-task-agent-demo` - built once from paths resolved relative
    to this script's own location, never the caller's current directory, and
    never relying on Compose's automatic .env discovery or any inherited
    shell environment variable (DATABASE_URL, API_KEYS, POSTGRES_*, ...).

    Before touching Docker, checks that host ports 8100 (app-demo) and 55434
    (postgres-demo) are available - ownership-aware: a port already held by
    this exact demo stack (Compose project fastapi-ai-task-agent-demo) is
    treated as "our own stack, not a conflict" and this script proceeds
    idempotently; a port held by anything else fails immediately with a
    clear message naming the port and, where safely available, the
    conflicting container's name or process id - this script never stops or
    removes whatever it finds.

.EXAMPLE
    .\docker\demo_start.ps1
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $RepoRoot "compose.demo.yaml"
$EnvFile = Join-Path $RepoRoot ".env.demo"
$ProjectName = "fastapi-ai-task-agent-demo"
$AppPort = 8100
$PostgresPort = 55434
$ReadyUrl = "http://localhost:$AppPort/ready"

function Test-DemoFiles {
    if (-not (Test-Path $ComposeFile)) {
        Write-Host "ERROR: compose.demo.yaml not found at $ComposeFile" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $EnvFile)) {
        Write-Host "ERROR: .env.demo not found at $EnvFile" -ForegroundColor Red
        Write-Host "Copy .env.demo.example to .env.demo and fill in your own values first:" -ForegroundColor Yellow
        Write-Host "  Copy-Item `"$RepoRoot\.env.demo.example`" `"$EnvFile`"" -ForegroundColor Yellow
        Write-Host "See docs/LOCAL_DEMO.md for the full one-time setup, including generating demo API keys." -ForegroundColor Yellow
        exit 1
    }
}

function Get-ComposeArgs {
    return @("--env-file", $EnvFile, "-f", $ComposeFile, "-p", $ProjectName)
}

function Get-PortOwnership {
    param([int]$Port)

    $containerName = (docker ps --filter "publish=$Port" --format "{{.Names}}" 2>$null | Select-Object -First 1)
    if ($containerName) {
        # {{json .Config.Labels}} + ConvertFrom-Json, rather than a Go
        # template `index` call with an embedded double-quoted label key
        # (e.g. '{{index .Config.Labels "com.docker.compose.project"}}') -
        # PowerShell's native-command argument quoting mangles embedded
        # double quotes inside a single-quoted argument, breaking Go
        # template parsing. JSON output sidesteps that entirely.
        $labelsJson = (docker inspect $containerName --format '{{json .Config.Labels}}' 2>$null)
        $label = $null
        if ($labelsJson) {
            $labels = $labelsJson | ConvertFrom-Json
            $label = $labels.'com.docker.compose.project'
        }
        if ($label -eq $ProjectName) {
            return @{ Occupied = $true; OwnedByDemo = $true; Detail = "container '$containerName' (this demo stack)" }
        }
        return @{ Occupied = $true; OwnedByDemo = $false; Detail = "container '$containerName' (a different Docker Compose project)" }
    }

    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listening) {
        $conflictingProcessId = $listening.OwningProcess
        return @{ Occupied = $true; OwnedByDemo = $false; Detail = "a non-Docker process (PID $conflictingProcessId)" }
    }

    return @{ Occupied = $false; OwnedByDemo = $false; Detail = $null }
}

function Test-DemoPorts {
    foreach ($port in @($AppPort, $PostgresPort)) {
        $status = Get-PortOwnership -Port $port
        if ($status.Occupied -and -not $status.OwnedByDemo) {
            Write-Host "ERROR: port $port is already in use by $($status.Detail)." -ForegroundColor Red
            Write-Host "Refusing to start - this script never stops or removes anything it didn't create. Free the port yourself, then re-run." -ForegroundColor Red
            exit 1
        }
    }
}

Test-DemoFiles
Test-DemoPorts

$ComposeArgs = Get-ComposeArgs

Write-Host "Starting postgres-demo and waiting for it to become healthy..." -ForegroundColor Cyan
& docker compose @ComposeArgs up -d --wait postgres-demo
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: postgres-demo failed to start/become healthy." -ForegroundColor Red
    exit 1
}

Write-Host "Applying Alembic migration (explicit, never automatic)..." -ForegroundColor Cyan
& docker compose @ComposeArgs run --rm app-demo python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Alembic migration failed." -ForegroundColor Red
    exit 1
}

Write-Host "Starting app-demo..." -ForegroundColor Cyan
& docker compose @ComposeArgs up -d app-demo
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: app-demo failed to start." -ForegroundColor Red
    exit 1
}

Write-Host "Waiting for GET /ready..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $response = Invoke-WebRequest -Uri $ReadyUrl -UseBasicParsing -TimeoutSec 3
        if ([int]$response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        # Not ready yet - keep polling.
    }
    Start-Sleep -Seconds 1
}

if (-not $ready) {
    Write-Host "ERROR: app-demo did not become ready within 30 seconds. Check logs:" -ForegroundColor Red
    Write-Host "  docker compose --env-file `"$EnvFile`" -f `"$ComposeFile`" -p $ProjectName logs app-demo" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Demo is ready." -ForegroundColor Green
Write-Host "Web UI: http://localhost:$AppPort/" -ForegroundColor Green
Write-Host "Paste a demo API key from .env.demo (API_KEYS=...) into the Web UI - never printed here." -ForegroundColor Yellow
