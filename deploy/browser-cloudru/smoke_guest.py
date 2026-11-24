"""Boot the browser-generated seed in QEMU; report success on its private serial port."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

import pexpect
import yaml


def run(image):
    here = Path(__file__).resolve().parent
    source = (here/"prepare.html").read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]
    js = ('const document={getElementById:()=>({})};\n' + source +
          '\nencryptSettings({secret:"fixture"},"offline-fixture").then(x=>console.log(cloudConfig(x)));')
    seed = yaml.safe_load(subprocess.check_output(["node", "-e", js], text=True))
    seed["packages"] = ["x11-utils"]
    # The probe runs outside the app. Never grant the installer cloud credentials.
    seed["write_files"].append({"path":"/usr/local/sbin/pc-browser-ci", "permissions":"0700", "content":'''#!/bin/bash
set -eu
base=/opt/portable-context-browser
for i in $(seq 1 300); do
    if test -f "$base/boot-complete" && systemctl is-active --quiet pc-browser-console &&
       runuser -u pcowner -- env DISPLAY=:0 XAUTHORITY=/run/pc-browser-console/Xauthority \\
       xwininfo -root -tree 2>/dev/null | grep -q 'Portable Context'; then
        test ! -f /home/pcowner/.local/share/pc-browser-state/credentials.json
        runuser -u pcowner -- ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new \\
            -i /home/pcowner/.local/share/pc-browser-state/id_ed25519 pcowner@127.0.0.1 true
        echo PC_BROWSER_DISPLAY_READY >/dev/ttyS0
        exit 0
    fi
    sleep 5
done
echo PC_BROWSER_BOOT_FAILED >/dev/ttyS0
'''})
    seed["runcmd"].append(["systemd-run", "--no-block", "/usr/local/sbin/pc-browser-ci"])
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder/"user-data").write_text("#cloud-config\n" + yaml.safe_dump(seed), encoding="utf-8")
        subprocess.run(["cloud-init","schema","--config-file",str(folder/"user-data")],check=True)
        (folder/"meta-data").write_text("instance-id: pc-browser-test\nlocal-hostname: pc-browser-test\n")
        subprocess.run(["cloud-localds","seed.img","user-data","meta-data"],cwd=tmp,check=True)
        subprocess.run(["qemu-img","create","-q","-f","qcow2","-F","qcow2","-b",str(image.resolve()),
                        "disk.img","12G"],cwd=tmp,check=True)
        accel = ["-accel","kvm","-cpu","host"] if os.access("/dev/kvm",os.W_OK) else ["-accel","tcg","-cpu","max"]
        guest = pexpect.spawn("qemu-system-x86_64", [*accel,"-m","2048","-smp","2","-vga","std",
            "-display","none","-monitor","none","-serial","stdio","-drive","file=disk.img,if=virtio,format=qcow2",
            "-drive","file=seed.img,if=virtio,format=raw,readonly=on","-nic","user,model=virtio-net-pci"],
            cwd=tmp, encoding="utf-8", codec_errors="replace", timeout=1800)
        try:
            # Only fixed markers leave the guest.
            if guest.expect(["PC_BROWSER_DISPLAY_READY","PC_BROWSER_BOOT_FAILED"]) != 0:
                raise RuntimeError("Browser console did not start")
            print("Verified Ubuntu first boot, actual X11 wizard window and private loopback install transport.")
        finally:
            guest.close(force=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    run(parser.parse_args().image)
