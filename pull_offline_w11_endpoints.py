#!/usr/bin/env python3
"""
Pull endpoints from the Trend Vision One Endpoint Inventory that:
  * have a host name starting with HOSTNAME_PREFIX (case-insensitive, default "iws"), AND
  * have been offline for at least 8 hours.

API used:  GET /v3.0/endpointSecurity/endpoints   (Endpoint Security -> Get endpoint list)

NOTE ON FILTERING
-----------------
The Vision One endpoint-list API (TMV1-Filter header) only supports the
operators eq / and / or / not / (). It has NO "starts-with" operator and NO
date range / greater-than operator. Therefore:
  * we narrow server-side to Windows endpoints (cheap, reduces volume), and
  * we apply the "host name starts with HOSTNAME_PREFIX" and "offline >= 8h" rules
    client-side after fetching the page.

"Offline" is determined from the most recent of the agent / sensor last-connected
timestamps. If that time is more than OFFLINE_HOURS ago, the endpoint is offline.

USAGE
-----
  export TMV1_TOKEN="<your Vision One API key>"
  export TMV1_REGION_URL="https://api.xdr.trendmicro.com"   # optional, defaults to US
  python3 pull_offline_w11_endpoints.py

Required API key permission: Endpoint Inventory -> View
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Regional base URL. Pick the one matching your Vision One region:
#   US        https://api.xdr.trendmicro.com
#   EU        https://api.eu.xdr.trendmicro.com
#   Singapore https://api.sg.xdr.trendmicro.com
#   Japan     https://api.xdr.trendmicro.co.jp
#   Australia https://api.au.xdr.trendmicro.com
#   India     https://api.in.xdr.trendmicro.com
#   UAE       https://api.mea.xdr.trendmicro.com
#   UK        https://api.uk.xdr.trendmicro.com
#   Canada    https://api.ca.xdr.trendmicro.com
#   S. Africa https://api.za.xdr.trendmicro.com
BASE_URL = os.environ.get("TMV1_REGION_URL", "https://api.xdr.trendmicro.com").rstrip("/")

TOKEN = os.environ.get("TMV1_TOKEN")

HOSTNAME_PREFIX = "iws"   # case-insensitive
OFFLINE_HOURS = 8
PAGE_SIZE = 1000          # allowed: 10, 50, 100, 200, 500, 1000
OUTPUT_CSV = f"offline_{HOSTNAME_PREFIX.lower()}_endpoints.csv"

ENDPOINTS_PATH = "/v3.0/endpointSecurity/endpoints"

# NOTE: we intentionally do NOT send a `select` param. The response body nests
# the agent/sensor fields under "eppAgent" and "edrSensor" objects (e.g.
# eppAgent.lastConnectedDateTime), whereas `select` uses flattened names. To
# avoid that mismatch silently dropping fields, we fetch full records and read
# the nested structure directly.

# Server-side filter: narrow to Windows endpoints to reduce data transferred.
# (We cannot express "starts with HOSTNAME_PREFIX" or "offline 8h" here.)
SERVER_FILTER = "osPlatform eq 'windows'"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_dt(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime.

    The API returns timestamps with no timezone (e.g. '2026-06-09T18:20:42'),
    which are UTC. We therefore treat any naive timestamp as UTC rather than
    letting it default to the local machine's timezone.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def last_seen(endpoint):
    """Return the most recent connection time across agent + sensor, or None.

    The agent/sensor fields are nested under 'eppAgent' and 'edrSensor'.
    """
    epp = endpoint.get("eppAgent") or {}
    edr = endpoint.get("edrSensor") or {}
    times = [
        parse_dt(epp.get("lastConnectedDateTime")),
        parse_dt(edr.get("lastConnectedDateTime")),
    ]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def fetch_all_endpoints(session):
    """Yield every endpoint matching the server-side filter, following nextLink pagination."""
    url = f"{BASE_URL}{ENDPOINTS_PATH}"
    params = {"top": PAGE_SIZE}
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "TMV1-Filter": SERVER_FILTER,
    }

    page = 0
    while url:
        page += 1
        resp = session.get(url, headers=headers, params=params, timeout=60)
        if resp.status_code != 200:
            sys.exit(f"API error {resp.status_code}: {resp.text}")

        body = resp.json()
        items = body.get("items", [])
        print(f"  page {page}: fetched {len(items)} endpoints", file=sys.stderr)
        for item in items:
            yield item

        # nextLink already contains query params; don't resend them.
        url = body.get("nextLink")
        params = None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    if not TOKEN:
        sys.exit("ERROR: set the TMV1_TOKEN environment variable to your Vision One API key.")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=OFFLINE_HOURS)
    print(f"Now (UTC):            {now.isoformat()}")
    print(f"Offline cutoff (UTC): {cutoff.isoformat()}  (last seen at or before this = offline)")
    print(f"Host name prefix:     {HOSTNAME_PREFIX!r} (case-insensitive)\n")

    session = requests.Session()
    matches = []
    scanned = 0

    for ep in fetch_all_endpoints(session):
        scanned += 1
        name = (ep.get("endpointName") or "")
        if not name.lower().startswith(HOSTNAME_PREFIX.lower()):
            continue

        seen = last_seen(ep)
        # Skip endpoints with no last-connected timestamp (never reported /
        # no telemetry) — we only flag hosts with a real connection time
        # that is older than the cutoff.
        if seen is None:
            continue
        if seen > cutoff:
            continue  # connected within the last 8h -> still online

        epp = ep.get("eppAgent") or {}
        edr = ep.get("edrSensor") or {}
        offline_for = (now - seen) if seen else None
        matches.append({
            "endpointName": name,
            "agentGuid": ep.get("agentGuid", ""),
            "type": ep.get("type", ""),
            "osName": ep.get("osName", ""),
            "ipAddresses": ", ".join(ep.get("ipAddresses", []) or []),
            "eppAgentStatus": epp.get("status", ""),
            "edrSensorConnectivity": edr.get("connectivity", ""),
            "lastSeenUtc": seen.isoformat() if seen else "never",
            "offlineHours": round(offline_for.total_seconds() / 3600, 1) if offline_for else "",
        })

    # Sort longest-offline first.
    matches.sort(key=lambda m: (m["offlineHours"] == "", m["offlineHours"]), reverse=True)

    print(f"\nScanned {scanned} Windows endpoints; "
          f"{len(matches)} match (host starts with '{HOSTNAME_PREFIX}' AND offline >= {OFFLINE_HOURS}h).\n")

    if matches:
        fieldnames = list(matches[0].keys())
        with open(OUTPUT_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(matches)
        print(f"Wrote {len(matches)} rows to {OUTPUT_CSV}\n")

        # Console preview
        for m in matches:
            print(f"  {m['endpointName']:<30} last seen {m['lastSeenUtc']:<28} "
                  f"offline {m['offlineHours']}h  ({m['agentGuid']})")
    else:
        print("No matching endpoints found.")


if __name__ == "__main__":
    main()
