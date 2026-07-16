#!/usr/bin/env python3
"""
Delete endpoints from the Trend Vision One Endpoint Inventory.

Reads the CSV produced by pull_offline_endpoints.py (endpointName +
agentGuid columns) and removes those endpoints from Endpoint Inventory.

Note: pull_offline_endpoints.py now offers to do this immediately after
listing offline endpoints, so you don't need to run this script separately
in the common case. This script remains useful for re-running the delete
step later against a previously-saved CSV (e.g. if you said "no" during the
pull, or want to retry).

IMPORTANT
---------
  * This removes the ENDPOINT INVENTORY RECORD. It does NOT uninstall the
    agent software from the physical machine.
  * Vision One's own docs warn: shut down endpoints before using this API;
    using it on active endpoints may prevent the resulting task from working
    correctly. This tool is intended for endpoints already confirmed offline
    by pull_offline_endpoints.py.
  * This API endpoint is only available on tenants updated to the Foundation
    Services release.

SAFETY
------
The CSV is read exactly once per run, and every name printed below is from
that same in-memory list — so whatever you confirm is guaranteed to be
exactly what gets deleted (no risk of the CSV changing between a "look" run
and a separate later delete run).

After listing the endpoints, the script always asks interactively whether to
proceed (unless --verify was already passed, which skips straight past that
first question). Either way, nothing is deleted until you additionally type
"yes" at the final "will be DELETED" confirmation. If stdin isn't a
terminal (e.g. run from cron), the delete prompt is skipped entirely rather
than risk hanging or misbehaving.

USAGE
-----
  export TMV1_TOKEN="<your Vision One API key>"
  export TMV1_REGION_URL="https://api.xdr.trendmicro.com"   # optional, defaults to US

  python3 delete_offline_endpoints.py              # list, then ask whether to proceed
  python3 delete_offline_endpoints.py --verify      # skip straight to the delete confirmation

Required API key permissions: Endpoint Inventory -> Remove agents, View
"""

import argparse
import csv
import os
import sys

import endpoint_delete

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = os.environ.get("TMV1_REGION_URL", "https://api.xdr.trendmicro.com").rstrip("/")
TOKEN = os.environ.get("TMV1_TOKEN")

INPUT_CSV = "offline_iws_endpoints.csv"
DELETE_RESULTS_CSV = "delete_results_iws.csv"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def load_endpoints(csv_path):
    """Read endpointName + agentGuid pairs from the puller's CSV output."""
    if not os.path.isfile(csv_path):
        sys.exit(f"ERROR: input CSV not found: {csv_path}\n"
                  f"Run pull_offline_endpoints.py first to generate it.")

    endpoints = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("endpointName") or "").strip()
            guid = (row.get("agentGuid") or "").strip()
            if not guid:
                continue
            endpoints.append({
                "endpointName": name,
                "agentGuid": guid,
                "eppAgentProtectionManager": (row.get("eppAgentProtectionManager") or "").strip(),
            })
    return endpoints


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Delete endpoints from the Trend Vision One Endpoint Inventory. "
                     "See this script's module docstring for full details (safety model, "
                     "required permissions, etc.).")
    parser.add_argument("--csv", default=INPUT_CSV, help=f"Input CSV path (default: {INPUT_CSV})")
    parser.add_argument("--results-csv", default=DELETE_RESULTS_CSV,
                         help=f"Results CSV path (default: {DELETE_RESULTS_CSV})")
    parser.add_argument("--verify", action="store_true",
                         help="Skip the interactive prompt below and go straight to the delete "
                              "confirmation. Without this flag, the script still only acts after "
                              "you confirm interactively — nothing is deleted non-interactively.")
    args = parser.parse_args()

    endpoints = load_endpoints(args.csv)
    if not endpoints:
        print(f"No endpoints found in {args.csv}. Nothing to do.")
        return

    print(f"{len(endpoints)} endpoint(s) in {args.csv}:")
    for ep in endpoints:
        print(f"  {ep['endpointName']:<30} ({ep['agentGuid']})")

    endpoint_delete.run_delete_flow(endpoints, BASE_URL, TOKEN, args.results_csv,
                                     skip_first_prompt=args.verify)


if __name__ == "__main__":
    main()
