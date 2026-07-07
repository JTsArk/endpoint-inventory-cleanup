#requires -Version 7.0
<#
.SYNOPSIS
    Delete endpoints from the Trend Vision One Endpoint Inventory.

.DESCRIPTION
    Reads the CSV produced by Get-OfflineEndpoints.ps1 (endpointName +
    agentGuid columns) and removes those endpoints from Endpoint Inventory.

    Note: Get-OfflineEndpoints.ps1 now offers to do this immediately after
    listing offline endpoints, so you don't need to run this script
    separately in the common case. This script remains useful for re-running
    the delete step later against a previously-saved CSV (e.g. if you said
    "no" during the pull, or want to retry).

    IMPORTANT
    ---------
      * This removes the ENDPOINT INVENTORY RECORD. It does NOT uninstall the
        agent software from the physical machine.
      * Vision One's own docs warn: shut down endpoints before using this API;
        using it on active endpoints may prevent the resulting task from
        working correctly. This tool is intended for endpoints already
        confirmed offline by Get-OfflineEndpoints.ps1.
      * This API endpoint is only available on tenants updated to the
        Foundation Services release.

    SAFETY
    ------
    The CSV is read exactly once per run, and every name printed is from that
    same in-memory list -- so whatever you confirm is guaranteed to be
    exactly what gets deleted (no risk of the CSV changing between a "look"
    run and a separate later delete run).

    After listing the endpoints, the script always asks interactively
    whether to proceed (unless -Verify was already passed, which skips
    straight past that first question). Either way, nothing is deleted until
    you additionally type "yes" at the final "will be DELETED" confirmation.
    If stdin is redirected (e.g. run from cron), the delete prompt is skipped
    entirely rather than risk hanging or misbehaving.

    Throttled (429) or transient (5xx) responses are retried with exponential
    backoff, honoring the Retry-After header when the API sends one.

.NOTES
    Requires API key permissions: Endpoint Inventory -> Remove agents, View
    Requires PowerShell 7+ (pwsh). Runs on macOS, Linux, and Windows.

.EXAMPLE
    $env:TMV1_TOKEN = "<your Vision One API key>"
    pwsh ./Remove-OfflineEndpoints.ps1              # list, then ask whether to proceed

.EXAMPLE
    pwsh ./Remove-OfflineEndpoints.ps1 -Verify      # skip straight to the delete confirmation
#>

[CmdletBinding()]
param(
    # Vision One API key. Defaults to the TMV1_TOKEN environment variable.
    [string]$Token = $env:TMV1_TOKEN,

    # Regional API base URL. Defaults to TMV1_REGION_URL, then US.
    [string]$BaseUrl = $(if ($env:TMV1_REGION_URL) { $env:TMV1_REGION_URL } else { "https://api.xdr.trendmicro.com" }),

    # Input CSV path (output of Get-OfflineEndpoints.ps1).
    [string]$InputCsv = "offline_iws_endpoints.csv",

    # Results/audit-trail CSV path.
    [string]$DeleteResultsCsv = "delete_results_iws.csv",

    # Skip the interactive prompt below and go straight to the delete
    # confirmation. Without this switch, the script still only acts after
    # you confirm interactively -- nothing is deleted non-interactively.
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

$BaseUrl = $BaseUrl.TrimEnd("/")

. (Join-Path $PSScriptRoot "EndpointDelete.Helpers.ps1")

# Read endpointName + agentGuid pairs from the puller's CSV output.
function Get-EndpointsFromCsv([string]$path) {
    if (-not (Test-Path $path)) {
        Write-Error "Input CSV not found: $path`nRun Get-OfflineEndpoints.ps1 first to generate it."
        exit 1
    }
    $rows = Import-Csv -Path $path
    $endpoints = foreach ($row in $rows) {
        if ([string]::IsNullOrWhiteSpace($row.agentGuid)) { continue }
        [pscustomobject]@{
            endpointName = [string]$row.endpointName
            agentGuid    = [string]$row.agentGuid
        }
    }
    return @($endpoints)
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

$endpoints = Get-EndpointsFromCsv $InputCsv
if ($endpoints.Count -eq 0) {
    Write-Host "No endpoints found in $InputCsv. Nothing to do."
    exit 0
}

Write-Host ("{0} endpoint(s) in {1}:" -f $endpoints.Count, $InputCsv)
foreach ($ep in $endpoints) {
    Write-Host ("  {0,-30} ({1})" -f $ep.endpointName, $ep.agentGuid)
}

Invoke-DeleteFlow -Endpoints $endpoints -BaseUrl $BaseUrl -Token $Token `
    -DeleteResultsCsv $DeleteResultsCsv -SkipFirstPrompt:$Verify | Out-Null
