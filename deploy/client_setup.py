"""Install/update the local client in a private venv on Windows, macOS or Linux."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def update_guide(path,version,python,profile,connection_verified=False):
    """Replace only our managed block; never discard the user's installation notes."""
    begin='<!-- portable-context-current:start -->';end='<!-- portable-context-current:end -->'
    repo=Path(__file__).resolve().parents[1]
    connection=('SSH/stdio connection checked during this setup; profile is pinned to its workspace. '
                'Recheck from the actual agent before using it.' if connection_verified else
                'Local client prepared only; this run did not check a memory connection. '
                'The profile path below is a destination, not proof that a profile exists or works.')
    body=(f'{begin}\n## Portable Context {version}: current client\n\n'
          f'{connection}\n\nPython: `{python}`. Profile: `{profile}`.\n\n'
          f'Read [START-HERE.md](<{(repo / "START-HERE.md").as_posix()}>) and the complete '
          f'skill at `{repo / "skills/portable-context"}`; keep the same checkout/ref when resuming.\n\n'
          'Reconnect MCP after server upgrade; read the installed skill and memory_status to verify the server version and workspace ID. '
          'The client version alone does not prove server migration. Do not create a replacement memory.\n\n'
          'For native HTTPS/OAuth use references/connection.md in the skill and browser consent; '
          'this CLI sends captures over SSH/stdio only. client-https.json is not its connection profile.\n\n'
          'Choose a real first import, verify saved messages and file receipts, then read them back. '
          'Wiki enrichment is separate: references/review.md explains explicit evidence reviews. '
          'Report import counts, originals, S3 proof dates, backup_status and actual review coverage.\n\n'
          'Server updates use deploy/setup.py --state PRIVATE_STATE update from the chosen checkout; '
          'this client installer does not update the server. Reuse the private deployment state. '
          'Keep endpoint, workspace ID, state/recovery-kit paths and unfinished job IDs in your notes; '
          'never passwords, tokens, login codes or recovery-kit contents.\n'+end+'\n')
    text=path.read_text(encoding='utf-8') if path.exists() else '# Portable Context: this installation\n\n'
    if begin in text and end in text:
        a=text.index(begin);b=text.index(end,a)+len(end);text=text[:a]+body.rstrip()+text[b:]
    else:text+='\n'+body
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True,help='Private client directory outside the public checkout')
    p.add_argument('--config',type=Path,help='Existing SSH/stdio MCP JSON configuration; verify the same memory (not client-https.json)')
    p.add_argument('--guide',type=Path,help='Existing private entry guide to refresh, preserving personal notes')
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
    version=subprocess.check_output([str(python),'-c','from portable_context import __version__; print(__version__)'],text=True).strip()
    guide=args.guide or directory/'START-HERE.md'
    update_guide(guide,version,python,directory/'profile.json',connection_verified=bool(args.config))
    print(json.dumps({'python':str(python),'profile':str(directory/'profile.json'),'guide':str(guide),
                      'ready':True,'connection_verified':bool(args.config)}))


if __name__=='__main__':
    main()
