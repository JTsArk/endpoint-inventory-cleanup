# Shared helpers for deleting Trend Vision One endpoints from Endpoint
# Inventory. Dot-sourced by both Get-OfflineW11Endpoints.ps1 (offers to
# delete immediately after listing offline endpoints) and
# Remove-OfflineEndpoints.ps1 (standalone re-run against a previously-saved
# CSV).
#
# API used: POST /v3.0/endpointSecurity/endpoints/delete, GET
# /v3.0/endpointSecurity/tasks/{id}  (Endpoint Security -> Remove endpoints)

$script:DeletePath = "/v3.0/endpointSecurity/endpoints/delete"
$script:TaskPathTemplate = "/v3.0/endpointSecurity/tasks/{0}"

$script:DeleteBatchSize = 1000   # API max items per delete call

$script:PollIntervalSeconds = 5
$script:PollTimeoutSeconds = 120

# Retry/backoff for throttled (429) or transient (5xx) API responses.
$script:DeleteMaxRetries = 5
$script:DeleteBackoffBaseSeconds = 1.0
$script:DeleteRetryableStatusCodes = 429, 500, 502, 503, 504

# Invoke-RestMethod with retry + exponential backoff on 429/5xx. Honors the
# Retry-After header when the API sends one; otherwise backs off exponentially
# (1s, 2s, 4s, ...) with a little jitter to avoid retry storms. Non-retryable
# errors are re-thrown immediately for the caller to handle.
function Invoke-RestMethodWithBackoff {
    param(
        [string]$Uri,
        [hashtable]$Headers,
        [string]$Method = "Get",
        $Body = $null,
        [int]$TimeoutSec = 60
    )

    for ($attempt = 0; $attempt -le $script:DeleteMaxRetries; $attempt++) {
        try {
            if ($null -ne $Body) {
                return Invoke-RestMethod -Uri $Uri -Headers $Headers -Method $Method -Body $Body -TimeoutSec $TimeoutSec
            }
            return Invoke-RestMethod -Uri $Uri -Headers $Headers -Method $Method -TimeoutSec $TimeoutSec
        } catch {
            $response = $_.Exception.Response
            $status = if ($response) { [int]$response.StatusCode } else { $null }

            if (-not $status -or $script:DeleteRetryableStatusCodes -notcontains $status -or $attempt -eq $script:DeleteMaxRetries) {
                throw
            }

            $delay = $null
            $retryAfter = $response.Headers.RetryAfter
            if ($retryAfter) {
                if ($retryAfter.Delta) {
                    $delay = $retryAfter.Delta.TotalSeconds
                } elseif ($retryAfter.Date) {
                    $delay = ($retryAfter.Date - [datetimeoffset]::UtcNow).TotalSeconds
                }
            }
            if (-not $delay -or $delay -le 0) {
                $delay = $script:DeleteBackoffBaseSeconds * [math]::Pow(2, $attempt) + (Get-Random -Minimum 0.0 -Maximum 0.5)
            }

            Write-Host ("  got {0}, retrying in {1:N1}s (attempt {2}/{3})" -f $status, $delay, ($attempt + 1), $script:DeleteMaxRetries)
            Start-Sleep -Seconds $delay
        }
    }
}

# POST one batch (<=1000 items) to /endpoints/delete. Returns a list of
# per-item objects aligned with $Batch: @{ taskId; error }.
function Submit-DeleteBatch {
    param($BaseUrl, [hashtable]$Headers, $Batch)

    $uri = "$BaseUrl$script:DeletePath"
    $body = $Batch | ForEach-Object { @{ agentGuid = $_.agentGuid } }
    $bodyJson = $body | ConvertTo-Json -AsArray -Depth 3

    try {
        $resp = Invoke-RestMethodWithBackoff -Uri $uri -Headers $Headers -Method Post -Body $bodyJson -TimeoutSec 60
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        Write-Error "API error $status submitting delete batch: $($_.Exception.Message)"
        exit 1
    }

    $results = @($resp)
    if ($results.Count -ne $Batch.Count) {
        Write-Error "API returned $($results.Count) results for a batch of $($Batch.Count) -- cannot reliably match results to endpoints."
        exit 1
    }

    foreach ($item in $results) {
        if ($item.status -eq 202) {
            $opLocation = ($item.headers | Where-Object { $_.name -eq "Operation-Location" } | Select-Object -First 1).value
            $taskId = if ($opLocation) { ($opLocation -split "/")[-1] } else { $null }
            [pscustomobject]@{ taskId = $taskId; error = $null }
        } else {
            $err = $item.body.error
            [pscustomobject]@{ taskId = $null; error = ("{0} {1}: {2}" -f $item.status, $err.code, $err.message).Trim() }
        }
    }
}

# Poll a delete task until it reaches a terminal status or times out.
function Wait-DeleteTask {
    param($BaseUrl, [hashtable]$Headers, [string]$TaskId)

    $uri = "$BaseUrl$($script:TaskPathTemplate -f $TaskId)"
    $deadline = (Get-Date).AddSeconds($script:PollTimeoutSeconds)

    while ($true) {
        try {
            $body = Invoke-RestMethodWithBackoff -Uri $uri -Headers $Headers -Method Get -TimeoutSec 60
        } catch {
            $status = $_.Exception.Response.StatusCode.value__
            return @{ status = "unknown"; errorMessage = "$status`: $($_.Exception.Message)" }
        }

        if ($body.status -in @("succeeded", "failed")) {
            $errorMessage = if ($body.status -eq "failed") { $body.error.message } else { "" }
            return @{ status = $body.status; errorMessage = $errorMessage }
        }

        if ((Get-Date) -ge $deadline) {
            return @{ status = "timeout"; errorMessage = "still '$($body.status)' after $($script:PollTimeoutSeconds)s" }
        }

        Start-Sleep -Seconds $script:PollIntervalSeconds
    }
}

# Interactive confirm-then-delete flow, shared by the puller (called right
# after listing, using the same in-memory endpoint list) and the standalone
# delete script (called after loading a saved CSV). $Endpoints is an array
# of objects with endpointName + agentGuid properties, already in memory.
# Returns $true if a delete was attempted (regardless of outcome), $false if
# the user declined, the session isn't interactive, or the token is missing.
function Invoke-DeleteFlow {
    param(
        [Parameter(Mandatory)] $Endpoints,
        [Parameter(Mandatory)] [string]$BaseUrl,
        [string]$Token,
        [Parameter(Mandatory)] [string]$OutputResultsCsv,
        [switch]$SkipFirstPrompt
    )

    $Endpoints = @($Endpoints)
    if ($Endpoints.Count -eq 0) { return $false }

    # A destructive action gated on typed confirmation must never run against
    # a non-interactive stdin (cron, CI, redirected input) -- Read-Host would
    # either throw or silently consume unrelated redirected data. Bail out
    # safely instead, regardless of -SkipFirstPrompt.
    if ([Console]::IsInputRedirected) {
        Write-Host "`nNon-interactive session detected; skipping the delete prompt for these $($Endpoints.Count) endpoint(s). Re-run interactively (e.g. Remove-OfflineEndpoints.ps1 -Verify) to delete them."
        return $false
    }

    if (-not $SkipFirstPrompt) {
        $answer = Read-Host "`nDelete these $($Endpoints.Count) endpoint(s) now? Type 'yes' to continue (anything else exits with no changes)"
        if ($answer.Trim().ToLower() -ne "yes") {
            Write-Host "No changes made."
            return $false
        }
    }

    if ([string]::IsNullOrWhiteSpace($Token)) {
        Write-Host "`nERROR: set the TMV1_TOKEN environment variable (or pass -Token) to your Vision One API key."
        return $false
    }

    Write-Host ("`nThe following {0} endpoint(s) will be DELETED from Endpoint Inventory:" -f $Endpoints.Count)
    foreach ($ep in $Endpoints) {
        Write-Host ("  {0,-30} (agentGuid {1})" -f $ep.endpointName, $ep.agentGuid)
    }

    $confirmation = Read-Host "`nType 'yes' to proceed"
    if ($confirmation.Trim().ToLower() -ne "yes") {
        Write-Host "Aborted. No endpoints were deleted."
        return $false
    }

    $headers = @{
        Authorization  = "Bearer $Token"
        "Content-Type" = "application/json;charset=utf-8"
    }

    # Submit in batches (API max 1000 items/call), tracking a result row per endpoint.
    $submitResults = [System.Collections.Generic.List[object]]::new()
    for ($i = 0; $i -lt $Endpoints.Count; $i += $script:DeleteBatchSize) {
        $batch = $Endpoints[$i..([Math]::Min($i + $script:DeleteBatchSize, $Endpoints.Count) - 1)]
        $batchResults = Submit-DeleteBatch -BaseUrl $BaseUrl -Headers $headers -Batch $batch
        foreach ($r in $batchResults) { $submitResults.Add($r) }
    }

    # Poll each accepted task to a terminal status, printing progress by name.
    Write-Host ""
    $finalResults = [System.Collections.Generic.List[object]]::new()
    for ($i = 0; $i -lt $Endpoints.Count; $i++) {
        $ep = $Endpoints[$i]
        $submitted = $submitResults[$i]

        if ($submitted.error) {
            Write-Host ("  {0,-30} -> submit failed: {1}" -f $ep.endpointName, $submitted.error)
            $finalResults.Add([pscustomobject]@{
                endpointName = $ep.endpointName
                agentGuid    = $ep.agentGuid
                taskId       = ""
                finalStatus  = "not_submitted"
                errorMessage = $submitted.error
            })
            continue
        }

        $result = Wait-DeleteTask -BaseUrl $BaseUrl -Headers $headers -TaskId $submitted.taskId
        $suffix = if ($result.errorMessage) { ": $($result.errorMessage)" } else { "" }
        Write-Host ("  {0,-30} -> task {1}{2}" -f $ep.endpointName, $result.status, $suffix)
        $finalResults.Add([pscustomobject]@{
            endpointName = $ep.endpointName
            agentGuid    = $ep.agentGuid
            taskId       = $submitted.taskId
            finalStatus  = $result.status
            errorMessage = $result.errorMessage
        })
    }

    $finalResults | Export-Csv -Path $OutputResultsCsv -Encoding utf8

    $succeeded = ($finalResults | Where-Object { $_.finalStatus -eq "succeeded" }).Count
    $failed = ($finalResults | Where-Object { $_.finalStatus -in @("failed", "not_submitted", "unknown") }).Count
    $timedOut = ($finalResults | Where-Object { $_.finalStatus -eq "timeout" }).Count

    Write-Host ("`n{0} succeeded, {1} failed, {2} timed out. Wrote {3} rows to {4}" -f `
        $succeeded, $failed, $timedOut, $finalResults.Count, $OutputResultsCsv)

    return $true
}
