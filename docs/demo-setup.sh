#!/usr/bin/env bash
# Prepara un /var/log casi lleno (un disco RAM de 32 MB en macOS) para grabar docs/demo.gif
# con vhs. Uso:  docs/demo-setup.sh && PATH="/tmp/logguard-demo/bin:$PATH" vhs docs/demo.tape
# Al terminar:   hdiutil detach /Volumes/LOGDEMO
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
D=/tmp/logguard-demo
hdiutil detach /Volumes/LOGDEMO >/dev/null 2>&1 || true
rm -rf "$D"; mkdir -p "$D/bin" "$D/archive"
DEV=$(hdiutil attach -nomount ram://65536 | awk '{print $1}')
diskutil erasevolume HFS+ LOGDEMO "$DEV" >/dev/null
L=/Volumes/LOGDEMO/log
mkdir -p "$L/nginx"
mk() { head -c "$2" /dev/zero | tr '\0' 'x' > "$L/$1"; }
mk syslog 4000000;           mk syslog.1 8000000
head -c 2000000 /dev/zero | tr '\0' 'x' | gzip -1 > "$L/syslog.2.gz"
mk kern.log 300000;          mk kern.log.1 5000000
mk auth.log 150000;          mk auth.log.1 2000000
mk daemon.log-20261110 3000000
mk nginx/access.log 700000;  mk nginx/access.log.1 5000000; mk nginx/error.log.1 1000000
cat > "$D/logguard.conf" <<CONF
LOG_DIR=$L
ARCHIVE_DIR=$D/archive
MIN_FREE_PERCENT=15
TARGET_FREE_PERCENT=25
JOURNAL_VACUUM_SIZE=
CONF
# En macOS no existe /run: el lock y la configuración se redirigen
cat > "$D/bin/logguard" <<SH
#!/usr/bin/env python3
import sys; sys.path.insert(0, "$REPO")
import logguard
logguard.LOCK_FILE = "$D/lock"; logguard.DEFAULT_CONFIG = "$D/logguard.conf"
sys.exit(logguard.main())
SH
chmod +x "$D/bin/logguard"
