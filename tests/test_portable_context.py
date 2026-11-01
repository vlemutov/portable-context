import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from portable_context.core import Store, key
from portable_context.importers import import_path, parse
from portable_context.live import enqueue, flush, make_server, watch_once


def record(**kwargs):
    return dict(source="test", instance="server-a", conversation_id="chat", message_id="1",
                role="user", text="Remember my project: переносимая память", **kwargs)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "memory")

    def test_replay_revisions_and_instance_isolation(self):
        first = record()
        self.assertEqual(self.store.ingest([first])["inserted"], 1)
        edited = dict(first, text="Changed preference")
        self.assertEqual(self.store.ingest([edited])["revised"], 1)
        self.assertEqual(self.store.ingest([first])["unchanged"], 1)
        mid = key("test", "server-a", "chat", "1")
        self.assertEqual(self.store.read(mid)["text"], edited["text"])
        self.store.ingest([dict(first, instance="server-b")])
        self.assertEqual(self.store.stats()["messages"], 2)
        self.assertEqual(len(self.store.read(mid)["history"]), 2)

    def test_transaction_rollback(self):
        with self.assertRaises(ValueError):
            self.store.ingest([record(), {"text": "broken"}])
        self.assertEqual(self.store.stats()["messages"], 0)
        self.assertEqual(self.store.search("project"), [])
        with self.assertRaises(ValueError):
            self.store.ingest([record(schema_version=2)])

    def test_parallel_writers(self):
        def write(i):
            return self.store.ingest([dict(record(), message_id=str(i))])
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(write, range(30)))
        self.assertEqual(self.store.stats()["messages"], 30)

    def test_search_unicode_scope_and_fts_punctuation(self):
        self.store.ingest([record(), dict(record(), instance="server-b")])
        self.assertEqual(len(self.store.search("переносимая память", instance="server-a")), 1)
        self.assertEqual(self.store.search('" * : ---'), [])
        self.assertEqual(len(self.store.search("project", source="test")), 2)

    def test_notes_and_manual_vault_edits(self):
        self.store.ingest([record()])
        mid = key("test", "server-a", "chat", "1")
        note = self.store.write_note("project", "# Project\nPortable memory", [mid], "user_statement")
        self.assertIn(mid, note["body"])
        path = self.store.note_path("project")
        path.write_text(note["body"] + "\nManual addition", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.write_note("project", "overwrite", [mid], "inference", note["hash"])
        self.assertEqual(len(self.store.search_notes("Manual")), 1)
        with self.assertRaises(ValueError):
            self.store.write_note("../outside", "bad", [], "user_note")
        with self.assertRaises(ValueError):
            self.store.write_note("unsupported", "no proof", [], "inference")
        self.store.project()
        transcript = next((self.store.root / "vault" / "conversations").glob("*.md"))
        transcript.write_text("My edit", encoding="utf-8")
        result = self.store.project()
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(transcript.read_text(), "My edit")
        self.assertTrue(transcript.with_suffix(".incoming.md").exists())

    def test_export_roundtrip(self):
        self.store.ingest([record()])
        export = self.base / "events.jsonl"
        self.store.export(export)
        other = Store(self.base / "other")
        result = import_path(other, export, "events", "server-a")
        self.assertFalse(result["errors"])
        self.assertEqual(other.search("project")[0]["id"], self.store.search("project")[0]["id"])

    def test_zip_original_intact_and_no_extraction(self):
        source = self.base / "archive.zip"
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("conversations.json", json.dumps([{"uuid": "chat", "chat_messages": [
                {"uuid": "m", "sender": "human", "text": "Hello"}]}]))
            z.writestr("../../evil.txt", "never extract")
            z.writestr("media/photo.bin", b"media")
        report = import_path(self.store, source, "claude", "account")
        archived = self.store.root / "originals" / report["files"][0]["archive"]
        self.assertEqual(source.read_bytes(), archived.read_bytes())
        self.assertFalse((self.base / "evil.txt").exists())
        self.assertEqual(self.store.stats()["messages"], 1)

    def test_watch_crash_catchup_and_partial_line(self):
        path = self.base / "events.jsonl"
        line = json.dumps(record())
        path.write_text(line + "\n", encoding="utf-8")
        self.assertEqual(len(watch_once(self.store, path, "events", "server-a")), 1)
        self.assertEqual(watch_once(self.store, path, "events", "server-a"), [])
        next_line = json.dumps(dict(record(), message_id="2"))
        path.write_text(line + "\n" + next_line[:20], encoding="utf-8")
        self.assertTrue(watch_once(self.store, path, "events", "server-a")[0]["errors"])
        self.assertEqual(self.store.stats()["messages"], 1)
        path.write_text(line + "\n" + next_line, encoding="utf-8")
        self.assertFalse(watch_once(self.store, path, "events", "server-a")[0]["errors"])
        self.assertEqual(self.store.stats()["messages"], 2)

    def test_http_queue_auth_replay_and_retry(self):
        token = "synthetic-test-token-123456789"
        server = make_server(self.store, token, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        outbox = self.base / "outbox"
        queued = Path(enqueue(outbox, [record()]))
        result = flush(outbox, endpoint, "wrong")
        self.assertTrue(result["failed"])
        self.assertTrue(queued.exists())
        self.assertEqual(self.store.stats()["messages"], 0)
        self.assertEqual(flush(outbox, endpoint, token)["sent"], 1)
        queued.with_suffix(".sent").rename(queued)
        self.assertEqual(flush(outbox, endpoint, token)["sent"], 1)
        self.assertEqual(self.store.stats()["messages"], 1)


class AdapterTests(unittest.TestCase):
    def run_adapter(self, fmt, data):
        warnings = []
        return list(parse(json.dumps(data), fmt, "instance", "data.json", warnings))

    def test_claude_content_and_attachments(self):
        result = self.run_adapter("claude", [{"uuid": "c", "name": "Chat", "chat_messages": [
            {"uuid": "m", "sender": "human", "content": [{"type": "text", "text": "Привет"}],
             "attachments": [{"file_name": "notes.pdf"}]}]}])
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["text"], "Привет")
        self.assertEqual(result[0]["attachments"][0]["file_name"], "notes.pdf")

    def test_chatgpt_all_branches_and_raw_fields(self):
        data = [{"id": "c", "current_node": "b", "mapping": {
            "root": {"message": None},
            "a": {"parent": "root", "message": {"id": "a", "author": {"role": "user"}, "content": {"parts": ["a"]}}},
            "b": {"parent": "a", "message": {"id": "b", "author": {"role": "assistant"}, "content": {"parts": ["b"]}}},
            "c": {"parent": "a", "message": {"id": "c", "author": {"role": "assistant"}, "content": {"parts": ["c"]}}}}}]
        result = self.run_adapter("chatgpt", data)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[-1]["parent_id"], "a")
        self.assertEqual(result[-1]["raw"]["current_node"], "b")

    def test_telegram_single_and_full_export(self):
        chat = {"id": 12, "name": "Agent", "messages": [{"id": 1, "type": "message", "from_id": "user1",
                "text": ["Hello ", {"type": "bold", "text": "world"}], "photo": "photos/1.jpg", "reply_to_message_id": 0}]}
        for data in (chat, {"chats": {"list": [chat]}}):
            result = self.run_adapter("telegram", data)
            self.assertEqual(result[0]["text"], "Hello world")
            self.assertEqual(result[0]["role"], "participant")
            self.assertEqual(result[0]["parent_id"], "0")
            self.assertEqual(result[0]["raw"]["from_id"], "user1")

    def test_gemini_json_and_html_audit(self):
        data = [{"title": "Asked for help", "time": "2026-01-01T00:00:00Z", "extra": "retained"}]
        result = self.run_adapter("gemini", data)
        self.assertEqual(result[0]["role"], "activity")
        self.assertEqual(result[0]["raw"]["extra"], "retained")
        warnings = []
        html = '<div class="outer-cell mdl-cell"><div>Question<br>Answer</div></div>'
        result = list(parse(html, "gemini", "web", "MyActivity.html", warnings))
        self.assertIn("Answer", result[0]["text"])
        self.assertTrue(warnings)

    def test_codex_avoids_duplicate_event_stream(self):
        rows = [{"type": "session_meta", "payload": {"id": "s"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "tool1", "output": "Done"}}]
        result = list(parse("\n".join(json.dumps(x) for x in rows), "codex", "laptop", "session.jsonl", []))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "tool")

    def test_unknown_schema_fails(self):
        for fmt in ("claude", "chatgpt", "telegram", "gemini"):
            with self.subTest(fmt=fmt), self.assertRaises((ValueError, TypeError)):
                self.run_adapter(fmt, {"unsupported": True})


if __name__ == "__main__":
    unittest.main()
