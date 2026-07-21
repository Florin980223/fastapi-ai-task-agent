<#
.SYNOPSIS
    Smoke-tests a running container: GET /health (no key), GET /ready
    (no key), GET / (Web UI, no key), GET /tasks (no key -> 401), GET
    /tasks (valid key -> 200), POST /agent/execute in rule_based mode,
    and - when -SmokeTestApiKey is explicitly supplied, or always under
    -Full - a create/list/delete task lifecycle check under a dedicated,
    isolated smoke-test user.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior
    (Invoke-WebRequest throws on non-2xx responses in 5.1 - there is no
    -SkipHttpErrorCheck there, unlike PowerShell 7+ - so status codes
    are read out of the caught exception's Response object instead).

    Never prints any API key anywhere, including on failure - only
    generic pass/fail messages and HTTP status codes are ever written
    to output.

    IMPORTANT - data safety: the task-lifecycle check creates a real row
    and then deletes it again. Only run this against a disposable
    database - a temporary SQLite file created outside the repository,
    or an explicitly named disposable PostgreSQL volume/database. Never
    point it at a developer's real tasks.db or normal persistent demo
    data (agent_data / postgres_data).

.PARAMETER BaseUrl
    Where the app is reachable. Defaults to http://localhost:8000,
    matching compose.yaml's 127.0.0.1:8000:8000 port binding. Used for
    every check in this script.

.PARAMETER ApiKey
    A valid API key, used only for the pre-existing read-only checks
    (GET /tasks with a key, POST /agent/execute). If omitted, the first
    key is read from the API_KEYS line of -EnvFile (default
    .env.docker) - the key's value is never echoed, only used in an
    HTTP header.

.PARAMETER EnvFile
    Path to a Docker env file containing an API_KEYS=... line, used
    only when -ApiKey isn't supplied directly.

.PARAMETER SmokeTestApiKey
    A dedicated, isolated API key mapped to its own smoke-test user
    (e.g. "smoketest-<random>:smoke_test_user" in the deployment's
    API_KEYS) - used ONLY for the task create/list/delete lifecycle
    check. Has no default and no .env.docker/-EnvFile fallback,
    unlike -ApiKey above: it must always be supplied explicitly by the
    caller. Outside -Full, if it (or -BaseUrl) was not explicitly
    supplied, the task-lifecycle check is skipped with a clear message
    instead of silently running against a default URL or a key pulled
    from a file. Never a real user's key.

.PARAMETER Full
    Strict/deployment mode: requires both -BaseUrl and -SmokeTestApiKey
    to have been explicitly supplied AND non-empty/non-whitespace,
    checked before any other check or HTTP request runs - a deployment
    smoke run that was supposed to exercise the authenticated task
    lifecycle must fail loudly (clear error, non-zero exit) rather than
    silently SKIP that check if a parameter is missing or empty. Always
    passed by the deploy_smoke CI job for exactly this reason.

.EXAMPLE
    .\docker\smoke_test.ps1
.EXAMPLE
    .\docker\smoke_test.ps1 -BaseUrl http://localhost:8000 -ApiKey devkey-alice
.EXAMPLE
    .\docker\smoke_test.ps1 -Full -BaseUrl http://localhost:8000 -ApiKey devkey-alice -SmokeTestApiKey devkey-smoketest
#>

param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$ApiKey,
    [string]$EnvFile = ".env.docker",
    [string]$SmokeTestApiKey,
    [switch]$Full
)

# -Full's pre-flight gate: checked first, before anything else in this
# script (including the -ApiKey/-EnvFile resolution below) - a
# deployment smoke run must never silently skip the authenticated task
# lifecycle check because of a missing or empty parameter. Two-part
# validation, both required: the parameter must have been explicitly
# passed (not just defaulted) AND its value must be non-empty/
# non-whitespace - an explicitly-passed empty string must fail exactly
# like an absent one. Never prints either value, only which parameter is
# missing/unusable.
if ($Full) {
    $baseUrlSupplied = $PSBoundParameters.ContainsKey('BaseUrl') -and -not [string]::IsNullOrWhiteSpace($BaseUrl)
    $smokeKeySupplied = $PSBoundParameters.ContainsKey('SmokeTestApiKey') -and -not [string]::IsNullOrWhiteSpace($SmokeTestApiKey)

    if (-not $baseUrlSupplied -or -not $smokeKeySupplied) {
        Write-Host "ERROR: -Full requires both -BaseUrl and -SmokeTestApiKey to be passed explicitly with non-empty values - refusing to silently skip the authenticated task lifecycle check." -ForegroundColor Red
        if (-not $baseUrlSupplied) {
            Write-Host "  -BaseUrl was not explicitly supplied, or was empty/whitespace." -ForegroundColor Red
        }
        if (-not $smokeKeySupplied) {
            Write-Host "  -SmokeTestApiKey was not explicitly supplied, or was empty/whitespace." -ForegroundColor Red
        }
        exit 1
    }
}

function Get-ApiKeyFromEnvFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    $line = Get-Content $Path | Where-Object { $_ -match '^\s*API_KEYS\s*=' } | Select-Object -First 1
    if (-not $line) {
        return $null
    }

    $value = ($line -split '=', 2)[1].Trim()
    if (-not $value) {
        return $null
    }

    $firstPair = ($value -split ',')[0]
    $key = ($firstPair -split ':')[0].Trim()
    if (-not $key) {
        return $null
    }

    return $key
}

if (-not $ApiKey) {
    $ApiKey = Get-ApiKeyFromEnvFile -Path $EnvFile
}

if (-not $ApiKey) {
    Write-Host "No API key available - pass -ApiKey, or ensure $EnvFile has an API_KEYS=... line." -ForegroundColor Red
    Write-Host "(The key's value is never printed by this script; only its presence is checked here.)"
    exit 1
}

function Invoke-CheckedRequest {
    param(
        [string]$Uri,
        [string]$Method = "GET",
        [hashtable]$Headers,
        [string]$Body,
        [string]$ContentType
    )

    $requestParams = @{
        Uri            = $Uri
        Method         = $Method
        UseBasicParsing = $true
    }
    if ($Headers) { $requestParams.Headers = $Headers }
    if ($Body) { $requestParams.Body = $Body }
    if ($ContentType) { $requestParams.ContentType = $ContentType }

    try {
        $response = Invoke-WebRequest @requestParams
        $responseContentType = $null
        if ($response.Headers -and $response.Headers["Content-Type"]) {
            $responseContentType = $response.Headers["Content-Type"]
        }
        return @{ StatusCode = [int]$response.StatusCode; Content = $response.Content; ResponseContentType = $responseContentType }
    } catch {
        # Windows PowerShell 5.1 throws on non-2xx responses instead of
        # returning them - pull the real status code back out of the
        # exception instead of treating every non-2xx as a hard error.
        # Never inspect/print $_.Exception.Message here: it can include
        # response detail we don't need, and this script's policy is to
        # never print anything beyond a generic pass/fail plus status
        # codes.
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        return @{ StatusCode = $statusCode; Content = $null }
    }
}

$script:allPassed = $true

function Test-Check {
    param(
        [string]$Name,
        [scriptblock]$Body
    )

    $passed = $false
    try {
        $passed = [bool](& $Body)
    } catch {
        $passed = $false
    }

    if ($passed) {
        Write-Host "PASS: $Name" -ForegroundColor Green
    } else {
        Write-Host "FAIL: $Name" -ForegroundColor Red
        $script:allPassed = $false
    }
}

Test-Check "GET /health without an API key returns 200" {
    $result = Invoke-CheckedRequest -Uri "$BaseUrl/health"
    $result.StatusCode -eq 200
}

Test-Check "GET /ready without an API key returns 200" {
    $result = Invoke-CheckedRequest -Uri "$BaseUrl/ready"
    $result.StatusCode -eq 200
}

Test-Check "GET / (Web UI) without an API key returns 200 HTML" {
    $result = Invoke-CheckedRequest -Uri "$BaseUrl/"
    ($result.StatusCode -eq 200) -and ($result.ResponseContentType -like "text/html*")
}

Test-Check "GET /tasks without an API key returns 401" {
    $result = Invoke-CheckedRequest -Uri "$BaseUrl/tasks"
    $result.StatusCode -eq 401
}

Test-Check "GET /tasks with a valid API key returns 200" {
    $result = Invoke-CheckedRequest -Uri "$BaseUrl/tasks" -Headers @{ "X-API-Key" = $ApiKey }
    $result.StatusCode -eq 200
}

Test-Check "POST /agent/execute works in rule_based mode" {
    $body = '{"message": "Add a task to buy milk"}'
    $result = Invoke-CheckedRequest -Uri "$BaseUrl/agent/execute" -Method POST -Headers @{ "X-API-Key" = $ApiKey } -Body $body -ContentType "application/json"
    if ($result.StatusCode -ne 200) {
        return $false
    }
    $data = $result.Content | ConvertFrom-Json
    return [bool]$data.selected_tool
}

$explicitBaseUrl = $PSBoundParameters.ContainsKey('BaseUrl')
$explicitSmokeTestApiKey = $PSBoundParameters.ContainsKey('SmokeTestApiKey')

if (-not $explicitBaseUrl -or -not $explicitSmokeTestApiKey) {
    Write-Host "SKIP: authenticated task create/list/delete lifecycle - refusing to run without both -BaseUrl and -SmokeTestApiKey passed explicitly (no hidden defaults for a check that creates and deletes real data)." -ForegroundColor Yellow
} else {
    # Uniquely identifiable per run, so cleanup below can never be
    # mistaken for - or accidentally touch - any other task, and so a
    # human scanning the smoke-test user's task list can immediately
    # recognize (and, if cleanup ever fails, manually remove) exactly
    # what this script created.
    $uniqueTitle = "smoke-test-$([guid]::NewGuid().ToString())"
    $createdTaskId = $null

    try {
        $createBody = (@{ title = $uniqueTitle } | ConvertTo-Json)
        $createResult = Invoke-CheckedRequest -Uri "$BaseUrl/tasks" -Method POST -Headers @{ "X-API-Key" = $SmokeTestApiKey } -Body $createBody -ContentType "application/json"

        $createdOk = $false
        if ($createResult.StatusCode -eq 201) {
            $createdTask = $createResult.Content | ConvertFrom-Json
            $createdTaskId = $createdTask.id
            $createdOk = [bool]$createdTaskId
        }

        $listedOk = $false
        if ($createdOk) {
            $listResult = Invoke-CheckedRequest -Uri "$BaseUrl/tasks" -Headers @{ "X-API-Key" = $SmokeTestApiKey }
            if ($listResult.StatusCode -eq 200) {
                $tasks = $listResult.Content | ConvertFrom-Json
                $listedOk = [bool]($tasks | Where-Object { $_.id -eq $createdTaskId })
            }
        }

        if ($createdOk -and $listedOk) {
            Write-Host "PASS: authenticated task create + list lifecycle (dedicated smoke-test user)" -ForegroundColor Green
        } else {
            Write-Host "FAIL: authenticated task create + list lifecycle (dedicated smoke-test user)" -ForegroundColor Red
            $script:allPassed = $false
        }
    } finally {
        # Runs even if the create/list assertions above failed - deletes
        # exactly the one task id this run created, never anything else
        # (no bulk delete, no "all tasks for this user").
        if ($createdTaskId) {
            $deleteResult = Invoke-CheckedRequest -Uri "$BaseUrl/tasks/$createdTaskId" -Method DELETE -Headers @{ "X-API-Key" = $SmokeTestApiKey }
            if ($deleteResult.StatusCode -eq 204) {
                Write-Host "PASS: cleanup - smoke-test task $createdTaskId deleted" -ForegroundColor Green
            } else {
                Write-Host "CLEANUP FAILED: smoke-test task $createdTaskId was not deleted - manual cleanup required" -ForegroundColor Red
                $script:allPassed = $false
            }
        }
    }
}

Write-Host ""
if ($script:allPassed) {
    Write-Host "All smoke-test checks passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "One or more smoke-test checks failed." -ForegroundColor Red
    exit 1
}
