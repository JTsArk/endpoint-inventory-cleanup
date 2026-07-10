#!/usr/bin/env python3
"""
On-demand AWS Lambda: scan Trend Vision One Endpoint Inventory for endpoints
matching a host-name prefix that have been offline for a while, and return
the list. LIST ONLY -- this function never deletes anything. Review its
output (or the S3/SNS notification, if configured), then delete separately
and explicitly -- either with delete_offline_endpoints.py /
Remove-OfflineEndpoints.ps1 locally, or by invoking the companion
lambda_delete_function.py Lambda -- there is no automatic or chained delete
step.

Only urllib is required (no dependency layer needed for the API calls);
boto3 is used for the optional S3/SNS integration and ships built into every
AWS-provided Python Lambda runtime, so this still deploys as a single file
with no packaging step: zip this file alone and upload it.

Invoke on demand, e.g.:
  aws lambda invoke --function-name offline-endpoint-scanner \
    --payload '{"hostnamePrefix": "iws", "offlineHours": 8}' \
    --cli-binary-format raw-in-base64-out response.json

Event overrides (all optional; fall back to the defaults below):
  hostnamePrefix, offlineHours, osPlatform, pageSize

Required Lambda environment variables:
  TMV1_TOKEN        Vision One API key (Endpoint Inventory -> View)
  TMV1_REGION_URL   optional, defaults to the US endpoint

Optional Lambda environment variables:
  RESULTS_BUCKET    if set, writes the match list as a CSV to
                    s3://RESULTS_BUCKET/scans/<prefix>-<timestamp>.csv
  SNS_TOPIC_ARN     if set, publishes a summary (+ S3 link, if RESULTS_BUCKET
                    is also set) to this topic after every scan, including
                    zero-match runs
"""

import csv
import io
import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

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
# S3 / SNS helpers
# --------------------------------------------------------------------------- #

RESULTS_CSV_FIELDNAMES = [
    "endpointName", "agentGuid", "type", "osName", "ipAddresses",
    "eppAgentStatus", "edrSensorConnectivity", "lastSeenUtc", "offlineHours",
]


def write_matches_csv_to_s3(bucket, key, matches):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RESULTS_CSV_FIELDNAMES)
    writer.writeheader()
    writer.writerows(matches)
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=buf.getvalue().encode("utf-8"),
                                   ContentType="text/csv")


def publish_scan_summary(topic_arn, hostname_prefix, offline_hours, os_platform, scanned, matches,
                          s3_bucket, s3_key):
    subject = f"Offline endpoint scan: {len(matches)} match(es) for '{hostname_prefix}'"[:100]
    lines = [
        f"Scanned {scanned} {os_platform} endpoint(s); {len(matches)} match "
        f"(host starts with '{hostname_prefix}' AND offline >= {offline_hours}h).",
        "",
    ]
    if s3_key:
        lines.append(f"Full list: s3://{s3_bucket}/{s3_key}")
        lines.append("")

    preview_count = 25
    for m in matches[:preview_count]:
        lines.append(f"  {m['endpointName']:<30} offline {m['offlineHours']}h  ({m['agentGuid']})")
    if len(matches) > preview_count:
        lines.append(f"  ... and {len(matches) - preview_count} more (see the S3 CSV above)")

    if matches:
        lines.append("")
        lines.append("Nothing was deleted. To delete, invoke offline-endpoint-deleter with:")
        lines.append(f'  {{"s3Bucket": "{s3_bucket}", "s3Key": "{s3_key}"}}')
        lines.append("to preview, then again with \"confirm\": true to actually delete.")

    boto3.client("sns").publish(TopicArn=topic_arn, Subject=subject, Message="\n".join(lines))


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

    s3_bucket = os.environ.get("RESULTS_BUCKET")
    s3_key = None
    if matches and s3_bucket:
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        s3_key = f"scans/{hostname_prefix.lower()}-{timestamp}.csv"
        write_matches_csv_to_s3(s3_bucket, s3_key, matches)

    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if sns_topic_arn:
        publish_scan_summary(sns_topic_arn, hostname_prefix, offline_hours, os_platform, scanned,
                              matches, s3_bucket, s3_key)

    return build_response(200, {
        "nowUtc": now.isoformat(),
        "cutoffUtc": cutoff.isoformat(),
        "hostnamePrefix": hostname_prefix,
        "offlineHours": offline_hours,
        "osPlatform": os_platform,
        "scanned": scanned,
        "matchCount": len(matches),
        "matches": matches,
        "s3Bucket": s3_bucket if s3_key else None,
        "s3Key": s3_key,
        "note": ("LIST ONLY -- nothing was deleted. Review these agentGuid values, then delete them "
                 "explicitly with delete_offline_endpoints.py, Remove-OfflineEndpoints.ps1, or by "
                 "invoking offline-endpoint-deleter with this s3Bucket/s3Key."),
    })
