"""Durable producer outbox, authenticated loopback ingest, and file catch-up."""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

from .core import Store, atomic_write, canonical, validate
from .importers import import_path

MAX_BODY = 8 * 1024 * 1024
MAX_BATCH = 500


def check_batch(records):
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH:
        raise ValueError("Expected an array of 1..500 events")
    return [validate(r) for r in records]


def enqueue(directory: Path, records: list[dict]):
    """Call AFTER the agent has durably persisted a conversation message."""
    body = canonical(check_batch(records))
    if len(body.encode("utf-8")) > MAX_BODY:
        raise ValueError("Batch exceeds 8 MiB")
    path = directory / (f"{time.time_ns():020d}-" + str(uuid.uuid4()) + ".json")
    atomic_write(path, body)
    return str(path)


def enqueue_batch(directory: Path, source_id: str, records: list[dict]):
    """Collector adapter for the SAME resumable import jobs used by an interactive agent."""
    request_id = str(uuid.uuid4())
    body = canonical({'source_id': source_id, 'request_id': request_id, 'records': check_batch(records)})
    if len(body.encode('utf-8')) > MAX_BODY:
        raise ValueError('Batch exceeds 8 MiB')
    path = directory / (f'{time.time_ns():020d}-' + request_id + '.json')
    atomic_write(path, body)
    return str(path)


def flush(directory: Path, endpoint: str, token: str):
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
        raise ValueError("Remote endpoints require HTTPS; use an SSH tunnel for loopback HTTP")
    if not token:
        raise ValueError("PC_INGEST_TOKEN is required")
    report = {"sent": 0, "failed": []}
    for path in sorted(directory.glob("*.json")):
        try:
            if path.stat().st_size > MAX_BODY:
                raise ValueError("Queued batch exceeds 8 MiB")
            body = path.read_bytes()
            payload = json.loads(body)
            check_batch(payload['records'] if isinstance(payload, dict) else payload)
            route = '/v1/batches' if isinstance(payload, dict) else '/v1/events'
            request = Request(endpoint.rstrip("/") + route, data=body,
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            with build_opener(NoRedirect).open(request, timeout=30) as response:
                result = json.load(response)
            if result.get("ok") is not True:
                raise ValueError("Ingest server did not acknowledge commit")
            # A crash between commit and rename replays safely by source IDs.
            path.rename(path.with_suffix(".sent"))
            report["sent"] += 1
        except Exception as exc:
            report["failed"].append({"file": path.name, "error": type(exc).__name__})
            break  # Keep ordering; retry this batch before later batches.
    return report


def make_server(store: Store, token: str, port: int = 8765):
    if len(token) < 24:
        raise ValueError("PC_INGEST_TOKEN must have at least 24 characters")
    from .workspace import Workspace
    workspace = Workspace(store)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            pass  # Never log request bodies or auth headers.

        def reply(self, code, payload):
            body = canonical(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.reply(401, {"ok": False, "error": "Unauthorized"})
                return
            if self.path not in ("/v1/events", "/v1/batches"):
                self.reply(404, {"ok": False})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_BODY:
                    self.reply(413, {"ok": False, "error": "Body must be 1 byte..8 MiB"})
                    return
                payload = json.loads(self.rfile.read(size))
                if self.path == '/v1/batches':
                    if not isinstance(payload, dict):
                        raise ValueError('Expected source_id, request_id and records envelope')
                    job = workspace.stage_records(payload['source_id'], check_batch(payload['records']), payload['request_id'])
                    while job['status'] != 'completed':
                        job = workspace.run_import(job['id'])
                    report = {'job_id': job['id'], **job['import_counts']}
                else:
                    records = check_batch(payload)
                    report = store.ingest(records, "live-http")
                self.reply(200, {"ok": True, **report})
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {"ok": False, "error": str(exc)})
            except Exception:
                self.reply(500, {"ok": False, "error": "Commit failed; retry batch"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def watch_once(store: Store, path: Path, fmt: str, instance: str):
    """Re-scan changed snapshots; DB commit precedes checkpoint."""
    if fmt not in ("events", "codex"):
        raise ValueError("Live watching currently supports events and Codex JSONL")
    path = path.resolve()
    if store.root == path or store.root.is_relative_to(path):
        raise ValueError("Store must be outside the watched directory")
    state_path = store.root / "collectors" / (hashlib.sha256(canonical([str(path), fmt, instance]).encode()).hexdigest() + ".json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    files = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
    reports = []
    for file in files:
        if file.is_symlink():
            continue
        # Hash, not mtime: edits with preserved timestamps must still be noticed.
        h = hashlib.sha256()
        with file.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                h.update(block)
        fingerprint = h.hexdigest()
        if state.get(str(file)) == fingerprint:
            continue
        result = import_path(store, file, fmt, instance)
        reports.append(result)
        if not result["errors"]:
            state[str(file)] = fingerprint
            atomic_write(state_path, canonical(state))
    return reports
