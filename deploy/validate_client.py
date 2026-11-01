"""Exercise client bootstrap and Terraform validation on every supported desktop OS; no cloud keys."""
from pathlib import Path
import shutil
import tempfile

import setup


def main():
    with tempfile.TemporaryDirectory(prefix='pc client test ') as tmp:
        state = setup.private_directory(Path(tmp) / 'state')
        setup.prepare(state, 'cloudru')
        setup.bootstrap(state)
        folder = state / 'terraform'
        folder.mkdir()
        for name in ('main.tf', 'deployment.tftest.hcl'):
            shutil.copyfile(setup.TEMPLATE / name, folder / name)
        for args in (['init', '-backend=false', '-input=false'], ['validate', '-no-color'], ['test', '-no-color']):
            setup.terraform(state, args, credentials=False)


if __name__ == '__main__':
    main()
