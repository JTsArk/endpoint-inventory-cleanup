# Endpoint Inventory Cleanup

Tooling for Trend Vision One that:

- Finds endpoints in the **Endpoint Inventory** matching a host-name prefix
  (default `iws`, case-insensitive) and an offline threshold (default 8 hours)
- Deletes those endpoints from Endpoint Inventory after verification

**Available in two equivalent implementations — pick one:**

- [Python](#python) — `pull_offline_endpoints.py` / `delete_offline_endpoints.py`
- [PowerShell](#powershell) — `Get-OfflineEndpoints.ps1` / `Remove-OfflineEndpoints.ps1`

Both do exactly the same thing and share the rest of this document (how it
works, permissions, regional URLs, deleting-endpoints behavior). Only the
setup/invocation commands differ.

There's also a [Lambda](#lambda-list-only-scanner-and-on-demand-deleter) variant
for running the scan (and, optionally, the delete) on demand without a
local Python/PowerShell setup — two separate functions, never chained
automatically: a scanner that only ever lists, and a deleter that defaults
to preview-only until you explicitly confirm.

## How It Works

Calls `GET /v3.0/endpointSecurity/endpoints` (Endpoint Security → Get endpoint
list) and paginates via `nextLink`.

The Vision One endpoint-list filter (`TMV1-Filter` header) only supports
`eq / and / or / not / ()` — it has **no "starts-with" operator and no
date-range operator**. So the script:

- narrows server-side to a configurable OS platform (`osPlatform eq '...'`,
  default `windows`), then
- applies the host-name prefix and the 8-hour offline test **client-side**.

"Offline" is derived from the most recent of the agent and sensor
last-connected times (`eppAgent.lastConnectedDateTime` /
`edrSensor.lastConnectedDateTime`, both nested in the response and returned in
UTC). Endpoints with no last-connected timestamp at all are skipped.

**Retry / backoff:** every API call (pulling endpoints and deleting them)
retries automatically on `429` (throttled) or transient `500 / 502 / 503 /
504` responses, up to 5 attempts. It honors the API's `Retry-After` header
when present; otherwise it backs off exponentially (1s, 2s, 4s, ...) with a
little random jitter to avoid retry storms. Any other error status is
returned immediately without retrying.

## Deleting Endpoints

Endpoints are removed from Endpoint Inventory via
`POST /v3.0/endpointSecurity/endpoints/delete`. There are two ways to trigger it
(commands for each language are in their own section below):

1. **Automatically, right after pulling** — the puller lists the matches,
   writes the CSV, and then immediately asks whether to delete those same
   endpoints, using the exact same in-memory list (no re-read of the CSV, so
   there's no gap where the data could have changed).
2. **Standalone, against a saved CSV** — the delete script reads
   `offline_iws_endpoints.csv` and does the same thing. Use this if you
   declined during the pull, or want to retry.

Both paths share the same underlying logic (`endpoint_delete.py` /
`EndpointDelete.Helpers.ps1`), so the behavior is identical either way.
Neither file is a script you run directly — they're shared helper modules
imported by the pull and delete scripts on their respective side, holding the
retry/backoff logic, the delete-submit-and-poll flow, and the results-CSV
writer.

> **This removes the Endpoint Inventory record only — it does NOT uninstall
> the agent software from the physical machine.** Vision One's own docs also
> warn that endpoints should be shut down before deleting them this way,
> which is why this tool only ever targets endpoints already confirmed
> offline.

**Also covers Server & Workload Protection (SWP / Cloud One Workload
Security)** — no separate SWP API integration needed. Per Trend Micro's own
docs (KA-0019142, KA-0012152), Endpoint Inventory removal now covers all
Trend Vision One Endpoint Security agent deployments, including Server &
Workload Protection, and (for endpoints removed after 2025/02/24) this
removal also affects the underlying Cloud One - Workload Security / Apex One
agent — not just the Vision One-side record.

> **Caveat:** this only applies to SWP endpoints already showing up in
> Vision One's unified Endpoint Inventory (the "Foundation-mode" console). A
> legacy Deep Security Manager / Cloud One Workload Security instance that
> isn't connected/migrated into Vision One won't appear in the
> `GET /v3.0/endpointSecurity/endpoints` results this tool queries, so those
> computers won't be touched by this tool either (see KA-0022958 for a known
> migration-discrepancy issue where endpoints stay listed under a
> disconnected Deep Security Manager instead of Server & Workload
> Protection).

**Safety model:** after listing the endpoints, you're always asked
interactively whether to proceed ("Delete these N endpoint(s) now?"), and
again with the full name list before anything is actually deleted ("Type
'yes' to proceed"). There is no way to delete non-interactively — if stdin
isn't a terminal (e.g. run from cron), the prompt is skipped entirely and
nothing is deleted. `--verify` / `-Verify` on the standalone scripts just
skips straight past the first question for convenience.

Deletion is asynchronous on Vision One's side — each accepted endpoint
creates a task, which is polled until it reaches `succeeded` / `failed` (or
times out after 120s), printing progress per endpoint name and writing a
full audit trail to `delete_results_iws.csv` (the filename tracks the
host-name prefix, same as the pull CSV)
(`endpointName, agentGuid, taskId, finalStatus, errorMessage, actionTaken`).
`actionTaken` is a human-readable summary derived from `finalStatus` (e.g.
"Deleted from Endpoint Inventory", "Delete failed", "Not submitted (API
error)").

## Configuration Reference

| Concept | Python | PowerShell | Default |
|---|---|---|---|
| Host name prefix to match (case-insensitive) | `HOSTNAME_PREFIX` | `-HostnamePrefix` | `iws` |
| Minimum hours offline | `OFFLINE_HOURS` | `-OfflineHours` | `8` |
| Page size per API call | `PAGE_SIZE` | `-PageSize` | `1000` |
| Pull output CSV | `OUTPUT_CSV` | `-OutputCsv` | derived, e.g. `offline_iws_endpoints.csv` |
| Delete audit-trail CSV | `DELETE_RESULTS_CSV` | `-DeleteResultsCsv` | derived, e.g. `delete_results_iws.csv` |
| Skip first delete prompt | `--verify` | `-Verify` | off |
| Standalone delete input CSV | `--csv` | `-InputCsv` | `offline_iws_endpoints.csv` |
| OS platform filter | `OS_PLATFORM` | `-OsPlatform` | `windows` |

Python options are constants near the top of the `.py` files; PowerShell
options are named parameters.

### Changing the Host Name Prefix, Offline Threshold, and OS Platform

**Python** — edit the `HOSTNAME_PREFIX`, `OFFLINE_HOURS`, and `OS_PLATFORM`
constants near the top of `pull_offline_endpoints.py`:

```python
HOSTNAME_PREFIX = "iws"   # case-insensitive
OFFLINE_HOURS = 8
OS_PLATFORM = "windows"   # windows | mac | linux | unix | unknown
```

**PowerShell** — pass `-HostnamePrefix`, `-OfflineHours`, and `-OsPlatform`
on the command line (no file edit needed); they default to `iws`, `8`, and
`windows` if omitted:

```powershell
pwsh ./Get-OfflineEndpoints.ps1 -HostnamePrefix corp -OfflineHours 24 -OsPlatform linux
pwsh ./run.ps1 -HostnamePrefix corp -OfflineHours 24 -OsPlatform linux   # via the wrapper
```

`-OsPlatform` is validated against `windows`, `mac`, `linux`, `unix`, and
`unknown` — anything else is rejected before any API call is made. The
Python side (`OS_PLATFORM`) checks the same set at startup.

Both output CSV filenames (`offline_<prefix>_endpoints.csv` and
`delete_results_<prefix>.csv`) are derived from the prefix automatically, in
both implementations.

### Regional Base URLs

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

`.env.example` sets `TMV1_REGION_URL` to the US endpoint by default — edit
it in your `.env` if your tenant is in another region.

## Notes

- The puller's API key needs the **Endpoint Inventory → View** permission.
- The delete scripts' API key needs **Endpoint Inventory → Remove agents**
  and **View**.
- This API endpoint is only available on tenants updated to the Foundation
  Services release.
- Never commit a real token. `.env` and `*.csv` are git-ignored.

---

## Python

### Setup (One Time)

```bash
python3 -m venv .venv
./.venv/bin/pip install requests

cp .env.example .env      # then edit .env and set TMV1_TOKEN + TMV1_REGION_URL
chmod 600 .env            # restrict to your user (recommended)
```

`.env` holds your token and region. It is git-ignored and must never be
committed. See `.env.example` for the available variables and regional URLs.

### Usage

Once `.env` is set up, run the wrapper — it loads `.env` and runs the script:

```bash
./run.sh
```

Results print to the console and are written to `offline_iws_endpoints.csv`
(the filename tracks whatever `HOSTNAME_PREFIX` is set to; git-ignored — it
contains customer endpoint data). If any matches were found, you'll then be
asked whether to delete them — see [Deleting Endpoints](#deleting-endpoints).
If you proceed, a delete audit-trail is written to `delete_results_iws.csv`
(also tracks `HOSTNAME_PREFIX`).

You can also run a specific script through the wrapper, or skip it entirely:

```bash
./run.sh delete_offline_endpoints.py --verify   # run any script through the wrapper

set -a; source .env; set +a                     # or load the environment yourself...
./.venv/bin/python pull_offline_endpoints.py    # ...and invoke Python directly

export TMV1_TOKEN="<your Vision One API key>"              # or skip .env entirely and
export TMV1_REGION_URL="https://api.xdr.trendmicro.com"    # export the variables inline
./.venv/bin/python pull_offline_endpoints.py
```

### Deleting Endpoints

```bash
# Pulls, lists, then offers to delete
./.venv/bin/python pull_offline_endpoints.py

# Standalone delete against a saved CSV
./.venv/bin/python delete_offline_endpoints.py              # list, then ask whether to proceed
./.venv/bin/python delete_offline_endpoints.py --verify      # skip straight to the delete confirmation
```

See [Deleting Endpoints](#deleting-endpoints) above for the safety model and
what deletion actually does.

---

## PowerShell

`Get-OfflineEndpoints.ps1` / `Remove-OfflineEndpoints.ps1` are a functionally
identical port for **PowerShell 7+** (`pwsh`). They need no dependencies —
`Invoke-RestMethod`, JSON handling, and `Export-Csv` are all built in.

On macOS, install PowerShell with `brew install --cask powershell` (or
`powershell@preview`, whose command is `pwsh-preview`). On Windows it is
usually preinstalled or available from the Microsoft Store.

### Setup (One Time)

Nothing to install beyond PowerShell 7+ itself. Either set environment
variables directly, or use the same `.env` file as the Python side (both
implementations read the same `TMV1_TOKEN` / `TMV1_REGION_URL`):

```bash
cp .env.example .env      # then edit .env and set TMV1_TOKEN + TMV1_REGION_URL
chmod 600 .env            # restrict to your user (recommended)
```

### Usage

Use the wrapper `run.ps1` (the PowerShell twin of `run.sh`) — it loads `.env`
if present, then runs the puller. Any parameters are forwarded:

```powershell
pwsh ./run.ps1                     # loads .env, runs with defaults
pwsh ./run.ps1 -OfflineHours 24    # forwards -OfflineHours to the script
pwsh ./run.ps1 Remove-OfflineEndpoints.ps1 -Verify   # run a specific script through the wrapper
```

Results print to the console and are written to `offline_iws_endpoints.csv`
(the filename tracks whatever `-HostnamePrefix` is set to; git-ignored — it
contains customer endpoint data). If any matches were found, you'll then be
asked whether to delete them — see [Deleting Endpoints](#deleting-endpoints).
If you proceed, a delete audit-trail is written to `delete_results_iws.csv`
(also tracks `-HostnamePrefix`).

Or invoke a script directly:

```powershell
$env:TMV1_TOKEN     = "<your Vision One API key>"
$env:TMV1_REGION_URL = "https://api.xdr.trendmicro.com"
pwsh ./Get-OfflineEndpoints.ps1

pwsh ./Get-OfflineEndpoints.ps1 -HostnamePrefix iws -OfflineHours 8   # parameters default to the env vars above
```

### Deleting Endpoints

```powershell
# Pulls, lists, then offers to delete
pwsh ./Get-OfflineEndpoints.ps1

# Standalone delete against a saved CSV
pwsh ./Remove-OfflineEndpoints.ps1              # list, then ask whether to proceed
pwsh ./Remove-OfflineEndpoints.ps1 -Verify      # skip straight to the delete confirmation
```

See [Deleting Endpoints](#deleting-endpoints) above for the safety model and
what deletion actually does.

---

## Lambda (List-Only Scanner and On-Demand Deleter)

Two on-demand Lambda functions, invoked independently — **never chained
automatically:**

- `lambda_function.py` (`offline-endpoint-scanner`) — scans Endpoint
  Inventory and returns the matches. **Never deletes anything.** Optionally
  writes the match list to S3 and publishes a summary to SNS (email/etc.).
- `lambda_delete_function.py` (`offline-endpoint-deleter`) — reads an
  endpoint CSV from S3 (normally the one the scanner just wrote) and deletes
  those endpoints. **Defaults to preview-only:** invoke it without
  `"confirm": true` and it only reports what *would* be deleted, without
  calling the delete API at all. Nothing is ever deleted on a first/default
  invoke — you must explicitly re-invoke with `"confirm": true` to actually
  delete. This mirrors the local scripts' two-step "type yes twice" prompt,
  just as two separate deliberate invokes instead of two `Read-Host` prompts.

Both are stdlib-only for the Vision One API calls (`urllib`, not
`requests`); the S3/SNS integration uses `boto3`, which ships built into
every AWS-provided Python Lambda runtime. So both still deploy as a single
file each, with no dependency layer.

### Deploy (One Time)

**1. Package both functions:**
```bash
zip scanner.zip lambda_function.py
zip deleter.zip lambda_delete_function.py
```

**2. Create an S3 bucket** (holds scan results and delete audit trails):
```bash
aws s3 mb s3://<your-bucket-name>
```

**3. Create an SNS topic and subscribe your email:**
```bash
TOPIC_ARN=$(aws sns create-topic --name offline-endpoint-notifications --query TopicArn --output text)
aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint you@example.com
```
AWS emails you a confirmation link — you won't receive notifications until
you click it.

**4. Create an execution role + policy for the scanner** (CloudWatch Logs,
plus `s3:PutObject` scoped to `scans/*`, plus `sns:Publish`):
```bash
aws iam create-role --role-name offline-endpoint-scanner-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name offline-endpoint-scanner-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name offline-endpoint-scanner-role \
  --policy-name scanner-s3-sns --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {\"Effect\": \"Allow\", \"Action\": \"s3:PutObject\", \"Resource\": \"arn:aws:s3:::<your-bucket-name>/scans/*\"},
      {\"Effect\": \"Allow\", \"Action\": \"sns:Publish\", \"Resource\": \"$TOPIC_ARN\"}
    ]
  }"
```

**5. Create an execution role + policy for the deleter** (CloudWatch Logs,
plus `s3:GetObject`/`s3:PutObject` across the bucket — it reads scan CSVs
and writes results next to them — plus `sns:Publish`):
```bash
aws iam create-role --role-name offline-endpoint-deleter-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name offline-endpoint-deleter-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name offline-endpoint-deleter-role \
  --policy-name deleter-s3-sns --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {\"Effect\": \"Allow\", \"Action\": [\"s3:GetObject\", \"s3:PutObject\"], \"Resource\": \"arn:aws:s3:::<your-bucket-name>/*\"},
      {\"Effect\": \"Allow\", \"Action\": \"sns:Publish\", \"Resource\": \"$TOPIC_ARN\"}
    ]
  }"
```
IAM roles can take 10-20 seconds to propagate — if function creation below
fails with "role cannot be assumed," just retry.

**6. Create both functions:**
```bash
aws lambda create-function --function-name offline-endpoint-scanner \
  --runtime python3.13 --handler lambda_function.lambda_handler \
  --zip-file fileb://scanner.zip \
  --role arn:aws:iam::<account-id>:role/offline-endpoint-scanner-role \
  --timeout 300 \
  --environment "Variables={TMV1_TOKEN=<your Vision One API key>,TMV1_REGION_URL=https://api.xdr.trendmicro.com,RESULTS_BUCKET=<your-bucket-name>,SNS_TOPIC_ARN=$TOPIC_ARN}"

aws lambda create-function --function-name offline-endpoint-deleter \
  --runtime python3.13 --handler lambda_delete_function.lambda_handler \
  --zip-file fileb://deleter.zip \
  --role arn:aws:iam::<account-id>:role/offline-endpoint-deleter-role \
  --timeout 300 \
  --environment "Variables={TMV1_TOKEN=<your Vision One API key>,TMV1_REGION_URL=https://api.xdr.trendmicro.com,SNS_TOPIC_ARN=$TOPIC_ARN}"
```
The deleter's token needs **Endpoint Inventory → Remove agents** in
addition to **View** — the scanner's only needs **View**. No VPC is needed
for either (the Vision One API is public). The default 3-second Lambda
timeout is nowhere near enough for a full paginated scan or a large delete
batch — `--timeout 300` gives 5 minutes; adjust to your tenant's endpoint
count.

To update code later:
```bash
zip scanner.zip lambda_function.py && aws lambda update-function-code --function-name offline-endpoint-scanner --zip-file fileb://scanner.zip
zip deleter.zip lambda_delete_function.py && aws lambda update-function-code --function-name offline-endpoint-deleter --zip-file fileb://deleter.zip
```

### Invoke — Scanning

```bash
# Defaults (hostnamePrefix=iws, offlineHours=8, osPlatform=windows)
aws lambda invoke --function-name offline-endpoint-scanner response.json
cat response.json

# Overriding parameters per invocation
aws lambda invoke --function-name offline-endpoint-scanner \
  --cli-binary-format raw-in-base64-out \
  --payload '{"hostnamePrefix": "corp", "offlineHours": 24, "osPlatform": "linux"}' \
  response.json
```

The response body is JSON: `scanned`, `matchCount`, `matches` (each with
`endpointName`, `agentGuid`, `lastSeenUtc`, `offlineHours`, etc.),
`s3Bucket`/`s3Key` (if `RESULTS_BUCKET` is set and there were matches), and a
`note` reiterating that nothing was deleted. If `SNS_TOPIC_ARN` is set, you
also get an email with a summary, a preview of the first 25 matches, and the
`s3Bucket`/`s3Key` to hand to the deleter — sent even on zero-match runs, so
you always know the scan ran. See
[Configuration Reference](#configuration-reference) for what each parameter
means — `hostnamePrefix`, `offlineHours`, `osPlatform`, and `pageSize` map
directly to the Python/PowerShell equivalents, just as event-payload keys
instead of constants/flags.

### Invoke — Deleting

Using the `s3Key` from the scanner's response or notification email:

```bash
# 1. Preview -- reports what would be deleted, calls no delete API at all
aws lambda invoke --function-name offline-endpoint-deleter \
  --cli-binary-format raw-in-base64-out \
  --payload '{"s3Bucket": "<your-bucket-name>", "s3Key": "scans/iws-20260710T120000Z.csv"}' \
  response.json
cat response.json

# 2. Actually delete -- same payload, plus confirm
aws lambda invoke --function-name offline-endpoint-deleter \
  --cli-binary-format raw-in-base64-out \
  --payload '{"s3Bucket": "<your-bucket-name>", "s3Key": "scans/iws-20260710T120000Z.csv", "confirm": true}' \
  response.json
```

The confirmed-delete response is JSON: `count`, `succeeded`, `failed`,
`timedOut`, and `results` (per-endpoint `finalStatus` / `actionTaken`,
matching the local scripts' audit-trail columns) — also written to
`s3://<bucket>/<key>-delete-results.csv` and, if `SNS_TOPIC_ARN` is set,
emailed as a completion summary.

See [Deleting Endpoints](#deleting-endpoints) above for what deletion
actually does (Endpoint Inventory record only, not the physical agent) and
required API key permissions.
