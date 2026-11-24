#!/bin/bash
# Run by cloud-init on a fresh Ubuntu 24.04 amd64 VM, before presenting the local console.
set -eu
umask 077
base=/opt/portable-context-browser
src=$base/source
user=pcowner
test "$(dpkg --print-architecture)" = amd64
if id "$user" >/dev/null 2>&1 && ! test -f "$base/owns-user"; then
    echo "Existing pcowner account; use a fresh VM." >&2
    exit 1
fi
if ! id "$user" >/dev/null 2>&1; then
    touch "$base/owns-user"
    useradd --create-home --shell /bin/bash "$user"
fi
# No sudo rights. A locked password disables ordinary password login.
loginctl enable-linger "$user"
state=/home/$user/.local/share/pc-browser-state
runuser -u "$user" -- /bin/bash -eu -s -- "$state" <<'OWNER'
umask 077
state=$1
mkdir -p "$state" "$HOME/.ssh"
if ! test -e "$state/id_ed25519"; then
    ssh-keygen -q -t ed25519 -N '' -f "$state/id_ed25519"
fi
key="restrict,from=\"127.0.0.1\" $(cat "$state/id_ed25519.pub")"
touch "$HOME/.ssh/authorized_keys"
grep -Fqx -- "$key" "$HOME/.ssh/authorized_keys" || echo "$key" >> "$HOME/.ssh/authorized_keys"
OWNER
# A hypervisor may omit its DMI UUID. The read-only preflight also checks the
# cloud-init instance ID; it refuses to plan if neither matches the provider.
if test -r /sys/class/dmi/id/product_uuid; then
    install -m 0444 /sys/class/dmi/id/product_uuid /etc/pc-browser-vm-id
fi
systemctl enable --now cron
systemctl start ssh.service
python3 -m venv "$base/venv"
"$base/venv/bin/pip" install "$src[deployment,backup]"
# The unprivileged console user needs to be able to execute the interpreter.
chmod 0755 "$base"
chmod -R a+rX "$base/venv"
install -m 0644 "$src/deploy/browser-cloudru/console.service" /etc/systemd/system/pc-browser-console.service
systemctl daemon-reload
# Reserve the graphical console's first text terminal for the owner menu on every boot.
systemctl mask --now getty@tty1.service
systemctl enable --now pc-browser-console.service
