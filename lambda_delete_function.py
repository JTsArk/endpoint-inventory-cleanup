#!/usr/bin/env python3
"""
On-demand AWS Lambda: delete endpoints from the Trend Vision One Endpoint
Inventory. Companion to lambda_function.py (the list-only scanner) -- reads
the CSV that scanner wrote to S3 (endpointName + agentGuid columns) and
removes those endpoints.

SAFETY MODEL: invoke WITHOUT "confirm": true and this only PREVIEWS -- it
reads and returns the endpoint list from S3 without calling the delete API
at all. Nothing is ever deleted on a first/default invoke. You must
explicitly re-invoke with "confirm": true (same s3Bucket/s3Key) to actually
delete. This mirrors the local scripts' two-step "type yes twice" prompt,
just as two separate deliberate invokes instead of two Read-Host prompts.

IMPORTANT: this removes the ENDPOINT INVENTORY RECORD. It does NOT
uninstall the agent software from the physical machine. Vision One's own
docs warn: shut down endpoints before using this API. This tool is intended
for endpoints already confirmed offline by the scanner Lambda.

Invoke to preview (no changes made):
  aws lambda invoke --function-name offline-endpoint-deleter \
    --cli-binary-format raw-in-base64-out \
    --payload '{"s3Bucket": "my-bucket", "s3Key": "scans/iws-20260710T120000Z.csv"}' \
    response.json

Invoke to actually delete (same payload, plus confirm):
  aws lambda invoke --function-name offline-endpoint-deleter \
    --cli-binary-format raw-in-base64-out \
    --payload '{"s3Bucket": "my-bucket", "s3Key": "scans/iws-20260710T120000Z.csv", "confirm": true}' \
    response.json

Required event fields: s3Bucket, s3Key -- any CSV with endpointName +
agentGuid columns (normally the one the scanner Lambda just wrote).
Optional event field: confirm (bool, default false -- preview only)

Required Lambda environment variables:
  TMV1_TOKEN         Vision One API key (Endpoint Inventory -> Remove agents, View)
  TMV1_REGION_URL    optional, defaults to the US endpoint

Optional Lambda environment variables:
  RESULTS_BUCKET     where the delete-results audit-trail CSV is written;
                     defaults to the input s3Bucket if unset
  SNS_TOPIC_ARN      if set, publishes a completion summary after a real
                     (confirm:true) delete -- not sent for preview invokes
"""

import csv
import io
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

DEFAULT_BASE_URL = "https://api.xdr.trendmicro.com"
DELETE_PATH = "/v3.0/endpointSecurity/endpoints/delete"
TASK_PATH = "/v3.0/endpointSecurity/tasks/{id}"

BATCH_SIZE = 1000   # API max items per delete call

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 120

ACTION_TAKEN_BY_STATUS = {
    "succeeded": "Deleted from Endpoint Inventory",
    "failed": "Delete failed",
    "timeout": "Delete timed out",
    "not_submitted": "Not submitted (API error)",
    "unknown": "Delete status unknown (poll failed)",
}

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)

RESULTS_CSV_FIELDNAMES = ["endpointName", "agentGuid", "taskId", "finalStatus", "errorMessage", "actionTaken"]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def request_with_backoff(url, headers, method="GET", body=None):
    """Request with retry + exponential backoff on 429 (throttled) / transient 5xx.

    Honors the Retry-After header when the API sends one; otherwise backs off
    exponentially (1s, 2s, 4s, ...) with a little jitter to avoid retry storms.
    Returns (status_code, headers, body_bytes) -- callers check status_code.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, resp_headers, resp_body = e.code, e.headers, e.read()

        if status not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
            return status, resp_headers, resp_body

        retry_after = resp_headers.get("Retry-After") if resp_headers else None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
        else:
            delay = BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
        time.sleep(delay)

    return status, resp_headers, resp_body  # unreachable, keeps type-checkers happy


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def task_id_from_operation_location(url):
    return urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def submit_delete_batch(base_url, headers, batch):
    """POST one batch (<=1000 items) to /endpoints/delete.

    Returns a list of per-item dicts aligned with `batch`:
      {"taskId": str, "error": None}  on 202 Accepted, or
      {"taskId": None, "error": "<code>: <message>"}  otherwise.
    """
    url = f"{base_url}{DELETE_PATH}"
    body = [{"agentGuid": ep["agentGuid"]} for ep in batch]
    status, _, resp_body = request_with_backoff(url, headers, method="POST", body=body)

    if status != 207:
        raise RuntimeError(f"API error {status} submitting delete batch: {resp_body.decode('utf-8', 'replace')}")

    results = json.loads(resp_body)
    if len(results) != len(batch):
        raise RuntimeError(f"API returned {len(results)} results for a batch of {len(batch)} -- cannot "
                            f"reliably match results to endpoints.")

    out = []
    for item in results:
        status_code = item.get("status")
        if status_code == 202:
            operation_location = next(
                (h["value"] for h in item.get("headers", []) if h.get("name") == "Operation-Location"),
                None,
            )
            task_id = task_id_from_operation_location(operation_location) if operation_location else None
            out.append({"taskId": task_id, "error": None})
        else:
            body_err = (item.get("body") or {}).get("error", {})
            err_msg = f"{status_code} {body_err.get('code', '')}: {body_err.get('message', '')}".strip()
            out.append({"taskId": None, "error": err_msg})
    return out


def poll_task(base_url, headers, task_id):
    """Poll a delete task until it reaches a terminal status or times out."""
    url = f"{base_url}{TASK_PATH.format(id=task_id)}"
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS

    while True:
        status, _, resp_body = request_with_backoff(url, headers)
        if status != 200:
            return "unknown", f"{status}: {resp_body.decode('utf-8', 'replace')}"

        body = json.loads(resp_body)
        task_status = body.get("status")
        if task_status in ("succeeded", "failed"):
            error = body.get("error") or {}
            error_msg = error.get("message", "") if task_status == "failed" else ""
            return task_status, error_msg

        if time.monotonic() >= deadline:
            return "timeout", f"still {task_status!r} after {POLL_TIMEOUT_SECONDS}s"

        time.sleep(POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# S3 / SNS helpers
# --------------------------------------------------------------------------- #

def load_endpoints_from_s3(bucket, key):
    obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    text = obj["Body"].read().decode("utf-8")
    endpoints = []
    for row in csv.DictReader(io.StringIO(text)):
        guid = (row.get("agentGuid") or "").strip()
        if not guid:
            continue
        endpoints.append({"endpointName": (row.get("endpointName") or "").strip(), "agentGuid": guid})
    return endpoints


def write_results_csv_to_s3(bucket, key, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RESULTS_CSV_FIELDNAMES)
    writer.writeheader()
    writer.writerows(rows)
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=buf.getvalue().encode("utf-8"),
                                   ContentType="text/csv")


def publish_completion_summary(topic_arn, s3_bucket, s3_key, succeeded, failed, timed_out, total,
                                results_bucket, results_key):
    subject = f"Endpoint delete complete: {succeeded} succeeded, {failed} failed, {timed_out} timed out"[:100]
    lines = [
        f"Deleted from s3://{s3_bucket}/{s3_key}",
        f"{succeeded} succeeded, {failed} failed, {timed_out} timed out out of {total} total.",
    ]
    if results_key:
        lines.append(f"Full audit trail: s3://{results_bucket}/{results_key}")
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

    s3_bucket = event.get("s3Bucket")
    s3_key = event.get("s3Key")
    confirm = bool(event.get("confirm", False))

    if not s3_bucket or not s3_key:
        return build_response(400, {"error": "s3Bucket and s3Key are required (the CSV written by the "
                                               "scanner Lambda, or any CSV with endpointName + agentGuid "
                                               "columns)."})

    token = os.environ.get("TMV1_TOKEN")
    if not token:
        return build_response(500, {"error": "TMV1_TOKEN environment variable is not set."})
    base_url = (os.environ.get("TMV1_REGION_URL") or DEFAULT_BASE_URL).rstrip("/")

    try:
        endpoints = load_endpoints_from_s3(s3_bucket, s3_key)
    except Exception as e:
        return build_response(502, {"error": f"Failed to read s3://{s3_bucket}/{s3_key}: {e}"})

    if not endpoints:
        return build_response(200, {"count": 0, "message": f"No endpoints found in s3://{s3_bucket}/{s3_key}. "
                                                             f"Nothing to do."})

    # PREVIEW: without confirm:true, only ever report what WOULD be deleted --
    # the delete API is never called. This is the safety gate: nothing is
    # deleted on a first/default invoke, mirroring the local scripts' two-step
    # confirmation as two separate deliberate invokes.
    if not confirm:
        return build_response(200, {
            "preview": True,
            "count": len(endpoints),
            "endpoints": endpoints,
            "message": (f"PREVIEW ONLY -- nothing was deleted. {len(endpoints)} endpoint(s) would be "
                        f"removed from Endpoint Inventory. Re-invoke with the same s3Bucket/s3Key plus "
                        f"\"confirm\": true to actually delete them."),
        })

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json;charset=utf-8",
    }

    results = []
    try:
        for batch in chunked(endpoints, BATCH_SIZE):
            results.extend(submit_delete_batch(base_url, headers, batch))
    except RuntimeError as e:
        return build_response(502, {"error": str(e)})

    for ep, submitted in zip(endpoints, results):
        if submitted["error"]:
            submitted["finalStatus"] = "not_submitted"
            submitted["errorMessage"] = submitted["error"]
            continue
        final_status, error_msg = poll_task(base_url, headers, submitted["taskId"])
        submitted["finalStatus"] = final_status
        submitted["errorMessage"] = error_msg

    rows = []
    for ep, r in zip(endpoints, results):
        final_status = r.get("finalStatus", "not_submitted")
        rows.append({
            "endpointName": ep["endpointName"],
            "agentGuid": ep["agentGuid"],
            "taskId": r.get("taskId") or "",
            "finalStatus": final_status,
            "errorMessage": r.get("errorMessage", ""),
            "actionTaken": ACTION_TAKEN_BY_STATUS.get(final_status, final_status),
        })

    succeeded = sum(1 for r in rows if r["finalStatus"] == "succeeded")
    failed = sum(1 for r in rows if r["finalStatus"] in ("failed", "not_submitted", "unknown"))
    timed_out = sum(1 for r in rows if r["finalStatus"] == "timeout")

    results_bucket = os.environ.get("RESULTS_BUCKET") or s3_bucket
    stem, _, ext = s3_key.rpartition(".")
    results_key = f"{stem or s3_key}-delete-results.{ext}" if stem else f"{s3_key}-delete-results.csv"
    try:
        write_results_csv_to_s3(results_bucket, results_key, rows)
    except Exception:
        results_key = None  # don't fail the whole delete just because the audit write failed

    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if sns_topic_arn:
        publish_completion_summary(sns_topic_arn, s3_bucket, s3_key, succeeded, failed, timed_out,
                                    len(rows), results_bucket, results_key)

    return build_response(200, {
        "count": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "timedOut": timed_out,
        "resultsBucket": results_bucket if results_key else None,
        "resultsKey": results_key,
        "results": rows,
    })
