#!/bin/sh
# relay-desk native acceptance (macOS / Linux). Thin wrapper around native_runner.py.
#   sh acceptance/run-native.sh                 # full suite + probes + loopback
#   sh acceptance/run-native.sh --quick         # skip the 35 s ledger-lock probe
# Use PYTHON=/path/to/python3 to pick an interpreter.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
cd "$HERE/.." || exit 2
exec "$PY" "$HERE/native_runner.py" --invoked-via sh "$@"
