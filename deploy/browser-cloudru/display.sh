#!/bin/bash
set -eu
umask 077
base=/opt/portable-context-browser
if test "${1:-}" = client; then
    exec /usr/sbin/runuser -u pcowner -- env HOME=/home/pcowner DISPLAY=:0 XAUTHORITY="$XAUTHORITY" LANG=C.UTF-8 \
        "$base/venv/bin/python" "$base/source/deploy/browser-cloudru/console.py"
fi
install -d -m 0700 -o pcowner -g pcowner /run/pc-browser-console
export XAUTHORITY=/run/pc-browser-console/Xauthority
runuser -u pcowner -- xauth -f "$XAUTHORITY" add :0 . "$(mcookie)"
exec xinit /bin/bash "$base/source/deploy/browser-cloudru/display.sh" client -- \
    /usr/lib/xorg/Xorg :0 vt7 -nolisten tcp -auth "$XAUTHORITY"
