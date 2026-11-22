"""Exercise client bootstrap and Terraform validation on every supported desktop OS; no cloud keys."""
from pathlib import Path
import shutil
import sys
import tempfile

import setup


def main():
    with tempfile.TemporaryDirectory(prefix='pc client test ') as tmp:
        state = setup.private_directory(Path(tmp) / 'state')
        setup.prepare(state, 'cloudru')
        setup.bootstrap(state)
        folder = state / 'terraform'
        folder.mkdir()
        for name in ('main.tf', 'deployment.tftest.hcl', 'boot-check.py'):
            shutil.copyfile(setup.TEMPLATE / name, folder / name)
        for args in (['init', '-backend=false', '-input=false'], ['validate', '-no-color'], ['test', '-no-color']):
            setup.terraform(state, args, credentials=False)
        if len(sys.argv) == 3 and sys.argv[1] == '--guest-image':
            from smoke_guest import check
            check(state, sys.argv[2])


if __name__ == '__main__':
    main()
