#!/usr/bin/env python3
"""
Shared helpers for deleting Trend Vision One endpoints from Endpoint
Inventory. Used by both pull_offline_endpoints.py (offers to delete
immediately after listing offline endpoints) and delete_offline_endpoints.py
(standalone re-run against a previously-saved CSV).

API used: POST /v3.0/endpointSecurity/endpoints/delete, GET
/v3.0/endpointSecurity/tasks/{id}  (Endpoint Security -> Remove endpoints)
"""

import csv
import random
import sys
import time
from urllib.parse import urlparse

import requests

DELETE_PATH = "/v3.0/endpointSecurity/endpoints/delete"
TASK_PATH = "/v3.0/endpointSecurity/tasks/{id}"

BATCH_SIZE = 1000   # API max items per delete call

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 120

# Human-readable summary written to the "actionTaken" results-CSV column, keyed by finalStatus.
ACTION_TAKEN_BY_STATUS = {
    "succeeded": "Deleted from Endpoint Inventory",
    "failed": "Delete failed",
    "timeout": "Delete timed out",
    "not_submitted": "Not submitted (API error)",
    "unknown": "Delete status unknown (poll failed)",
}

# Retry/backoff for throttled (429) or transient (5xx) API responses.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def request_with_backoff(session, method, url, headers=None, params=None, json_body=None, timeout=60):
    """Request with retry + exponential backoff on 429 (throttled) / transient 5xx.

    Honors the Retry-After header when the API sends one; otherwise backs off
    exponentially (1s, 2s, 4s, ...) with a little jitter to avoid retry storms.
    Non-retryable errors are returned immediately for the caller to handle.
    """
    for attempt in range(MAX_RETRIES + 1):
        resp = session.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
        if resp.status_code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
        else:
            delay = BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)

        print(f"  got {resp.status_code}, retrying in {delay:.1f}s "
              f"(attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
        time.sleep(delay)

    return resp  # unreachable, but keeps type-checkers happy


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def task_id_from_operation_location(url):
    """Extract the task id from an Operation-Location header URL."""
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def submit_delete_batch(session, base_url, headers, batch):
    """POST one batch (<=1000 items) to /endpoints/delete.

    Returns a list of per-item dicts aligned with `batch`:
      {"taskId": str, "error": None}  on 202 Accepted, or
      {"taskId": None, "error": "<code>: <message>"}  otherwise.
    """
    url = f"{base_url}{DELETE_PATH}"
    body = [{"agentGuid": ep["agentGuid"]} for ep in batch]
    resp = request_with_backoff(session, "POST", url, headers=headers, json_body=body)

    if resp.status_code != 207:
        sys.exit(f"API error {resp.status_code} submitting delete batch: {resp.text}")

    results = resp.json()
    if len(results) != len(batch):
        sys.exit(f"API returned {len(results)} results for a batch of {len(batch)} — cannot "
                  f"reliably match results to endpoints.")

    out = []
    for item in results:
        status = item.get("status")
        if status == 202:
            operation_location = next(
                (h["value"] for h in item.get("headers", []) if h.get("name") == "Operation-Location"),
                None,
            )
            task_id = task_id_from_operation_location(operation_location) if operation_location else None
            out.append({"taskId": task_id, "error": None})
        else:
            body_err = (item.get("body") or {}).get("error", {})
            err_msg = f"{status} {body_err.get('code', '')}: {body_err.get('message', '')}".strip()
            out.append({"taskId": None, "error": err_msg})
    return out


def poll_task(session, base_url, headers, task_id):
    """Poll a delete task until it reaches a terminal status or times out."""
    url = f"{base_url}{TASK_PATH.format(id=task_id)}"
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS

    while True:
        resp = request_with_backoff(session, "GET", url, headers=headers)
        if resp.status_code != 200:
            return "unknown", f"{resp.status_code}: {resp.text}"

        body = resp.json()
        status = body.get("status")
        if status in ("succeeded", "failed"):
            error = body.get("error") or {}
            error_msg = error.get("message", "") if status == "failed" else ""
            return status, error_msg

        if time.monotonic() >= deadline:
            return "timeout", f"still {status!r} after {POLL_TIMEOUT_SECONDS}s"

        time.sleep(POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Confirm-then-delete flow
# --------------------------------------------------------------------------- #

def run_delete_flow(endpoints, base_url, token, results_csv, skip_first_prompt=False):
    """Interactive confirm-then-delete flow shared by the puller (called
    right after listing, using the same in-memory endpoint list) and the
    standalone delete script (called after loading a saved CSV).

    `endpoints` is a list of {"endpointName": ..., "agentGuid": ...} dicts.
    Returns True if a delete was attempted (regardless of outcome), False if
    the user declined, the session isn't interactive, or the token is missing.
    """
    if not endpoints:
        return False

    # A destructive action gated on typed confirmation must never run against
    # a non-interactive stdin (cron, CI, piped input) — input() would either
    # raise EOFError or silently consume unrelated piped data. Bail out safely
    # instead, regardless of skip_first_prompt.
    if not sys.stdin.isatty():
        print(f"\nNon-interactive session detected; skipping the delete prompt "
              f"for these {len(endpoints)} endpoint(s). Re-run interactively "
              f"(e.g. delete_offline_endpoints.py --verify) to delete them.")
        return False

    if not skip_first_prompt:
        answer = input(f"\nDelete these {len(endpoints)} endpoint(s) now? "
                        f"Type 'yes' to continue (anything else exits with no changes): ").strip().lower()
        if answer != "yes":
            print("No changes made.")
            return False

    if not token:
        print("\nERROR: set the TMV1_TOKEN environment variable to your Vision One API key.",
              file=sys.stderr)
        return False

    print(f"\nThe following {len(endpoints)} endpoint(s) will be DELETED from Endpoint Inventory:")
    for ep in endpoints:
        print(f"  {ep['endpointName']:<30} (agentGuid {ep['agentGuid']})")

    confirmation = input("\nType 'yes' to proceed: ").strip().lower()
    if confirmation != "yes":
        print("Aborted. No endpoints were deleted.")
        return False

    session = requests.Session()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json;charset=utf-8",
    }

    # Submit in batches (API max 1000 items/call), tracking a result row per endpoint.
    results = []  # aligned 1:1 with `endpoints`
    for batch in chunked(endpoints, BATCH_SIZE):
        results.extend(submit_delete_batch(session, base_url, headers, batch))

    # Poll each accepted task to a terminal status, printing progress by name.
    print()
    for ep, submitted in zip(endpoints, results):
        if submitted["error"]:
            print(f"  {ep['endpointName']:<30} -> submit failed: {submitted['error']}")
            submitted["finalStatus"] = "not_submitted"
            submitted["errorMessage"] = submitted["error"]
            continue

        final_status, error_msg = poll_task(session, base_url, headers, submitted["taskId"])
        submitted["finalStatus"] = final_status
        submitted["errorMessage"] = error_msg
        suffix = f": {error_msg}" if error_msg else ""
        print(f"  {ep['endpointName']:<30} -> task {final_status}{suffix}")

    # Write the audit-trail CSV.
    fieldnames = ["endpointName", "agentGuid", "taskId", "finalStatus", "errorMessage", "actionTaken"]
    with open(results_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ep, r in zip(endpoints, results):
            final_status = r.get("finalStatus", "not_submitted")
            writer.writerow({
                "endpointName": ep["endpointName"],
                "agentGuid": ep["agentGuid"],
                "taskId": r.get("taskId") or "",
                "finalStatus": final_status,
                "errorMessage": r.get("errorMessage", ""),
                "actionTaken": ACTION_TAKEN_BY_STATUS.get(final_status, final_status),
            })

    succeeded = sum(1 for r in results if r.get("finalStatus") == "succeeded")
    failed = sum(1 for r in results if r.get("finalStatus") in ("failed", "not_submitted", "unknown"))
    timed_out = sum(1 for r in results if r.get("finalStatus") == "timeout")
    print(f"\n{succeeded} succeeded, {failed} failed, {timed_out} timed out. "
          f"Wrote {len(results)} rows to {results_csv}")
    return True
