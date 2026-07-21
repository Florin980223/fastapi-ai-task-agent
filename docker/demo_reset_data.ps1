<#
.SYNOPSIS
    Wipes and recreates ONLY the isolated demo PostgreSQL volume
    (fastapi-ai-task-agent-demo-postgres-data), then brings the demo
    stack back up clean (migrated, ready). Never touches agent_data or
    fastapi-ai-task-agent-postgres-data - those belong to a completely
    separate Compose project/file.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior,
    matching docker/smoke_test.ps1's conventions.

    Dry-run by default: prints exactly what would happen and changes
    nothing. Pass -Force to actually perform the reset.

    Before removing anything, verifies (via `docker inspect`/`docker
    volume inspect`) that the target container and volume actually carry
    the `com.docker.compose.project=fastapi-ai-task-agent-demo` label -
    if a label is missing or different, this script refuses and exits
    with an error rather than proceeding. The volume name itself is a
    single hard-coded constant, never accepted as a parameter and never
    matched via a wildcard/pattern - there is no input that could
    redirect this at the wrong resource.

    Never runs `docker compose down -v`. Never prints the contents of
    .env.demo or any API key. This also wipes any data
    docker/demo_seed.py inserted - that's expected.

.PARAMETER Force
    Required to actually perform the reset. Without it, this script only
    previews what it would do and exits 0 without changing anything.

.EXAMPLE
    .\docker\demo_reset_data.ps1
.EXAMPLE
    .\docker\demo_reset_data.ps1 -Force
#>

param(
    [switch]$Force
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $RepoRoot "compose.demo.yaml"
$EnvFile = Join-Path $RepoRoot ".env.demo"
$ProjectName = "fastapi-ai-task-agent-demo"
$VolumeName = "fastapi-ai-task-agent-demo-postgres-data"
$PostgresContainer = "fastapi-ai-task-agent-demo-postgres-demo-1"
$AppContainer = "fastapi-ai-task-agent-demo-app-demo-1"

if (-not (Test-Path $ComposeFile)) {
    Write-Host "ERROR: compose.demo.yaml not found at $ComposeFile" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $EnvFile)) {
    Write-Host "ERROR: .env.demo not found at $EnvFile - the demo has never been set up. See docs/LOCAL_DEMO.md." -ForegroundColor Red
    exit 1
}

$ComposeArgs = @("--env-file", $EnvFile, "-f", $ComposeFile, "-p", $ProjectName)

function Get-ComposeProjectLabel {
    param([string]$ResourceType, [string]$Name)

    if ($ResourceType -eq "volume") {
        $json = (docker volume inspect $Name --format '{{json .Labels}}' 2>$null)
    } else {
        $json = (docker inspect $Name --format '{{json .Config.Labels}}' 2>$null)
    }
    if (-not $json) {
        return $null  # not found - informational/non-fatal, handled by callers
    }
    $labels = $json | ConvertFrom-Json
    return $labels.'com.docker.compose.project'
}

Write-Host "This will reset ONLY the isolated demo database:" -ForegroundColor Yellow
Write-Host "  - Stop and remove container: $PostgresContainer" -ForegroundColor Yellow
Write-Host "  - Remove volume (exact name, never a wildcard): $VolumeName" -ForegroundColor Yellow
Write-Host "  - Recreate the volume, wait for postgres-demo healthy, re-run 'alembic upgrade head'" -ForegroundColor Yellow
Write-Host "  - Restart app-demo" -ForegroundColor Yellow
Write-Host "This wipes ALL demo data (tasks, conversation state, run history, any seeded rows)." -ForegroundColor Yellow
Write-Host "This NEVER touches agent_data or fastapi-ai-task-agent-postgres-data (a different Compose project)." -ForegroundColor Yellow
Write-Host ""

if (-not $Force) {
    Write-Host "Dry run only - pass -Force to actually perform this reset. Nothing has been changed." -ForegroundColor Cyan
    exit 0
}

$postgresLabel = Get-ComposeProjectLabel -ResourceType "container" -Name $PostgresContainer
if ($null -ne $postgresLabel -and $postgresLabel -ne $ProjectName) {
    Write-Host "ERROR: container '$PostgresContainer' exists but its com.docker.compose.project label is '$postgresLabel', not '$ProjectName' - refusing to touch it." -ForegroundColor Red
    exit 1
}
if ($null -eq $postgresLabel) {
    Write-Host "Container '$PostgresContainer' not found - nothing to stop (safe to continue)." -ForegroundColor Cyan
}

$volumeLabel = Get-ComposeProjectLabel -ResourceType "volume" -Name $VolumeName
if ($null -ne $volumeLabel -and $volumeLabel -ne $ProjectName) {
    Write-Host "ERROR: volume '$VolumeName' exists but its com.docker.compose.project label is '$volumeLabel', not '$ProjectName' - refusing to remove it." -ForegroundColor Red
    exit 1
}
if ($null -eq $volumeLabel) {
    Write-Host "Volume '$VolumeName' not found - nothing to remove (safe to continue)." -ForegroundColor Cyan
}

Write-Host "Stopping and removing postgres-demo..." -ForegroundColor Cyan
& docker compose @ComposeArgs stop postgres-demo
& docker compose @ComposeArgs rm -f postgres-demo

if ($null -ne $volumeLabel) {
    Write-Host "Removing volume: docker volume rm $VolumeName" -ForegroundColor Cyan
    & docker volume rm $VolumeName
}

Write-Host "Recreating postgres-demo and waiting for it to become healthy..." -ForegroundColor Cyan
& docker compose @ComposeArgs up -d --wait postgres-demo
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: postgres-demo failed to start/become healthy after reset." -ForegroundColor Red
    exit 1
}

Write-Host "Re-applying Alembic migration (explicit, never automatic)..." -ForegroundColor Cyan
& docker compose @ComposeArgs run --rm app-demo python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Alembic migration failed after reset." -ForegroundColor Red
    exit 1
}

Write-Host "Restarting app-demo..." -ForegroundColor Cyan
& docker compose @ComposeArgs up -d app-demo
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: app-demo failed to restart after reset." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Demo data reset complete. Run docker\demo_status.ps1 to confirm readiness." -ForegroundColor Green
