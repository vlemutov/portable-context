"""Integration example: call after your agent persists a message, not on token deltas.

Install portable-context in the producer environment. No network call happens in this hook.
Run `portable-context flush --outbox ... --endpoint ... --loop` separately.
"""
from pathlib import Path

from portable_context.live import enqueue


def on_persisted_message(outbox: Path, instance: str, chat_id: str, message_id: str,
                         role: str, text: str, timestamp: str, raw: dict | None = None):
    return enqueue(outbox, [{
        "source": "ouroboros", "instance": instance,
        "conversation_id": chat_id, "message_id": message_id,
        "role": role, "text": text, "timestamp": timestamp,
        "raw": raw or {},
    }])
