#!/usr/bin/env bash
# Host watchdog compatibility wrapper; install both adjacent scripts on Linux:
#   sudo install -m 755 guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
#   sudo install -m 644 guardian-watchdog.py /usr/local/bin/guardian-watchdog.py
#   sudo install -m 644 guardian-watchdog.service /etc/systemd/system/
#   sudo install -m 644 guardian-watchdog.timer /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now guardian-watchdog.timer
# Windows foreground worker (no automatic installation):
#   python guardian-watchdog.py --poll-interval 30
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${GUARDIAN_PYTHON:-${PYTHON:-python3}}" "$HERE/guardian-watchdog.py" "$@"
