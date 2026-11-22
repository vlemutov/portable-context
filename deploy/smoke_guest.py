"""Boot a disposable Ubuntu guest with the installer's actual Terraform user-data (Linux CI)."""
import base64
import json
import os
from pathlib import Path
import subprocess
import time

import pexpect
import setup


def check(state, image):
    fixture_id = '11111111-1111-4111-8111-111111111111'
    setup.configure(state, dict(project_id=fixture_id, tenant_id=fixture_id, image_id=fixture_id,
                                zone='fixture', flavor='fixture', ssh_cidr='192.0.2.1/32'))
    setup.render(state, setup.read(state / 'deployment.json'))
    folder = state / 'terraform'
    result = subprocess.run([state / 'tools/terraform', 'console'], cwd=folder,
                            env=setup.command_env(state, credentials=False), check=True, capture_output=True,
                            input='nonsensitive(local.memory_userdata)\n', text=True)
    (state / 'user-data').write_bytes(base64.b64decode(json.loads(result.stdout), validate=True))
    (state / 'meta-data').write_text('instance-id: pc-ci\nlocal-hostname: pc-ci\n')
    subprocess.run(['cloud-localds', 'seed.img', 'user-data', 'meta-data'], cwd=state, check=True)
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2', '-b',
                    str(Path(image).resolve()), 'disk.img', '8G'], cwd=state, check=True)
    accel = ['-accel', 'kvm', '-cpu', 'host'] if os.access('/dev/kvm', os.W_OK) else ['-accel', 'tcg', '-cpu', 'max']
    guest = pexpect.spawn('qemu-system-x86_64', [*accel, '-m', '1536', '-smp', '2', '-display', 'none',
                          '-monitor', 'none', '-serial', 'stdio', '-drive', 'file=disk.img,if=virtio,format=qcow2',
                          '-drive', 'file=seed.img,if=virtio,format=raw,readonly=on',
                          '-nic', 'user,model=virtio-net-pci,restrict=on,hostfwd=tcp:127.0.0.1:22222-:22'],
                          cwd=str(state), echo=False, encoding='utf-8', codec_errors='replace', timeout=300)
    stage = 'Ubuntu console login'
    try:
        guest.expect('login:')
        guest.sendline('pcadmin')
        guest.expect('Password:')
        guest.sendline(setup.read(state / 'console-login.json')['password'])
        guest.expect(r'pcadmin@pc-ci:.*\$')
        print('Verified pcadmin console password.', flush=True)
        stage = 'SSH key login and guest boot report'
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=3',
               '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile="{state / "known_hosts"}"',
               '-i', str(state / 'id_ed25519'), '-p', '22222', 'pcadmin@127.0.0.1']
        marker = 'PC-BOOT pcadmin=ok authorized_key=ok tcp22=ok ssh_banner=ok'
        for boot in (1, 2):
            stage = f'boot {boot}: SSH login, password policy and guest diagnostics'
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                result = subprocess.run([*ssh, 'sudo sshd -T && sudo journalctl -b -u pc-boot-check.service -o cat'],
                                        capture_output=True, text=True, timeout=15)
                if not result.returncode and 'passwordauthentication no' in result.stdout.splitlines() and marker in result.stdout:
                    break
                time.sleep(3)
            else:
                raise SystemExit('Guest smoke failed at ' + stage)
            print(f'Boot {boot}: SSH key login, password policy and all four diagnostics verified.', flush=True)
            if boot == 1:
                guest.sendline('sudo systemctl reboot')
                guest.expect('pc-ci login:')
    except (pexpect.ExceptionPexpect, subprocess.TimeoutExpired):
        # A serial buffer can contain login input. Report only the failed test stage.
        raise SystemExit('Guest smoke failed at ' + stage) from None
    finally:
        guest.close(force=True)
