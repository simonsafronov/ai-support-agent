"""Диалог стенда -> кейс проверочного набора.

Оператор видит диалог, где агент ошибся, и одной кнопкой отправляет его в набор, отметив, как должно было быть.
Кейсы копятся в eval/cases_added.jsonl и проигрываются вместе с основным набором при каждом прогоне.
"""
from __future__ import annotations

import json

from app import config, store
from app.agent import loop

ADDED = config.ROOT / "eval" / "cases_added.jsonl"


def from_session(session_id: str, handoff: bool | str, quote: bool, note: str = "") -> dict | None:
    """Кейс из диалога: реплики клиента без контактов и ожидание, которое задал оператор."""
    turns = [loop.mask_contacts(m["content"])[0] for m in store.messages(session_id) if m["role"] == "user"]
    if not turns:
        return None
    tools = list(dict.fromkeys(s["name"] for s in store.steps(session_id)
                               if s["kind"] == "tool" and s["name"] != "note_facts"))
    return {"id": "s-" + session_id[:8], "category": "Со стенда", "scenario": "from_stand", "turns": turns,
            "note": note.strip()[:500],
            "expect": {"tools_any": tools, "ask_about": [], "mention_any": [], "handoff": handoff, "quote": bool(quote),
                       "must_not": ["invent_price"]}}


def add(case: dict) -> int:
    """Дописывает кейс в набор; повторная отправка того же диалога заменяет прежний кейс. Возвращает размер набора."""
    cases = [c for c in load() if c["id"] != case["id"]] + [case]
    ADDED.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    return len(cases)


def load() -> list[dict]:
    if not ADDED.exists():
        return []
    return [json.loads(line) for line in ADDED.read_text(encoding="utf-8").splitlines() if line.strip()]
