"""Install/update the local client in a private venv on Windows, macOS or Linux."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def update_guide(path,version,python,profile):
    """Replace only our managed block; never discard the user's installation notes."""
    begin='<!-- portable-context-current:start -->';end='<!-- portable-context-current:end -->'
    body=(f'{begin}\n## Portable Context {version}: current client\n\n'
          f'Python: `{python}`. Profile: `{profile}`.\n\n'
          'Reconnect MCP after server upgrade; read the installed skill and memory_status to verify the server version and workspace ID. '
          'The client version alone does not prove server migration. Do not create a replacement memory.\n\n'
          'For 0.9: read review_coverage, select conversations, start_enrichment, claim, read_evidence, '
          'commit explicit reviews and cited pages, then finish selectively or continue. Report unread evidence separately. '
          'Mixed messages require the user text_range for human quotations. Read references/review.md in the skill.\n\n'
          'After import report counts, S3 proof dates, backup_status, updated pages and review coverage. '
          'Legacy completed chunks do not establish full review.\n'+end+'\n')
    text=path.read_text(encoding='utf-8') if path.exists() else '# Portable Context: this installation\n\n'
    if begin in text and end in text:
        a=text.index(begin);b=text.index(end,a)+len(end);text=text[:a]+body.rstrip()+text[b:]
    else:text+='\n'+body
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True,help='Private client directory outside the public checkout')
    p.add_argument('--config',type=Path,help='Existing MCP JSON configuration; connect the same memory')
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
    update_guide(guide,version,python,directory/'profile.json')
    print(json.dumps({'python':str(python),'profile':str(directory/'profile.json'),'guide':str(guide),'ready':True}))


if __name__=='__main__':
    main()
