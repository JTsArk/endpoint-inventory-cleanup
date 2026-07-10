#!/usr/bin/env python3
"""
On-demand AWS Lambda: scan Trend Vision One Endpoint Inventory for endpoints
matching a host-name prefix that have been offline for a while, and return
the list. LIST ONLY -- this function never deletes anything. Review its
output, then delete separately and explicitly with delete_offline_endpoints.py
or Remove-OfflineEndpoints.ps1 (pointed at the agentGuid values returned
here) -- there is no automatic or chained delete step.

Stdlib only (urllib), so it deploys as a single file with no dependency
layer: zip this file alone and upload it.

Invoke on demand, e.g.:
  aws lambda invoke --function-name offline-endpoint-scanner \
    --payload '{"hostnamePrefix": "iws", "offlineHours": 8}' \
    --cli-binary-format raw-in-base64-out response.json

Event overrides (all optional; fall back to the defaults below):
  hostnamePrefix, offlineHours, osPlatform, pageSize

Required Lambda environment variables:
  TMV1_TOKEN        Vision One API key (Endpoint Inventory -> View)
  TMV1_REGION_URL   optional, defaults to the US endpoint
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_BASE_URL = "https://api.xdr.trendmicro.com"
ENDPOINTS_PATH = "/v3.0/endpointSecurity/endpoints"

DEFAULT_HOSTNAME_PREFIX = "iws"
DEFAULT_OFFLINE_HOURS = 8
DEFAULT_OS_PLATFORM = "windows"
DEFAULT_PAGE_SIZE = 1000
VALID_OS_PLATFORMS = ("windows", "mac", "linux", "unix", "unknown")

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_dt(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime.

    The API returns timestamps with no timezone, which are UTC -- treat any
    naive timestamp as UTC rather than local time.
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
    """Return the most recent connection time across agent + sensor, or None."""
    epp = endpoint.get("eppAgent") or {}
    edr = endpoint.get("edrSensor") or {}
    times = [t for t in (parse_dt(epp.get("lastConnectedDateTime")),
                         parse_dt(edr.get("lastConnectedDateTime"))) if t is not None]
    return max(times) if times else None


def request_with_backoff(url, headers):
    """GET url with retry + exponential backoff on 429 (throttled) / transient 5xx.

    Honors the Retry-After header when the API sends one; otherwise backs off
    exponentially (1s, 2s, 4s, ...) with a little jitter to avoid retry storms.
    Returns (status_code, headers, body_bytes) -- callers check status_code.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, resp_headers, body = e.code, e.headers, e.read()

        if status not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
            return status, resp_headers, body

        retry_after = resp_headers.get("Retry-After") if resp_headers else None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
        else:
            delay = BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
        time.sleep(delay)

    return status, resp_headers, body  # unreachable, keeps type-checkers happy


def fetch_all_endpoints(base_url, token, server_filter, page_size):
    """Yield every endpoint matching the server-side filter, following nextLink pagination."""
    url = f"{base_url}{ENDPOINTS_PATH}?top={page_size}"
    headers = {"Authorization": f"Bearer {token}", "TMV1-Filter": server_filter}

    while url:
        status, _, body = request_with_backoff(url, headers)
        if status != 200:
            raise RuntimeError(f"API error {status} while calling {url}: {body.decode('utf-8', 'replace')}")

        data = json.loads(body)
        for item in data.get("items", []):
            yield item

        url = data.get("nextLink")  # full URL, query params already attached


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def build_response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def lambda_handler(event, context):
    event = event or {}

    token = os.environ.get("TMV1_TOKEN")
    if not token:
        return build_response(500, {"error": "TMV1_TOKEN environment variable is not set."})
    base_url = (os.environ.get("TMV1_REGION_URL") or DEFAULT_BASE_URL).rstrip("/")

    hostname_prefix = str(event.get("hostnamePrefix", DEFAULT_HOSTNAME_PREFIX))
    offline_hours = int(event.get("offlineHours", DEFAULT_OFFLINE_HOURS))
    os_platform = str(event.get("osPlatform", DEFAULT_OS_PLATFORM))
    page_size = int(event.get("pageSize", DEFAULT_PAGE_SIZE))

    if os_platform not in VALID_OS_PLATFORMS:
        return build_response(400, {"error": f"osPlatform must be one of {VALID_OS_PLATFORMS}, got {os_platform!r}."})

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=offline_hours)
    server_filter = f"osPlatform eq '{os_platform}'"

    matches = []
    scanned = 0
    try:
        for ep in fetch_all_endpoints(base_url, token, server_filter, page_size):
            scanned += 1
            name = ep.get("endpointName") or ""
            if not name.lower().startswith(hostname_prefix.lower()):
                continue

            seen = last_seen(ep)
            if seen is None or seen > cutoff:
                continue  # no telemetry, or connected within the window -> still online

            epp = ep.get("eppAgent") or {}
            edr = ep.get("edrSensor") or {}
            matches.append({
                "endpointName": name,
                "agentGuid": ep.get("agentGuid", ""),
                "type": ep.get("type", ""),
                "osName": ep.get("osName", ""),
                "ipAddresses": ", ".join(ep.get("ipAddresses", []) or []),
                "eppAgentStatus": epp.get("status", ""),
                "edrSensorConnectivity": edr.get("connectivity", ""),
                "lastSeenUtc": seen.isoformat(),
                "offlineHours": round((now - seen).total_seconds() / 3600, 1),
            })
    except RuntimeError as e:
        return build_response(502, {"error": str(e)})

    matches.sort(key=lambda m: m["offlineHours"], reverse=True)

    return build_response(200, {
        "nowUtc": now.isoformat(),
        "cutoffUtc": cutoff.isoformat(),
        "hostnamePrefix": hostname_prefix,
        "offlineHours": offline_hours,
        "osPlatform": os_platform,
        "scanned": scanned,
        "matchCount": len(matches),
        "matches": matches,
        "note": ("LIST ONLY -- nothing was deleted. Review these agentGuid values, then delete "
                 "them explicitly with delete_offline_endpoints.py or Remove-OfflineEndpoints.ps1."),
    })
