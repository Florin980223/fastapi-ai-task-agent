<#
.SYNOPSIS
    Smoke-tests a running container: GET /health (no key), GET /tasks
    (no key -> 401), GET /tasks (valid key -> 200), and POST
    /agent/execute in rule_based mode.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior
    (Invoke-WebRequest throws on non-2xx responses in 5.1 - there is no
    -SkipHttpErrorCheck there, unlike PowerShell 7+ - so status codes
    are read out of the caught exception's Response object instead).

    Never prints the API key anywhere, including on failure - only
    generic pass/fail messages and HTTP status codes are ever written
    to output.

.PARAMETER BaseUrl
    Where the app is reachable. Defaults to http://localhost:8000,
    matching compose.yaml's 127.0.0.1:8000:8000 port binding.

.PARAMETER ApiKey
    A valid API key. If omitted, the first key is read from the
    API_KEYS line of -EnvFile (default .env.docker) - the key's value
    is never echoed, only used in an HTTP header.

.PARAMETER EnvFile
    Path to a Docker env file containing an API_KEYS=... line, used
    only when -ApiKey isn't supplied directly.

.EXAMPLE
    .\docker\smoke_test.ps1
.EXAMPLE
    .\docker\smoke_test.ps1 -BaseUrl http://localhost:8000 -ApiKey devkey-alice
#>

param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$ApiKey,
    [string]$EnvFile = ".env.docker"
)

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
        return @{ StatusCode = [int]$response.StatusCode; Content = $response.Content }
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

Write-Host ""
if ($script:allPassed) {
    Write-Host "All smoke-test checks passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "One or more smoke-test checks failed." -ForegroundColor Red
    exit 1
}
