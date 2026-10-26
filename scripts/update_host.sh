#!/usr/bin/env sh
# Run under systemd (Type=simple), or invoke with --once for one spool pass.
# SESSION_SECRET is read by Python from the protected project .env; never source
# .env as a shell script or expose credentials on the command line.
set -eu
updater_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
umask 077
if [ -x "$updater_root/data/update/host-env/bin/python" ]; then
    updater_python="$updater_root/data/update/host-env/bin/python"
elif [ -x "$updater_root/.venv/bin/python" ]; then
    updater_python="$updater_root/.venv/bin/python"
else
    updater_python=python3
fi
exec "$updater_python" -u "$updater_root/scripts/update_host.py" --root "$updater_root" --mode compose "$@"
