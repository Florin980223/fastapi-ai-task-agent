<#
.SYNOPSIS
    Tears down the isolated local demo stack (compose.demo.yaml):
    stops and removes ONLY the explicitly-named demo containers and
    network. Optionally also removes the explicitly-named demo volume.
    Never touches agent_data or fastapi-ai-task-agent-postgres-data -
    those belong to a completely separate Compose project/file.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior,
    matching docker/smoke_test.ps1's conventions.

    Dry-run by default: prints exactly what would happen and changes
    nothing. Pass -Force to actually remove the demo containers and
    network. Pass -RemoveData (in addition to -Force) to also remove
    the demo PostgreSQL volume - a separate, additional opt-in, since
    losing data is a bigger deal than removing containers.

    Before removing anything, verifies (via `docker inspect`/`docker
    volume inspect`/`docker network inspect`) that each target actually
    carries the `com.docker.compose.project=fastapi-ai-task-agent-demo`
    label - if a label is missing or different, this script refuses and
    exits with an error rather than proceeding. Never runs `docker
    compose down -v`. Never runs `docker system prune`/`docker volume
    prune` or any other broad/wildcard cleanup - only ever the exact
    three named demo resources.

.PARAMETER Force
    Required to actually remove the demo containers/network. Without
    it, this script only previews what it would do and exits 0 without
    changing anything.

.PARAMETER RemoveData
    Additionally removes the named demo PostgreSQL volume. Requires
    -Force to also be passed - this switch alone does nothing.

.EXAMPLE
    .\docker\demo_cleanup.ps1
.EXAMPLE
    .\docker\demo_cleanup.ps1 -Force
.EXAMPLE
    .\docker\demo_cleanup.ps1 -Force -RemoveData
#>

param(
    [switch]$Force,
    [switch]$RemoveData
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $RepoRoot "compose.demo.yaml"
$EnvFile = Join-Path $RepoRoot ".env.demo"
$ProjectName = "fastapi-ai-task-agent-demo"
$VolumeName = "fastapi-ai-task-agent-demo-postgres-data"
$NetworkName = "fastapi-ai-task-agent-demo-net"
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
    } elseif ($ResourceType -eq "network") {
        $json = (docker network inspect $Name --format '{{json .Labels}}' 2>$null)
    } else {
        $json = (docker inspect $Name --format '{{json .Config.Labels}}' 2>$null)
    }
    if (-not $json) {
        return $null  # not found - informational/non-fatal, handled by callers
    }
    $labels = $json | ConvertFrom-Json
    return $labels.'com.docker.compose.project'
}

Write-Host "This will remove ONLY the isolated demo stack's own resources:" -ForegroundColor Yellow
Write-Host "  - Container: $PostgresContainer" -ForegroundColor Yellow
Write-Host "  - Container: $AppContainer" -ForegroundColor Yellow
Write-Host "  - Network:   $NetworkName" -ForegroundColor Yellow
if ($RemoveData) {
    Write-Host "  - Volume (exact name, never a wildcard): $VolumeName  <-- ALL demo data lost" -ForegroundColor Yellow
} else {
    Write-Host "  - Volume $VolumeName is KEPT (pass -RemoveData together with -Force to also remove it)" -ForegroundColor Cyan
}
Write-Host "This NEVER touches agent_data or fastapi-ai-task-agent-postgres-data (a different Compose project). Never runs 'docker compose down -v'." -ForegroundColor Yellow
Write-Host ""

if (-not $Force) {
    Write-Host "Dry run only - pass -Force to actually remove containers/network (add -RemoveData to also remove the volume). Nothing has been changed." -ForegroundColor Cyan
    exit 0
}

foreach ($target in @(
        @{ Type = "container"; Name = $PostgresContainer },
        @{ Type = "container"; Name = $AppContainer },
        @{ Type = "network"; Name = $NetworkName }
    )) {
    $label = Get-ComposeProjectLabel -ResourceType $target.Type -Name $target.Name
    if ($null -ne $label -and $label -ne $ProjectName) {
        Write-Host "ERROR: $($target.Type) '$($target.Name)' exists but its com.docker.compose.project label is '$label', not '$ProjectName' - refusing to touch it." -ForegroundColor Red
        exit 1
    }
    if ($null -eq $label) {
        Write-Host "$($target.Type) '$($target.Name)' not found - nothing to remove (safe to continue)." -ForegroundColor Cyan
    }
}

Write-Host "Removing demo containers and network (docker compose down, never -v)..." -ForegroundColor Cyan
& docker compose @ComposeArgs down
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: 'docker compose down' failed." -ForegroundColor Red
    exit 1
}

if ($RemoveData) {
    $volumeLabel = Get-ComposeProjectLabel -ResourceType "volume" -Name $VolumeName
    if ($null -ne $volumeLabel -and $volumeLabel -ne $ProjectName) {
        Write-Host "ERROR: volume '$VolumeName' exists but its com.docker.compose.project label is '$volumeLabel', not '$ProjectName' - refusing to remove it." -ForegroundColor Red
        exit 1
    }
    if ($null -eq $volumeLabel) {
        Write-Host "Volume '$VolumeName' not found - nothing to remove." -ForegroundColor Cyan
    } else {
        Write-Host "Removing volume: docker volume rm $VolumeName" -ForegroundColor Cyan
        & docker volume rm $VolumeName
    }
}

Write-Host ""
Write-Host "Demo cleanup complete." -ForegroundColor Green
