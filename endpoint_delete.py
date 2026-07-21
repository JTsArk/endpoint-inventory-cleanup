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
import json
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
    "likely_deleted": "Likely already deleted (NotFound on a retried submission) -- verify in Audit Logs",
}

# Retry/backoff. This default set (429/5xx) is used for read-only GET calls
# (pulling endpoints, polling task status) as well as the delete-submission
# POST -- see submit_delete_batch.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)

# Set by request_with_backoff after each call to the number of attempts it
# took (1 = succeeded on the first try, no retry). submit_delete_batch reads
# this immediately after its own call to tell a NotFound on a retried
# submission (likely already deleted by an earlier, response-lost attempt)
# apart from a NotFound on a first-try submission (a real error).
_LAST_REQUEST_ATTEMPTS = 0


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def request_with_backoff(session, method, url, headers=None, params=None, json_body=None, timeout=60,
                          retryable_status_codes=RETRYABLE_STATUS_CODES):
    """Request with retry + exponential backoff. Honors the Retry-After header
    when the API sends one; otherwise backs off exponentially (1s, 2s, 4s,
    ...) with a little jitter to avoid retry storms. Non-retryable errors are
    returned immediately for the caller to handle.

    `retryable_status_codes` defaults to 429/5xx. This also covers the
    delete-submission POST: a 500/502/503/504 there can mean the request
    already reached and was processed by the server, with only the
    *response* lost in transit -- but live-verified behavior confirms
    resubmitting is safe even then. An agentGuid the first attempt actually
    deleted comes back "404 NotFound" on retry (not a duplicate action), and
    the API documents a "TaskError: Delete task already in progress"
    conflict for one still mid-flight. A submission that still fails after
    exhausting retries doesn't abort the run either -- see
    submit_delete_batch, which records a not_submitted row per endpoint in
    that batch instead.
    """
    global _LAST_REQUEST_ATTEMPTS
    for attempt in range(MAX_RETRIES + 1):
        resp = session.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
        if resp.status_code not in retryable_status_codes or attempt == MAX_RETRIES:
            _LAST_REQUEST_ATTEMPTS = attempt + 1
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

    _LAST_REQUEST_ATTEMPTS = MAX_RETRIES + 1
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
      {"taskId": str, "httpStatus": 202, "errorCode": None, "errorMessage": None, "submittedAfterRetry": bool}  on 202 Accepted, or
      {"taskId": None, "httpStatus": int, "errorCode": str, "errorMessage": str, "submittedAfterRetry": bool}  otherwise.

    submittedAfterRetry is True when this batch's call only succeeded after 1+
    retries. It lets the caller tell a NotFound that shows up on a retried
    call (the agentGuid was most likely deleted by an earlier attempt in this
    same batch whose response got lost) apart from a NotFound on a first-try
    call (a real, pre-existing problem) -- see run_delete_flow.

    A batch-level failure (the submission POST itself errors, or the response
    doesn't line up 1:1 with `batch`) does NOT abort the run -- it returns a
    synthetic not_submitted dict (errorCode "SubmitError") for every item in
    the batch instead, so the caller still gets a complete CSV covering this
    batch and goes on to attempt any remaining batches.
    """
    url = f"{base_url}{DELETE_PATH}"
    body = [{"agentGuid": ep["agentGuid"]} for ep in batch]
    # Retries 429/5xx, same as read-only calls -- see the note on request_with_backoff.
    resp = request_with_backoff(session, "POST", url, headers=headers, json_body=body)

    if resp.status_code != 207:
        note = ""
        if resp.status_code in (500, 502, 503, 504):
            note = (f" This batch was already retried {MAX_RETRIES} time(s) and still failed -- "
                     "check Vision One's Audit Logs for this batch to see if it's a persistent "
                     "issue before re-running. Resubmitting is safe (an endpoint the earlier "
                     "attempt already deleted comes back 404 NotFound rather than a duplicate "
                     "action).")
        message = f"API error {resp.status_code} submitting delete batch: {resp.text}{note}"
        print(message, file=sys.stderr)
        return [{"taskId": None, "httpStatus": resp.status_code, "errorCode": "SubmitError",
                  "errorMessage": message, "submittedAfterRetry": False} for _ in batch]

    was_retried = _LAST_REQUEST_ATTEMPTS > 1

    results = resp.json()
    if len(results) != len(batch):
        message = (f"API returned {len(results)} results for a batch of {len(batch)} — cannot "
                   f"reliably match results to endpoints.")
        print(message, file=sys.stderr)
        return [{"taskId": None, "httpStatus": None, "errorCode": "SubmitError",
                  "errorMessage": message, "submittedAfterRetry": False} for _ in batch]

    out = []
    for item in results:
        status = item.get("status")
        if status == 202:
            operation_location = next(
                (h["value"] for h in item.get("headers", []) if h.get("name") == "Operation-Location"),
                None,
            )
            task_id = task_id_from_operation_location(operation_location) if operation_location else None
            out.append({"taskId": task_id, "httpStatus": 202, "errorCode": None, "errorMessage": None,
                        "submittedAfterRetry": was_retried})
        else:
            # code/message live directly on body -- e.g.
            #   {"status":404,"body":{"code":"NotFound","message":"Endpoint not found"}}
            # NOT nested under body.error as the bundled OpenAPI spec claims (confirmed against
            # the live API; the spec is wrong here). body has also been observed as a
            # JSON-encoded string instead of a parsed object. Try the real shape first, fall back
            # to the spec's documented shape in case some other error path actually does nest it,
            # and if neither yields anything, fall back to the raw item so a failure is never
            # silently reported with a blank message.
            item_body = item.get("body")
            if isinstance(item_body, str):
                try:
                    item_body = json.loads(item_body)
                except (ValueError, TypeError):
                    item_body = None
            item_body = item_body or {}
            code = item_body.get("code")
            message = item_body.get("message")
            if not (code or message):
                nested_err = item_body.get("error") or {}
                code = nested_err.get("code")
                message = nested_err.get("message")

            if code or message:
                out.append({"taskId": None, "httpStatus": status, "errorCode": code, "errorMessage": message,
                            "submittedAfterRetry": was_retried})
            else:
                raw = json.dumps(item.get("body")) if item.get("body") is not None else "(no error body returned)"
                out.append({"taskId": None, "httpStatus": status, "errorCode": None, "errorMessage": raw,
                            "submittedAfterRetry": was_retried})
    return out


def poll_task(session, base_url, headers, task_id):
    """Poll a delete task until it reaches a terminal status or times out.

    Returns (status, httpStatus, errorCode, errorMessage).
    """
    url = f"{base_url}{TASK_PATH.format(id=task_id)}"
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS

    while True:
        resp = request_with_backoff(session, "GET", url, headers=headers)
        if resp.status_code != 200:
            return "unknown", resp.status_code, None, resp.text

        body = resp.json()
        status = body.get("status")
        if status in ("succeeded", "failed"):
            error = body.get("error") or {}
            error_code = error.get("code") if status == "failed" else None
            error_msg = error.get("message", "") if status == "failed" else ""
            return status, None, error_code, error_msg

        if time.monotonic() >= deadline:
            return "timeout", None, None, f"still {status!r} after {POLL_TIMEOUT_SECONDS}s"

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
        if submitted["httpStatus"] != 202:
            # A NotFound on a call that only succeeded after a retry most likely means an
            # earlier attempt in this same batch already deleted it and its response was
            # lost -- not a real error. A NotFound on a first-try call is a genuine problem
            # (stale CSV, invalid agentGuid, deleted outside this run). See submit_delete_batch.
            final_status = ("likely_deleted" if submitted["errorCode"] == "NotFound" and submitted.get("submittedAfterRetry")
                             else "not_submitted")
            print(f"  {ep['endpointName']:<30} -> submit {final_status}: "
                  f"{submitted['httpStatus']} {submitted['errorCode']}: {submitted['errorMessage']}")
            submitted["finalStatus"] = final_status
            continue

        final_status, http_status, error_code, error_msg = poll_task(session, base_url, headers, submitted["taskId"])
        submitted["finalStatus"] = final_status
        submitted["httpStatus"] = http_status
        submitted["errorCode"] = error_code
        submitted["errorMessage"] = error_msg
        suffix = f": {error_msg}" if error_msg else ""
        print(f"  {ep['endpointName']:<30} -> task {final_status}{suffix}")

    # Write the audit-trail CSV. httpStatus/errorCode/errorMessage are kept as
    # separate columns (rather than one packed string) so a short/empty API
    # message doesn't collapse into something like "400 :" -- which some CSV
    # viewers (Excel) misread as a duration.
    fieldnames = ["endpointName", "agentGuid", "eppAgentProtectionManager", "taskId", "finalStatus",
                  "httpStatus", "errorCode", "errorMessage", "actionTaken"]
    with open(results_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ep, r in zip(endpoints, results):
            final_status = r.get("finalStatus", "not_submitted")
            writer.writerow({
                "endpointName": ep["endpointName"],
                "agentGuid": ep["agentGuid"],
                "eppAgentProtectionManager": ep.get("eppAgentProtectionManager", ""),
                "taskId": r.get("taskId") or "",
                "finalStatus": final_status,
                "httpStatus": r.get("httpStatus") if r.get("httpStatus") is not None else "",
                "errorCode": r.get("errorCode") or "",
                "errorMessage": r.get("errorMessage") or "",
                "actionTaken": ACTION_TAKEN_BY_STATUS.get(final_status, final_status),
            })

    succeeded = sum(1 for r in results if r.get("finalStatus") == "succeeded")
    failed = sum(1 for r in results if r.get("finalStatus") in ("failed", "not_submitted", "unknown"))
    timed_out = sum(1 for r in results if r.get("finalStatus") == "timeout")
    likely_deleted = sum(1 for r in results if r.get("finalStatus") == "likely_deleted")
    print(f"\n{succeeded} succeeded, {failed} failed, {timed_out} timed out, "
          f"{likely_deleted} likely already deleted (verify). Wrote {len(results)} rows to {results_csv}")
    return True
