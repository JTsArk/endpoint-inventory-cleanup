#requires -Version 7.0
<#
.SYNOPSIS
    Pull Trend Vision One endpoints whose host name starts with -HostnamePrefix
    (default "iws") and that have been offline for at least 8 hours.

.DESCRIPTION
    Calls GET /v3.0/endpointSecurity/endpoints (Endpoint Security -> Get endpoint
    list) and paginates via nextLink.

    The Vision One endpoint-list filter (TMV1-Filter header) only supports the
    operators eq / and / or / not / (). It has NO "starts-with" operator and NO
    date range / greater-than operator. Therefore:
      * we narrow server-side to Windows endpoints (cheap, reduces volume), and
      * we apply the "host name starts with -HostnamePrefix" and "offline >= N hours"
        rules client-side after fetching each page.

    "Offline" is determined from the most recent of the agent / sensor
    last-connected timestamps (eppAgent.lastConnectedDateTime /
    edrSensor.lastConnectedDateTime). These are nested in the response and are
    returned in UTC with no timezone marker, so we treat them as UTC.
    Endpoints with no last-connected timestamp at all are skipped.

.NOTES
    Requires API key permission: Endpoint Inventory -> View
    Requires PowerShell 7+ (pwsh). Runs on macOS, Linux, and Windows.

.EXAMPLE
    $env:TMV1_TOKEN = "<your Vision One API key>"
    $env:TMV1_REGION_URL = "https://api.xdr.trendmicro.com"
    pwsh ./Get-OfflineW11Endpoints.ps1
#>

[CmdletBinding()]
param(
    # Vision One API key. Defaults to the TMV1_TOKEN environment variable.
    [string]$Token = $env:TMV1_TOKEN,

    # Regional API base URL. Defaults to TMV1_REGION_URL, then US.
    [string]$BaseUrl = $(if ($env:TMV1_REGION_URL) { $env:TMV1_REGION_URL } else { "https://api.xdr.trendmicro.com" }),

    # Host name prefix to match (case-insensitive).
    [string]$HostnamePrefix = "iws",

    # Minimum hours offline to be included.
    [int]$OfflineHours = 8,

    # Records per page: 10, 50, 100, 200, 500, or 1000.
    [int]$PageSize = 1000,

    # Output CSV path. Defaults to a name derived from -HostnamePrefix.
    [string]$OutputCsv = "offline_$($HostnamePrefix.ToLower())_endpoints.csv"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error "Set the TMV1_TOKEN environment variable (or pass -Token) to your Vision One API key."
    exit 1
}

$BaseUrl = $BaseUrl.TrimEnd("/")
$endpointsPath = "/v3.0/endpointSecurity/endpoints"

# Server-side filter: narrow to Windows endpoints. We cannot express
# "starts with -HostnamePrefix" or "offline Nh" here (operator set is eq/and/or/not).
$serverFilter = "osPlatform eq 'windows'"

# Parse an ISO-8601 timestamp to a UTC DateTime. The API omits the timezone,
# and the values are UTC, so we assume UTC rather than local time.
function ConvertTo-Utc([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) { return $null }
    try {
        $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor `
                  [System.Globalization.DateTimeStyles]::AdjustToUniversal
        return [datetime]::Parse($value, [System.Globalization.CultureInfo]::InvariantCulture, $styles)
    } catch {
        return $null
    }
}

# Most recent connection time across agent + sensor, or $null.
function Get-LastSeen($endpoint) {
    $times = @(
        ConvertTo-Utc $endpoint.eppAgent.lastConnectedDateTime
        ConvertTo-Utc $endpoint.edrSensor.lastConnectedDateTime
    ) | Where-Object { $_ -ne $null }
    if ($times.Count -eq 0) { return $null }
    return ($times | Measure-Object -Maximum).Maximum
}

$now    = [datetime]::UtcNow
$cutoff = $now.AddHours(-$OfflineHours)

Write-Host ("Now (UTC):            {0:o}" -f $now)
Write-Host ("Offline cutoff (UTC): {0:o}  (last seen at or before this = offline)" -f $cutoff)
Write-Host ("Host name prefix:     '{0}' (case-insensitive)`n" -f $HostnamePrefix)

$headers = @{
    Authorization = "Bearer $Token"
    "TMV1-Filter" = $serverFilter
}

$uri     = "$BaseUrl$endpointsPath`?top=$PageSize"
$results = [System.Collections.Generic.List[object]]::new()
$scanned = 0
$page    = 0

while ($uri) {
    $page++
    try {
        $resp = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get -TimeoutSec 60
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        Write-Error "API error $status while calling $uri : $($_.Exception.Message)"
        exit 1
    }

    $items = @($resp.items)
    Write-Host ("  page {0}: fetched {1} endpoints" -f $page, $items.Count)

    foreach ($ep in $items) {
        $scanned++

        $name = [string]$ep.endpointName
        if (-not ($name -like "$HostnamePrefix*")) { continue }   # -like is case-insensitive

        $seen = Get-LastSeen $ep
        if ($null -eq $seen)   { continue }   # no telemetry -> skip
        if ($seen -gt $cutoff) { continue }   # connected within window -> still online

        # NOTE: PowerShell variable names are case-insensitive, so this must NOT
        # be named $offlineHours (that would alias the $OfflineHours parameter).
        $hoursOffline = [math]::Round(($now - $seen).TotalHours, 1)
        $results.Add([pscustomobject]@{
            endpointName          = $name
            agentGuid             = $ep.agentGuid
            type                  = $ep.type
            osName                = $ep.osName
            ipAddresses           = ($ep.ipAddresses -join ", ")
            eppAgentStatus        = $ep.eppAgent.status
            edrSensorConnectivity = $ep.edrSensor.connectivity
            lastSeenUtc           = $seen.ToString("o")
            offlineHours          = $hoursOffline
        })
    }

    $uri = $resp.nextLink   # full URL with query params already attached
}

# Longest-offline first. Wrap in @() so a single match stays a countable array.
$results = @($results | Sort-Object offlineHours -Descending)

Write-Host ("`nScanned {0} Windows endpoints; {1} match (host starts with '{2}' AND offline >= {3}h).`n" -f `
    $scanned, $results.Count, $HostnamePrefix, $OfflineHours)

if ($results.Count -gt 0) {
    $results | Export-Csv -Path $OutputCsv -Encoding utf8
    Write-Host ("Wrote {0} rows to {1}`n" -f $results.Count, $OutputCsv)
    foreach ($m in $results) {
        Write-Host ("  {0,-30} last seen {1,-28} offline {2}h  ({3})" -f `
            $m.endpointName, $m.lastSeenUtc, $m.offlineHours, $m.agentGuid)
    }
} else {
    Write-Host "No matching endpoints found."
}
