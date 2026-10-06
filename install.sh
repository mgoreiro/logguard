#!/bin/sh
# Install (or --uninstall) logguard. Run as root on the target board.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
cd "$(dirname "$0")"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now logguard.timer 2>/dev/null || true
    rm -f /etc/systemd/system/logguard.service /etc/systemd/system/logguard.timer \
          /usr/sbin/logguard
    systemctl daemon-reload
    echo "removed (kept /etc/logguard.conf and /var/lib/logguard)"
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

install -m 755 logguard.py /usr/sbin/logguard
[ -f /etc/logguard.conf ] || install -m 644 logguard.conf /etc/logguard.conf
install -d -m 755 /var/lib/logguard/archive
install -m 644 systemd/logguard.service systemd/logguard.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now logguard.timer
echo "installed. Check: logguard --status ; journalctl -u logguard"
