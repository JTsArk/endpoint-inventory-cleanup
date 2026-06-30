# CHC Delete Endpoints

Python tooling for Trend Vision One that pulls endpoints from the **Endpoint
Inventory** matching a host-name prefix and an offline threshold.

`pull_offline_w11_endpoints.py` lists endpoints whose host name starts with
`w11` (case-insensitive) and that have been **offline for at least 8 hours**.

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

Results print to the console and are written to `offline_w11_endpoints.csv`
(git-ignored — it contains customer endpoint data).

### Running without run.sh

You can also load the environment yourself and invoke Python directly:

```bash
set -a; source .env; set +a
./.venv/bin/python pull_offline_w11_endpoints.py
```

Or skip `.env` entirely and export the variables inline:

```bash
export TMV1_TOKEN="<your Vision One API key>"              # needs Endpoint Inventory: View
export TMV1_REGION_URL="https://api.xdr.trendmicro.com"    # US default; change per region
./.venv/bin/python pull_offline_w11_endpoints.py
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

## Configuration

Edit the constants near the top of `pull_offline_w11_endpoints.py`:

- `HOSTNAME_PREFIX` (default `w11`)
- `OFFLINE_HOURS` (default `8`)
- `PAGE_SIZE` (default `1000`)
- `OUTPUT_CSV` (default `offline_w11_endpoints.csv`)

## Notes

- The API key must have the **Endpoint Inventory → View** permission.
- Never commit a real token. `.env` and `*.csv` are git-ignored.
