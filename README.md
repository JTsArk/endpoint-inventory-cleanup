# CHC Delete Endpoints

Tooling for Trend Vision One that (1) finds endpoints in the **Endpoint
Inventory** matching a host-name prefix and an offline threshold, and (2)
deletes those endpoints from Endpoint Inventory — all in one run if you want.

`pull_offline_endpoints.py` lists endpoints whose host name starts with
`iws` (case-insensitive, configurable) and that have been **offline for at
least 8 hours**, writing them to `offline_iws_endpoints.csv`. **Right after
listing, it offers to delete them too** — see
[Deleting endpoints](#deleting-endpoints) below. `delete_offline_endpoints.py`
remains available to delete later against a saved CSV (e.g. if you declined
during the pull, or want to retry).

## How it works

Calls `GET /v3.0/endpointSecurity/endpoints` (Endpoint Security → Get endpoint
list) and paginates via `nextLink`.

The Vision One endpoint-list filter (`TMV1-Filter` header) only supports
`eq / and / or / not / ()` — it has **no "starts-with" operator and no
date-range operator**. So the script:

- narrows server-side to Windows endpoints (`osPlatform eq 'windows'`), then
- applies the host-name prefix and the 8-hour offline test **client-side**.

"Offline" is derived from the most recent of the agent and sensor
last-connected times (`eppAgent.lastConnectedDateTime` /
`edrSensor.lastConnectedDateTime`, both nested in the response and returned in
UTC). Endpoints with no last-connected timestamp at all are skipped.

## Setup (one time)

```bash
python3 -m venv .venv
./.venv/bin/pip install requests

cp .env.example .env      # then edit .env and set TMV1_TOKEN + TMV1_REGION_URL
chmod 600 .env            # restrict to your user (recommended)
```

`.env` holds your token and region. It is git-ignored and must never be
committed. See `.env.example` for the available variables and regional URLs.

## Usage

Once `.env` is set up, run the wrapper — it loads `.env` and runs the script:

```bash
./run.sh
```

Results print to the console and are written to `offline_iws_endpoints.csv`
(the filename tracks whatever `HOSTNAME_PREFIX` is set to; git-ignored — it
contains customer endpoint data). If any matches were found, you'll then be
asked whether to delete them — see [Deleting endpoints](#deleting-endpoints).

### Running without run.sh

You can also load the environment yourself and invoke Python directly:

```bash
set -a; source .env; set +a
./.venv/bin/python pull_offline_endpoints.py
```

Or skip `.env` entirely and export the variables inline:

```bash
export TMV1_TOKEN="<your Vision One API key>"              # needs Endpoint Inventory: View
export TMV1_REGION_URL="https://api.xdr.trendmicro.com"    # US default; change per region
./.venv/bin/python pull_offline_endpoints.py
```

### Regional base URLs

| Region | URL |
|--------|-----|
| US | `https://api.xdr.trendmicro.com` |
| EU (Germany) | `https://api.eu.xdr.trendmicro.com` |
| Singapore | `https://api.sg.xdr.trendmicro.com` |
| Japan | `https://api.xdr.trendmicro.co.jp` |
| Australia | `https://api.au.xdr.trendmicro.com` |
| India | `https://api.in.xdr.trendmicro.com` |
| UAE | `https://api.mea.xdr.trendmicro.com` |
| UK | `https://api.uk.xdr.trendmicro.com` |
| Canada | `https://api.ca.xdr.trendmicro.com` |
| South Africa | `https://api.za.xdr.trendmicro.com` |

## PowerShell version

`Get-OfflineEndpoints.ps1` is a functionally identical port for
**PowerShell 7+** (`pwsh`). It needs no dependencies — `Invoke-RestMethod`,
JSON handling, and `Export-Csv` are all built in.

```powershell
$env:TMV1_TOKEN     = "<your Vision One API key>"
$env:TMV1_REGION_URL = "https://api.xdr.trendmicro.com"
pwsh ./Get-OfflineEndpoints.ps1
```

Or use the wrapper `run.ps1` (the PowerShell twin of `run.sh`) — it loads
`.env` if present, then runs the script. Any parameters are forwarded:

```powershell
pwsh ./run.ps1                     # loads .env, runs with defaults
pwsh ./run.ps1 -OfflineHours 24    # forwards -OfflineHours to the script
```

Parameters can also be passed directly to the script (they default to the env
vars):

```powershell
pwsh ./Get-OfflineEndpoints.ps1 -HostnamePrefix iws -OfflineHours 8
```

On macOS, install PowerShell with `brew install --cask powershell` (or
`powershell@preview`, whose command is `pwsh-preview`). On Windows it is
usually preinstalled or available from the Microsoft Store.

## Configuration

Edit the constants near the top of `pull_offline_endpoints.py` (Python), or
pass parameters to `Get-OfflineEndpoints.ps1` (PowerShell):

- `HOSTNAME_PREFIX` / `-HostnamePrefix` (default `iws`)
- `OFFLINE_HOURS` / `-OfflineHours` (default `8`)
- `PAGE_SIZE` / `-PageSize` (default `1000`)
- `OUTPUT_CSV` / `-OutputCsv` (default derived from `HOSTNAME_PREFIX`, e.g.
  `offline_iws_endpoints.csv`)
- `DELETE_RESULTS_CSV` / `-DeleteResultsCsv` (default derived from
  `HOSTNAME_PREFIX`, e.g. `delete_results_iws.csv`) — used only if you opt to
  delete right after pulling

## Deleting endpoints

Endpoints are removed from Endpoint Inventory via
`POST /v3.0/endpointSecurity/endpoints/delete`. There are two ways to trigger it:

1. **Automatically, right after pulling** — `pull_offline_endpoints.py` /
   `Get-OfflineEndpoints.ps1` list the matches, write the CSV, and then
   immediately ask whether to delete those same endpoints, using the exact
   same in-memory list (no re-read of the CSV, so there's no gap where the
   data could have changed).
2. **Standalone, against a saved CSV** — `delete_offline_endpoints.py` /
   `Remove-OfflineEndpoints.ps1` read `offline_iws_endpoints.csv` and do the
   same thing. Use this if you declined during the pull, or want to retry.

Both paths share the same underlying logic (`endpoint_delete.py` /
`EndpointDelete.Helpers.ps1`), so the behavior is identical either way.

> **This removes the Endpoint Inventory record only — it does NOT uninstall
> the agent software from the physical machine.** Vision One's own docs also
> warn that endpoints should be shut down before deleting them this way,
> which is why this tool only ever targets endpoints already confirmed
> offline.

**Safety model:** after listing the endpoints, you're always asked
interactively whether to proceed ("Delete these N endpoint(s) now?"), and
again with the full name list before anything is actually deleted ("Type
'yes' to proceed"). There is no way to delete non-interactively — if stdin
isn't a terminal (e.g. run from cron), the prompt is skipped entirely and
nothing is deleted. `--verify` / `-Verify` on the standalone scripts just
skips straight past the first question for convenience.

```bash
# Python — pulls, lists, then offers to delete
./.venv/bin/python pull_offline_endpoints.py

# Python — standalone delete against a saved CSV
./.venv/bin/python delete_offline_endpoints.py              # list, then ask whether to proceed
./.venv/bin/python delete_offline_endpoints.py --verify      # skip straight to the delete confirmation
```

```powershell
# PowerShell — pulls, lists, then offers to delete
pwsh ./Get-OfflineEndpoints.ps1

# PowerShell — standalone delete against a saved CSV
pwsh ./Remove-OfflineEndpoints.ps1              # list, then ask whether to proceed
pwsh ./Remove-OfflineEndpoints.ps1 -Verify      # skip straight to the delete confirmation
```

Deletion is asynchronous on Vision One's side — each accepted endpoint
creates a task, which is polled until it reaches `succeeded` / `failed` (or
times out after 120s), printing progress per endpoint name and writing a
full audit trail to `delete_results_iws.csv`
(`endpointName, agentGuid, taskId, finalStatus, errorMessage`).

Standalone-script options: `--csv` / `-InputCsv` (default
`offline_iws_endpoints.csv`), `--results-csv` / `-DeleteResultsCsv` (default
`delete_results_iws.csv`).

## Notes

- The puller's API key needs the **Endpoint Inventory → View** permission.
- The delete scripts' API key needs **Endpoint Inventory → Remove agents**
  and **View**.
- This API endpoint is only available on tenants updated to the Foundation
  Services release.
- Never commit a real token. `.env` and `*.csv` are git-ignored.
