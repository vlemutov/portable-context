"""Check local SSH prerequisites from inside the Cloud.ru guest after each boot."""
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import time


def user_and_key():
    try:
        account = pwd.getpwnam("pcadmin")
    except KeyError:
        return "missing", "unchecked"
    try:
        expected = Path("/etc/portable-context/expected-key.pub").read_text().split()[:2]
        lines = (Path(account.pw_dir) / ".ssh/authorized_keys").read_text().splitlines()
    except OSError:
        return "ok", "unreadable"
    matches = len(expected) == 2 and any(line.split()[:2] == expected for line in lines)
    return "ok", "ok" if matches else "missing"


def probe_ssh():
    tcp = "unavailable"
    try:
        result = subprocess.run(["ip", "-j", "-4", "addr", "show", "scope", "global"],
                                check=True, capture_output=True, timeout=3)
        addresses = [addr["local"] for iface in json.loads(result.stdout)
                     for addr in iface["addr_info"] if addr["family"] == "inet"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return "address-unknown", "unchecked"
    for address in addresses:
        try:
            with socket.create_connection((address, 22), timeout=2) as conn:
                tcp = "ok"
                with conn.makefile("rb") as stream:
                    if stream.readline(256).startswith(b"SSH-2.0-"):
                        return tcp, "ok"
        except OSError:
            continue
    return tcp, "missing" if tcp == "ok" else "unchecked"


def main():
    deadline = time.monotonic() + 60
    while True:
        user, key = user_and_key()
        tcp, banner = probe_ssh()
        ok = all(value == "ok" for value in (user, key, tcp, banner))
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(2)
    line = f"PC-BOOT pcadmin={user} authorized_key={key} tcp22={tcp} ssh_banner={banner}"
    print(line, flush=True)
    for device in ("/dev/tty1", "/dev/ttyS0"):
        try:
            fd = os.open(device, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            with os.fdopen(fd, "w") as stream:
                print(line, file=stream)
        except OSError:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
