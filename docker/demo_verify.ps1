<#
.SYNOPSIS
    Focused verification for the isolated local demo: reuses the
    existing, unmodified docker/smoke_test.ps1 -Full for baseline
    checks, then proves per-user isolation (tasks + run history) between
    two dedicated demo identities, and exercises the clarification and
    destructive-confirmation flows.

.DESCRIPTION
    Written against Windows PowerShell 5.1 syntax/cmdlet behavior,
    matching docker/smoke_test.ps1's conventions. Never prints either
    API key. All three parameters are required with no defaults -
    refuses to run with anything unspecified, since this script creates
    and deletes real rows.

.PARAMETER BaseUrl
    Where the demo app is reachable, e.g. http://localhost:8100. Required.

.PARAMETER DemoApiKeyA
    API key for the first dedicated demo identity (e.g. demo_user_a).
    Required.

.PARAMETER DemoApiKeyB
    API key for the second dedicated demo identity (e.g. demo_user_b).
    Required.

.EXAMPLE
    .\docker\demo_verify.ps1 -BaseUrl http://localhost:8100 -DemoApiKeyA <key-a> -DemoApiKeyB <key-b>
#>

param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$DemoApiKeyA,
    [Parameter(Mandatory = $true)][string]$DemoApiKeyB
)

$script:allPassed = $true

function Test-Check {
    param([string]$Name, [scriptblock]$Body)
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

function Invoke-Demo {
    param(
        [string]$Uri,
        [string]$Method = "GET",
        [string]$ApiKey,
        [string]$Body,
        [string]$ContentType
    )
    $requestParams = @{
        Uri             = $Uri
        Method          = $Method
        UseBasicParsing = $true
        Headers         = @{ "X-API-Key" = $ApiKey }
    }
    if ($Body) { $requestParams.Body = $Body }
    if ($ContentType) { $requestParams.ContentType = $ContentType }
    try {
        $response = Invoke-WebRequest @requestParams
        return @{ StatusCode = [int]$response.StatusCode; Content = $response.Content }
    } catch {
        $statusCode = $null
        $content = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $content = $reader.ReadToEnd()
            } catch {
                $content = $null
            }
        }
        return @{ StatusCode = $statusCode; Content = $content }
    }
}

Write-Host "=== Baseline checks (reusing docker/smoke_test.ps1 -Full, unmodified) ===" -ForegroundColor Cyan
& "$PSScriptRoot\smoke_test.ps1" -Full -BaseUrl $BaseUrl -ApiKey $DemoApiKeyA -SmokeTestApiKey $DemoApiKeyA
if ($LASTEXITCODE -ne 0) {
    $script:allPassed = $false
}

Write-Host ""
Write-Host "=== Two-identity isolation checks ===" -ForegroundColor Cyan

$uniqueTitleA = "isolation-test-a-$([guid]::NewGuid().ToString())"
$createdA = Invoke-Demo -Uri "$BaseUrl/tasks" -Method POST -ApiKey $DemoApiKeyA -ContentType "application/json" -Body (@{ title = $uniqueTitleA } | ConvertTo-Json)
$taskIdA = $null
if ($createdA.StatusCode -eq 201) {
    $taskIdA = ($createdA.Content | ConvertFrom-Json).id
}

try {
    Test-Check "Task created by identity A is visible to identity A" {
        $listA = Invoke-Demo -Uri "$BaseUrl/tasks" -ApiKey $DemoApiKeyA
        $tasks = $listA.Content | ConvertFrom-Json
        [bool]($tasks | Where-Object { $_.id -eq $taskIdA })
    }

    Test-Check "Task created by identity A is NOT visible to identity B" {
        $listB = Invoke-Demo -Uri "$BaseUrl/tasks" -ApiKey $DemoApiKeyB
        $tasks = $listB.Content | ConvertFrom-Json
        -not ($tasks | Where-Object { $_.id -eq $taskIdA })
    }

    Test-Check "Identity B cannot fetch identity A's task by id (404, not 403)" {
        $get = Invoke-Demo -Uri "$BaseUrl/tasks/$taskIdA" -ApiKey $DemoApiKeyB
        $get.StatusCode -eq 404
    }
} finally {
    if ($taskIdA) {
        Invoke-Demo -Uri "$BaseUrl/tasks/$taskIdA" -Method DELETE -ApiKey $DemoApiKeyA | Out-Null
    }
}

Write-Host ""
Write-Host "=== Agent-run isolation and history ===" -ForegroundColor Cyan

$runResult = Invoke-Demo -Uri "$BaseUrl/agent/execute" -Method POST -ApiKey $DemoApiKeyA -ContentType "application/json" -Body '{"message": "list my tasks"}'
$runIdA = $null
if ($runResult.StatusCode -eq 200) {
    $runIdA = ($runResult.Content | ConvertFrom-Json).run_id
}

Test-Check "Run created by identity A appears in identity A's run history" {
    $runsA = Invoke-Demo -Uri "$BaseUrl/agent/runs" -ApiKey $DemoApiKeyA
    $runs = $runsA.Content | ConvertFrom-Json
    [bool]($runs | Where-Object { $_.run_id -eq $runIdA })
}

Test-Check "Run created by identity A does NOT appear in identity B's run history" {
    $runsB = Invoke-Demo -Uri "$BaseUrl/agent/runs" -ApiKey $DemoApiKeyB
    $runs = $runsB.Content | ConvertFrom-Json
    -not ($runs | Where-Object { $_.run_id -eq $runIdA })
}

Test-Check "Identity B cannot fetch identity A's run by id (404, not 403)" {
    $get = Invoke-Demo -Uri "$BaseUrl/agent/runs/$runIdA" -ApiKey $DemoApiKeyB
    $get.StatusCode -eq 404
}

Test-Check "Identity A's own run detail includes step-level history" {
    $get = Invoke-Demo -Uri "$BaseUrl/agent/runs/$runIdA" -ApiKey $DemoApiKeyA
    if ($get.StatusCode -ne 200) { return $false }
    $detail = $get.Content | ConvertFrom-Json
    $detail.steps.Count -gt 0
}

Write-Host ""
Write-Host "=== Clarification flow (rule_based, offline) ===" -ForegroundColor Cyan

Test-Check "An incomplete create-task instruction triggers clarification" {
    $result = Invoke-Demo -Uri "$BaseUrl/agent/execute" -Method POST -ApiKey $DemoApiKeyA -ContentType "application/json" -Body '{"message": "create a task"}'
    if ($result.StatusCode -ne 200) { return $false }
    $data = $result.Content | ConvertFrom-Json
    ($data.needs_clarification -eq $true) -and [bool]$data.clarification_question
}

Write-Host ""
Write-Host "=== Destructive-confirmation flow ===" -ForegroundColor Cyan

$confirmTitle = "confirmation-test-$([guid]::NewGuid().ToString())"
$createdForDeletion = Invoke-Demo -Uri "$BaseUrl/tasks" -Method POST -ApiKey $DemoApiKeyA -ContentType "application/json" -Body (@{ title = $confirmTitle } | ConvertTo-Json)
$deleteTaskId = $null
if ($createdForDeletion.StatusCode -eq 201) {
    $deleteTaskId = ($createdForDeletion.Content | ConvertFrom-Json).id
}

try {
    $conversationId = [guid]::NewGuid().ToString()

    Test-Check "Requesting deletion triggers a pending confirmation" {
        $body = @{ message = "delete task $deleteTaskId"; conversation_id = $conversationId } | ConvertTo-Json
        $result = Invoke-Demo -Uri "$BaseUrl/agent/execute" -Method POST -ApiKey $DemoApiKeyA -ContentType "application/json" -Body $body
        if ($result.StatusCode -ne 200) { return $false }
        $data = $result.Content | ConvertFrom-Json
        ($data.needs_confirmation -eq $true) -and [bool]$data.confirmation_question
    }

    Test-Check "Confirming with 'yes' actually deletes the task" {
        $body = @{ message = "yes"; conversation_id = $conversationId } | ConvertTo-Json
        Invoke-Demo -Uri "$BaseUrl/agent/execute" -Method POST -ApiKey $DemoApiKeyA -ContentType "application/json" -Body $body | Out-Null
        $get = Invoke-Demo -Uri "$BaseUrl/tasks/$deleteTaskId" -ApiKey $DemoApiKeyA
        $get.StatusCode -eq 404
    }
    $deleteTaskId = $null  # already deleted by the confirmed flow above - finally below must not double-delete
} finally {
    if ($deleteTaskId) {
        Invoke-Demo -Uri "$BaseUrl/tasks/$deleteTaskId" -Method DELETE -ApiKey $DemoApiKeyA | Out-Null
    }
}

Write-Host ""
if ($script:allPassed) {
    Write-Host "All demo verification checks passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "One or more demo verification checks failed." -ForegroundColor Red
    exit 1
}
