"""Install/update the local client in a private venv on Windows, macOS or Linux."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True,help='Private client directory outside the public checkout')
    p.add_argument('--config',type=Path,help='Existing MCP JSON configuration; connect the same memory')
    args=p.parse_args()
    repo=Path(__file__).resolve().parents[1]
    directory=args.directory.expanduser().resolve()
    if directory==repo or directory.is_relative_to(repo):
        p.error('Client state must be outside the public checkout')
    directory.mkdir(parents=True,exist_ok=True)
    venv=directory/'venv'
    subprocess.run([sys.executable,'-m','venv',str(venv)],check=True)
    python=venv/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    subprocess.run([str(python),'-m','pip','install','--upgrade',str(repo)+'[mcp]','mcp==2.3.0'],check=True)
    if args.config:
        subprocess.run([str(python),'-m','portable_context.client','connect','--config',str(args.config),
                        '--profile',str(directory/'profile.json')],check=True)
    print(json.dumps({'python':str(python),'profile':str(directory/'profile.json'),'ready':True}))


if __name__=='__main__':
    main()
