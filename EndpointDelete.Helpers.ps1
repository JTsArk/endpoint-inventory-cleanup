# Shared helpers for deleting Trend Vision One endpoints from Endpoint
# Inventory. Dot-sourced by both Get-OfflineEndpoints.ps1 (offers to
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

# Human-readable summary written to the "actionTaken" results-CSV column, keyed by finalStatus.
$script:ActionTakenByStatus = @{
    succeeded      = "Deleted from Endpoint Inventory"
    failed         = "Delete failed"
    timeout        = "Delete timed out"
    not_submitted  = "Not submitted (API error)"
    unknown        = "Delete status unknown (poll failed)"
}

# Retry/backoff for throttled (429) or transient (5xx) API responses.
$script:DeleteMaxRetries = 5
$script:DeleteBackoffBaseSeconds = 1.0
$script:DeleteRetryableStatusCodes = 429, 500, 502, 503, 504

# Invoke-RestMethod with retry + exponential backoff. Honors the Retry-After
# header when the API sends one; otherwise backs off exponentially (1s, 2s,
# 4s, ...) with a little jitter to avoid retry storms. Non-retryable errors
# are re-thrown immediately for the caller to handle.
#
# -RetryableStatusCodes defaults to 429/5xx. This default is also used for
# the delete-submission POST (see Submit-DeleteBatch): a 500/502/503/504
# there can mean the request already reached and was processed by the
# backend, with only the *response* lost in transit -- but live-verified
# behavior confirms resubmitting is safe even then. An agentGuid the first
# attempt actually deleted comes back "404 NotFound" on retry (not a
# duplicate action), and the API documents a "TaskError: Delete task
# already in progress" conflict for one still mid-flight. A submission
# that still fails after exhausting retries doesn't abort the run either --
# see Submit-DeleteBatch, which records a not_submitted row per endpoint in
# that batch instead.
function Invoke-RestMethodWithBackoff {
    param(
        [string]$Uri,
        [hashtable]$Headers,
        [string]$Method = "Get",
        $Body = $null,
        [int]$TimeoutSec = 60,
        [int[]]$RetryableStatusCodes = $script:DeleteRetryableStatusCodes
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

            if (-not $status -or $RetryableStatusCodes -notcontains $status -or $attempt -eq $script:DeleteMaxRetries) {
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
# per-item objects aligned with $Batch: @{ taskId; httpStatus; errorCode; errorMessage }.
# httpStatus/errorCode/errorMessage are kept as separate fields (rather than one
# packed string) so a short/empty API message doesn't collapse into something
# like "400 :" -- which some CSV viewers (Excel) misread as a duration.
#
# A batch-level failure (the submission POST itself errors, or the response
# doesn't line up 1:1 with $Batch) does NOT abort the run -- it returns a
# synthetic not_submitted row (errorCode "SubmitError") for every item in the
# batch instead, so the caller still gets a complete CSV covering this batch
# and goes on to attempt any remaining batches.
function Submit-DeleteBatch {
    param($BaseUrl, [hashtable]$Headers, $Batch)

    $uri = "$BaseUrl$script:DeletePath"
    $body = $Batch | ForEach-Object { @{ agentGuid = $_.agentGuid } }
    $bodyJson = $body | ConvertTo-Json -AsArray -Depth 3

    try {
        # Retries 429/5xx, same as read-only calls -- see the note on Invoke-RestMethodWithBackoff.
        $resp = Invoke-RestMethodWithBackoff -Uri $uri -Headers $Headers -Method Post -Body $bodyJson -TimeoutSec 60
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        $note = if ($status -in @(500, 502, 503, 504)) {
            " This batch was already retried $script:DeleteMaxRetries time(s) and still failed -- check Vision One's Audit Logs for this batch to see if it's a persistent issue before re-running. Resubmitting is safe (an endpoint the earlier attempt already deleted comes back 404 NotFound rather than a duplicate action)."
        } else { "" }
        $errMessage = "$($_.Exception.Message)$note"
        Write-Error "API error $status submitting delete batch: $errMessage" -ErrorAction Continue
        return $Batch | ForEach-Object {
            [pscustomobject]@{ taskId = $null; httpStatus = $status; errorCode = "SubmitError"; errorMessage = $errMessage }
        }
    }

    $results = @($resp)
    if ($results.Count -ne $Batch.Count) {
        $msg = "API returned $($results.Count) results for a batch of $($Batch.Count) -- cannot reliably match results to endpoints."
        Write-Error $msg -ErrorAction Continue
        return $Batch | ForEach-Object {
            [pscustomobject]@{ taskId = $null; httpStatus = $null; errorCode = "SubmitError"; errorMessage = $msg }
        }
    }

    foreach ($item in $results) {
        if ($item.status -eq 202) {
            $opLocation = ($item.headers | Where-Object { $_.name -eq "Operation-Location" } | Select-Object -First 1).value
            $taskId = if ($opLocation) { ($opLocation -split "/")[-1] } else { $null }
            [pscustomobject]@{ taskId = $taskId; httpStatus = 202; errorCode = $null; errorMessage = $null }
        } else {
            # code/message live directly on body -- e.g.
            #   {"status":404,"body":{"code":"NotFound","message":"Endpoint not found"}}
            # NOT nested under body.error as the bundled OpenAPI spec claims (confirmed
            # against the live API; the spec is wrong here). body has also been observed
            # as an escaped JSON string instead of a parsed object. Try the real shape
            # first, fall back to the spec's documented shape in case some other error
            # path actually does nest it, and if neither yields anything, fall back to
            # the raw item so a failure is never silently reported with a blank message.
            $itemBody = $item.body
            if ($itemBody -is [string]) {
                try { $itemBody = $itemBody | ConvertFrom-Json } catch { $itemBody = $null }
            }
            $code = $itemBody.code
            $message = $itemBody.message
            if (-not ($code -or $message)) {
                $code = $itemBody.error.code
                $message = $itemBody.error.message
            }

            if ($code -or $message) {
                [pscustomobject]@{ taskId = $null; httpStatus = $item.status; errorCode = $code; errorMessage = $message }
            } else {
                $raw = if ($null -ne $item.body) { $item.body | ConvertTo-Json -Depth 5 -Compress } else { "(no error body returned)" }
                [pscustomobject]@{ taskId = $null; httpStatus = $item.status; errorCode = $null; errorMessage = $raw }
            }
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
            return @{ status = "unknown"; httpStatus = $status; errorCode = $null; errorMessage = $_.Exception.Message }
        }

        if ($body.status -in @("succeeded", "failed")) {
            $errorCode    = if ($body.status -eq "failed") { $body.error.code }    else { $null }
            $errorMessage = if ($body.status -eq "failed") { $body.error.message } else { "" }
            return @{ status = $body.status; httpStatus = $null; errorCode = $errorCode; errorMessage = $errorMessage }
        }

        if ((Get-Date) -ge $deadline) {
            return @{ status = "timeout"; httpStatus = $null; errorCode = $null; errorMessage = "still '$($body.status)' after $($script:PollTimeoutSeconds)s" }
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
        [Parameter(Mandatory)] [string]$DeleteResultsCsv,
        [switch]$SkipFirstPrompt
    )

    $Endpoints = @($Endpoints)
    if ($Endpoints.Count -eq 0) { return $false }

    # A destructive action gated on typed confirmation must never run against
    # a non-interactive stdin (cron, CI, redirected input) -- Read-Host would
    # either throw or silently consume unrelated redirected data. Bail out
    # safely instead, regardless of -SkipFirstPrompt.
    #
    # [Console]::IsInputRedirected is a known false positive under the
    # PowerShell ISE and the VS Code PowerShell extension's Integrated
    # Console: neither gives the process a real console (ISE has none; VS
    # Code talks to it over a named pipe), so this API reports $true even
    # though a person is sitting there and Read-Host works fine. Recognize
    # those hosts by name so a real interactive user isn't blocked; genuine
    # redirection (cron, CI, `... | pwsh script.ps1`) still bails out below.
    $isKnownInteractiveHost = $Host.Name -in @("Windows PowerShell ISE Host", "Visual Studio Code Host")
    if ([Console]::IsInputRedirected -and -not $isKnownInteractiveHost) {
        Write-Host "`nNon-interactive session detected ($($Host.Name)); skipping the delete prompt for these $($Endpoints.Count) endpoint(s). Re-run from an interactive terminal (a plain console window, not a redirected/piped session) to delete them."
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

        if ($submitted.httpStatus -ne 202) {
            Write-Host ("  {0,-30} -> submit failed: {1} {2}: {3}" -f $ep.endpointName, $submitted.httpStatus, $submitted.errorCode, $submitted.errorMessage)
            $finalResults.Add([pscustomobject]@{
                endpointName              = $ep.endpointName
                agentGuid                 = $ep.agentGuid
                eppAgentProtectionManager = $ep.eppAgentProtectionManager
                taskId                    = ""
                finalStatus               = "not_submitted"
                httpStatus                = $submitted.httpStatus
                errorCode                 = $submitted.errorCode
                errorMessage              = $submitted.errorMessage
                actionTaken               = $script:ActionTakenByStatus["not_submitted"]
            })
            continue
        }

        $result = Wait-DeleteTask -BaseUrl $BaseUrl -Headers $headers -TaskId $submitted.taskId
        $suffix = if ($result.errorMessage) { ": $($result.errorMessage)" } else { "" }
        Write-Host ("  {0,-30} -> task {1}{2}" -f $ep.endpointName, $result.status, $suffix)
        $actionTaken = if ($script:ActionTakenByStatus.ContainsKey($result.status)) { $script:ActionTakenByStatus[$result.status] } else { $result.status }
        $finalResults.Add([pscustomobject]@{
            endpointName              = $ep.endpointName
            agentGuid                 = $ep.agentGuid
            eppAgentProtectionManager = $ep.eppAgentProtectionManager
            taskId                    = $submitted.taskId
            finalStatus               = $result.status
            httpStatus                = $result.httpStatus
            errorCode                 = $result.errorCode
            errorMessage              = $result.errorMessage
            actionTaken               = $actionTaken
        })
    }

    $finalResults | Export-Csv -Path $DeleteResultsCsv -Encoding utf8

    $succeeded = ($finalResults | Where-Object { $_.finalStatus -eq "succeeded" }).Count
    $failed = ($finalResults | Where-Object { $_.finalStatus -in @("failed", "not_submitted", "unknown") }).Count
    $timedOut = ($finalResults | Where-Object { $_.finalStatus -eq "timeout" }).Count

    Write-Host ("`n{0} succeeded, {1} failed, {2} timed out. Wrote {3} rows to {4}" -f `
        $succeeded, $failed, $timedOut, $finalResults.Count, $DeleteResultsCsv)

    return $true
}
