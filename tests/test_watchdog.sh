#!/usr/bin/env bash
# Platform-independent host watchdog tests; no real Docker operations.
# Usage: PYTHON=python3 bash tests/test_watchdog.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python3}" "$HERE/test_watchdog.py" "$@"
