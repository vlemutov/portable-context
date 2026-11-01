"""Explicit export adapters. Unknown structures fail visibly; raw input is retained."""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import zipfile

from .core import Store, digest, canonical

FORMATS = ("claude", "chatgpt", "telegram", "gemini", "codex", "events")
MAX_MEMBER = 128 * 1024 * 1024
MAX_ZIP_TEXT = 512 * 1024 * 1024


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text(x) for x in value)
    if isinstance(value, dict):
        for field in ("text", "content", "parts"):
            if field in value:
                return text(value[field])
        # Unknown blocks (images, tool calls, etc.) stay visible, not silently dropped.
        return "\n" + canonical(value) + "\n"
    return "" if value is None else str(value)


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value) if value is not None else None


def event(fmt, instance, cid, mid, role, body, raw, title=None, date=None, parent=None, attachments=None):
    if cid is None or mid is None:
        raise ValueError(f"{fmt}: missing conversation/message identifier")
    return {"schema_version": 1, "source": fmt, "instance": instance,
            "conversation_id": str(cid), "message_id": str(mid), "role": role,
            "text": text(body), "raw": raw, "title": str(title or cid),
            "timestamp": timestamp(date), "parent_id": str(parent) if parent is not None else None,
            "attachments": attachments or []}


def claude(data, instance, warnings):
    chats = data if isinstance(data, list) else data.get("conversations")
    if not isinstance(chats, list):
        raise ValueError("Expected Claude conversations array")
    for chat in chats:
        if "chat_messages" not in chat:
            raise ValueError("Claude conversation lacks chat_messages")
        for m in chat["chat_messages"]:
            yield event("claude", instance, chat.get("uuid", chat.get("id")), m.get("uuid", m.get("id")),
                        {"human": "user"}.get(m.get("sender"), m.get("sender") or "unknown"),
                        m.get("content") or m.get("text", ""), m, chat.get("name"), m.get("created_at"),
                        m.get("parent_message_uuid"), (m.get("attachments") or []) + (m.get("files") or []))


def chatgpt(data, instance, warnings):
    chats = data if isinstance(data, list) else [data]
    for chat in chats:
        mapping = chat.get("mapping")
        if not isinstance(mapping, dict):
            raise ValueError("Expected ChatGPT mapping object")
        # Preserve ALL branches, not only current_node's path.
        for node_id, node in mapping.items():
            m = node.get("message")
            if m is None:
                continue
            content = m.get("content", {})
            yield event("chatgpt", instance, chat.get("id", chat.get("conversation_id")),
                        m.get("id", node_id), m.get("author", {}).get("role", "unknown"),
                        content.get("parts", content), {"node": node, "current_node": chat.get("current_node")},
                        chat.get("title"), m.get("create_time"), node.get("parent"),
                        m.get("metadata", {}).get("attachments", []))


def telegram(data, instance, warnings):
    if "messages" in data:
        chats = [data]
    else:
        chats = data.get("chats", {}).get("list")
    if not isinstance(chats, list):
        raise ValueError("Expected Telegram messages or chats.list")
    for chat in chats:
        for m in chat.get("messages", []):
            attachments = [{"field": f, "path": m[f]} for f in ("file", "photo", "thumbnail") if m.get(f)]
            # Telegram does not prove which participant is the user or an AI.
            role = "service" if m.get("type") == "service" else "participant"
            yield event("telegram", instance, chat.get("id"), m.get("id"), role,
                        m.get("text", m.get("action", "")), m, chat.get("name"), m.get("date"),
                        m.get("reply_to_message_id"), attachments)


def gemini(data, instance, warnings):
    if not isinstance(data, list):
        raise ValueError("Expected Google Takeout My Activity JSON array")
    warnings.append("Gemini activity entries are not guaranteed to contain full conversations or replies.")
    for item in data:
        if not isinstance(item, dict) or "title" not in item or "time" not in item:
            raise ValueError("Unsupported Gemini activity entry: expected title/time")
        # Takeout often supplies no stable IDs; identical activity entries collapse.
        mid = str(item.get("id") or digest(canonical(item)))
        body = item["title"]
        if item.get("subtitles"):
            body += "\n" + text(item["subtitles"])
        yield event("gemini", instance, item.get("conversation_id", mid), mid, "activity",
                    body, item, item["title"], item["time"])


class ActivityHTML(HTMLParser):
    """Extract Takeout outer-cell entries without executing or fetching HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.entries = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "outer-cell" in attrs.get("class", "").split():
                self.depth, self.parts = 1, []
        if self.depth and tag in ("br", "p"):
            self.parts.append("\n")
        if self.depth and tag == "a" and attrs.get("href"):
            self.parts.append(" [" + attrs["href"] + "] ")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1
            self.parts.append("\n")
            if not self.depth:
                self.entries.append("".join(self.parts).strip())


def codex(lines, instance, warnings):
    records = [json.loads(line) for line in lines.splitlines() if line.strip()]
    meta = next((r["payload"] for r in records if r.get("type") == "session_meta"), None)
    if not meta or not meta.get("id"):
        raise ValueError("Codex JSONL needs session_meta.id; paginated app-server storage is not JSONL")
    warnings.append("Codex JSONL adapter covers this file only; app-server pagination is a separate integration.")
    for i, r in enumerate(records):
        if r.get("type") != "response_item":
            continue  # event_msg often duplicates response_item; originals preserve both.
        p = r.get("payload", {})
        typ = p.get("type")
        role = p.get("role") or ("tool" if typ in ("function_call_output", "custom_tool_call_output") else "assistant")
        body = p.get("content", p.get("output", p))
        yield event("codex", instance, meta["id"], p.get("id") or f"line-{i + 1}", role,
                    body, r, meta.get("title", meta["id"]), r.get("timestamp"))


def parse(content: str, fmt: str, instance: str, filename: str, warnings: list):
    if fmt == "events":
        for line in content.splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("instance") != instance:
                    raise ValueError("Event instance differs from --instance; keep each instance separate")
                yield r
    elif fmt == "codex":
        yield from codex(content, instance, warnings)
    elif fmt == "gemini" and filename.lower().endswith((".html", ".htm")):
        parser = ActivityHTML()
        parser.feed(content)
        if not parser.entries:
            raise ValueError("No Takeout outer-cell entries found; export My Activity as JSON")
        warnings.append("Gemini HTML: dates/roles/conversation grouping are unparsed; inspect original entries.")
        for body in parser.entries:
            mid = digest(body)
            yield event("gemini", instance, mid, mid, "activity", body, {"html_entry_text": body})
    else:
        data = json.loads(content)
        yield from {"claude": claude, "chatgpt": chatgpt, "telegram": telegram, "gemini": gemini}[fmt](data, instance, warnings)


def candidate(name: str, fmt: str):
    name = name.replace("\\", "/").lower()
    base = name.rsplit("/", 1)[-1]
    if fmt in ("claude", "chatgpt"):
        return base.startswith("conversations") and base.endswith(".json")
    if fmt == "telegram":
        return base == "result.json"
    if fmt == "gemini":
        return ("gemini" in name or "bard" in name) and name.endswith((".json", ".html", ".htm"))
    return name.endswith(".jsonl")


def import_path(store: Store, path: Path, fmt: str, instance: str):
    if fmt not in FORMATS:
        raise ValueError("Unknown format")
    if not instance.strip():
        raise ValueError("An instance ID is required")
    report = {"format": fmt, "instance": instance, "files": [], "warnings": [], "errors": []}
    warnings = report["warnings"]
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink() and candidate(str(p), fmt))
        if store.root == path.resolve() or store.root.is_relative_to(path.resolve()):
            raise ValueError("Store must be outside the watched/imported directory")
        if not files:
            raise ValueError("No supported export files found; select an individual file explicitly")
        warnings.append("Directory import retains selected data files only; use original ZIP to retain all media/account files.")
    else:
        files = [path]
    for file in files:
        origin = store.archive(file)
        archived = store.root / "originals" / origin
        if zipfile.is_zipfile(archived):
            with zipfile.ZipFile(archived) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and candidate(m.filename, fmt)]
                if not members:
                    report["errors"].append({"file": file.name, "error": "No recognized data member; extract desired JSON and import explicitly"})
                    continue
                if sum(m.file_size for m in members) > MAX_ZIP_TEXT:
                    raise ValueError("ZIP text exceeds 512 MiB; import smaller exports")
                warnings.append("ZIP retained intact; attachments referenced but not extracted or analyzed.")
                for member in members:
                    if member.file_size > MAX_MEMBER:
                        raise ValueError("ZIP member exceeds 128 MiB")
                    _import_content(store, archive.read(member).decode("utf-8-sig"), fmt, instance,
                                    member.filename, origin, report)
        else:
            if archived.stat().st_size > MAX_MEMBER:
                raise ValueError("Input exceeds 128 MiB; split into separate conversation files")
            _import_content(store, archived.read_text(encoding="utf-8-sig"), fmt, instance, file.name, origin, report)
    report["warnings"] = sorted(set(warnings))
    return report


def _import_content(store, content, fmt, instance, filename, origin, report):
    try:
        result = store.ingest(parse(content, fmt, instance, filename, report["warnings"]), origin)
        report["files"].append({"file": filename, "archive": origin, **result})
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        report["errors"].append({"file": filename, "error": str(exc)})
