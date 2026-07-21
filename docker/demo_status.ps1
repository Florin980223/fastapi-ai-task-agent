<#
.SYNOPSIS
    Prints a friendly status summary of the isolated local demo stack
    (compose.demo.yaml): container states plus live /health and /ready
    checks against the running app-demo instance.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior,
    matching docker/smoke_test.ps1's conventions. Read-only - never
    starts, stops, or removes anything. Never prints .env.demo's
    contents or any API key.

.EXAMPLE
    .\docker\demo_status.ps1
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $RepoRoot "compose.demo.yaml"
$EnvFile = Join-Path $RepoRoot ".env.demo"
$ProjectName = "fastapi-ai-task-agent-demo"
$AppPort = 8100

if (-not (Test-Path $ComposeFile)) {
    Write-Host "ERROR: compose.demo.yaml not found at $ComposeFile" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $EnvFile)) {
    Write-Host "ERROR: .env.demo not found at $EnvFile - the demo has never been set up. See docs/LOCAL_DEMO.md." -ForegroundColor Red
    exit 1
}

$ComposeArgs = @("--env-file", $EnvFile, "-f", $ComposeFile, "-p", $ProjectName)

Write-Host "=== Demo containers (project $ProjectName) ===" -ForegroundColor Cyan
& docker compose @ComposeArgs ps

Write-Host ""
Write-Host "=== Live checks ===" -ForegroundColor Cyan

try {
    $health = Invoke-WebRequest -Uri "http://localhost:$AppPort/health" -UseBasicParsing -TimeoutSec 3
    Write-Host "GET /health: $([int]$health.StatusCode)" -ForegroundColor Green
} catch {
    Write-Host "GET /health: unreachable" -ForegroundColor Red
}

try {
    $ready = Invoke-WebRequest -Uri "http://localhost:$AppPort/ready" -UseBasicParsing -TimeoutSec 3
    Write-Host "GET /ready: $([int]$ready.StatusCode) $($ready.Content)" -ForegroundColor Green
} catch {
    $statusCode = $null
    if ($_.Exception.Response) {
        $statusCode = [int]$_.Exception.Response.StatusCode
    }
    if ($statusCode) {
        Write-Host "GET /ready: $statusCode (not ready)" -ForegroundColor Yellow
    } else {
        Write-Host "GET /ready: unreachable" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Web UI: http://localhost:$AppPort/" -ForegroundColor Cyan
