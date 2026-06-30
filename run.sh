#!/usr/bin/env bash
# Convenience wrapper: load secrets from .env and run the endpoint puller.
# Usage:  ./run.sh
set -euo pipefail

# Always run from this script's own directory, so it works no matter where
# you invoke it from.
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill in TMV1_TOKEN / TMV1_REGION_URL." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "ERROR: .venv not found. Create it with:  python3 -m venv .venv && ./.venv/bin/pip install requests" >&2
  exit 1
fi

# Load .env and export every variable in it so the Python script can read them.
set -a
# shellcheck disable=SC1091
source .env
set +a

exec ./.venv/bin/python pull_offline_w11_endpoints.py "$@"
