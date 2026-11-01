from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .core import Store
from .importers import FORMATS, import_path
from .live import enqueue, flush, make_server, watch_once


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Portable Context — local archives and living memory")
    parser.add_argument("--home", default=os.environ.get("PC_HOME", str(Path.home() / ".portable-context")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("stats")
    commands.add_parser("status")
    commands.add_parser("sources")
    commands.add_parser("jobs")
    commands.add_parser("lint")
    p = commands.add_parser("connect")
    p.add_argument("source_id")
    p.add_argument("--format", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--label")
    p.add_argument("--location")
    p = commands.add_parser("preview")
    p.add_argument("source_id")
    p.add_argument("--query", default="")
    p = commands.add_parser("sync")
    p.add_argument("source_id")
    p.add_argument("--conversation-key", action="append")
    p.add_argument("--include-forks", action="store_true")
    p.add_argument("--request-id")
    p = commands.add_parser("job")
    p.add_argument("job_id")
    p.add_argument("--run", action="store_true")
    p = commands.add_parser("enrich")
    p.add_argument("--conversation-key", action="append")
    p.add_argument("--request-id")
    p = commands.add_parser("graph")
    p.add_argument("--node")
    p.add_argument("--layer", choices=("structural", "semantic"))
    for name in ("import", "watch"):
        p = commands.add_parser(name)
        p.add_argument("path", type=Path)
        p.add_argument("--format", required=True, choices=FORMATS if name == "import" else ("events", "codex"))
        p.add_argument("--instance", required=True)
        if name == "watch":
            p.add_argument("--once", action="store_true")
            p.add_argument("--interval", type=float, default=10)
    p = commands.add_parser("search")
    p.add_argument("query")
    p.add_argument("--source")
    p.add_argument("--instance")
    p.add_argument("--limit", type=int, default=10)
    p = commands.add_parser("read")
    p.add_argument("message_key")
    p = commands.add_parser("export")
    p.add_argument("path", type=Path)
    p.add_argument("--revisions", action="store_true")
    commands.add_parser("vault")
    p = commands.add_parser("mcp")
    p.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--source-root", action="append", help="Explicit server directories readable by source registration")
    p.add_argument("--allow-unauthenticated-local", action="store_true", help="Explicit opt-out for trusted loopback-only use")
    p = commands.add_parser("serve-ingest")
    p.add_argument("--port", type=int, default=8765)
    p = commands.add_parser("enqueue")
    p.add_argument("path", type=Path, help="JSONL normalized events")
    p.add_argument("--outbox", type=Path, required=True)
    p = commands.add_parser("flush")
    p.add_argument("--outbox", type=Path, required=True)
    p.add_argument("--endpoint", required=True)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=10)
    args = parser.parse_args(argv)

    def output(value):
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)

    try:
        if args.command == "enqueue":
            records = [json.loads(line) for line in args.path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            output({"queued": enqueue(args.outbox, records)})
            return 0
        if args.command == "flush":
            while True:
                result = flush(args.outbox, args.endpoint, os.environ.get("PC_INGEST_TOKEN", ""))
                output(result)
                if not args.loop:
                    return 1 if result["failed"] else 0
                time.sleep(max(1, args.interval))
        store = Store(args.home)
        from .workspace import Workspace
        workspace = Workspace(store)
        if args.command in ("init", "stats"):
            output({"home": str(store.root), **store.stats()})
        elif args.command == "import":
            result = import_path(store, args.path, args.format, args.instance)
            output(result)
            return 1 if result["errors"] else 0
        elif args.command == "watch":
            while True:
                reports = watch_once(store, args.path, args.format, args.instance)
                output({"imports": reports})
                if args.once:
                    return 1 if any(r["errors"] for r in reports) else 0
                time.sleep(max(1, args.interval))
        elif args.command == "search":
            output(store.search(args.query, args.limit, args.source, args.instance))
        elif args.command == "read":
            output(store.read(args.message_key))
        elif args.command == "export":
            store.export(args.path, args.revisions)
            output({"exported": str(args.path)})
        elif args.command == "vault":
            output(workspace.refresh())
        elif args.command == "status":
            output(workspace.describe())
        elif args.command == "sources":
            output(workspace.list_sources())
        elif args.command == "connect":
            output(workspace.register_source(args.source_id, args.label or args.source_id,
                                            args.format, args.instance, args.location, trusted_local=True))
        elif args.command == "preview":
            output(workspace.preview(args.source_id, args.query))
        elif args.command == "sync":
            result = workspace.start_import(args.source_id, args.conversation_key, args.include_forks, args.request_id)
            output({"job_id": result['id'], "status": result['status']})
            while result['status'] != 'completed':
                result = workspace.run_import(result['id'])
            output(result)
        elif args.command == "jobs":
            output(workspace.list_jobs())
        elif args.command == "job":
            output(workspace.run_import(args.job_id) if args.run else workspace.job(args.job_id))
        elif args.command == "enrich":
            output(workspace.start_enrichment(args.conversation_key, request_id=args.request_id))
        elif args.command == "graph":
            from .graph import read_graph
            output(read_graph(store, args.node, args.layer))
        elif args.command == "lint":
            from .wiki import lint
            output(lint(workspace))
        elif args.command == "serve-ingest":
            server = make_server(store, os.environ.get("PC_INGEST_TOKEN", ""), args.port)
            print(f"Ingest listening on 127.0.0.1:{server.server_port}", file=sys.stderr)
            try:
                server.serve_forever()
            finally:
                server.server_close()
        elif args.command == "mcp":
            from .mcp_server import build_server
            server = build_server(store, args.source_root)
            if args.transport == "stdio":
                server.run()
            else:
                import uvicorn
                from .auth import BearerAuth
                app = server.streamable_http_app(host='127.0.0.1')
                if not args.allow_unauthenticated_local:
                    app = BearerAuth(app, os.environ.get('PC_MCP_TOKEN', ''))
                uvicorn.run(app, host='127.0.0.1', port=args.port)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
