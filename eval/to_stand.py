"""Перенос диалогов прогона на стенд: экран оператора показывает их как обычные диалоги.

    python -m eval.to_stand eval/runs/full5            # каталог прогона: в нем или в подкаталогах лежат app.sqlite
    python -m eval.to_stand eval/runs/full5 --replace  # сначала убрать со стенда диалоги прошлых прогонов

Диалоги, шаги агента и обращения копируются как есть. Черновики КП формируются заново кодом стенда, чтобы
номер, файл PDF и запись в базе совпадали: в прогоне, разбитом на части, номера КП у частей пересекаются.
Контакт клиента в памяти диалога и в обращении берется из его сообщений тем же кодом, что на стенде.
Повторный запуск уже перенесенные диалоги не трогает. Диалоги живых посетителей стенда не затрагиваются.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from app import config, store
from app.agent import loop, tools
from app.quote import kp

TABLES = ("messages", "steps", "handoffs")
RUN_CLIENTS = "client_id LIKE 'eval-%' OR client_id = 'demo'"   # диалоги, которые создает прогон, а не посетитель


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _copy(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, where: str, args: tuple):
    cols = [c for c in _columns(src, table) if c in _columns(dst, table) and (c != "id" or table == "sessions")]
    rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY rowid", args).fetchall()
    dst.executemany(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", rows)


def _requote(src: sqlite3.Connection, dst: sqlite3.Connection, session: sqlite3.Row) -> int:
    """КП диалога заново: позиции из последнего вызова make_quote, реквизиты из памяти диалога."""
    sid = session["id"]
    old = src.execute("SELECT number FROM quotes WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    step = src.execute("SELECT input_json FROM steps WHERE session_id=? AND kind='tool' AND name='make_quote' "
                       "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if not old or not step:
        return 0
    args = json.loads(step["input_json"])
    facts = json.loads(dst.execute("SELECT facts_json FROM sessions WHERE id=?", (sid,)).fetchone()[0] or "{}")
    res = kp.make_quote(sid, facts.get("компания") or args.get("customer", ""), args.get("items") or [],
                        inn=facts.get("ИНН", ""), kpp=facts.get("КПП", ""), payment=args.get("payment", ""),
                        contact=facts.get("контакт", ""))
    if not res.get("ok"):
        return 0
    for table, column in (("messages", "meta_json"), ("messages", "content"), ("steps", "output_json"),
                          ("handoffs", "summary")):
        dst.execute(f"UPDATE {table} SET {column}=REPLACE({column}, ?, ?) WHERE session_id=?",
                    (old["number"], res["number"], sid))
    dst.commit()
    return 1


def _contact(dst: sqlite3.Connection, sid: str):
    """Контакт клиента из его сообщений: в карточке обращения менеджер видит адрес, а не слова модели о нем."""
    said = [m["content"] for m in dst.execute("SELECT content FROM messages WHERE session_id=? AND role='user' "
                                              "ORDER BY id", (sid,))]
    contact = tools.merge_contact("", ", ".join(c for text in said for c in loop.mask_contacts(text)[1]))
    facts = json.loads(dst.execute("SELECT facts_json FROM sessions WHERE id=?", (sid,)).fetchone()[0] or "{}")
    facts.pop("контакт", None)
    if contact:
        facts["контакт"] = contact
    dst.execute("UPDATE sessions SET facts_json=? WHERE id=?", (json.dumps(facts, ensure_ascii=False), sid))
    dst.execute("UPDATE handoffs SET contact=? WHERE session_id=?", (contact, sid))


def clear() -> int:
    """Убирает со стенда диалоги прошлых прогонов вместе с их черновиками КП."""
    dst = store.db()
    sessions = [r[0] for r in dst.execute(f"SELECT id FROM sessions WHERE {RUN_CLIENTS}")]
    for sid in sessions:
        for (number,) in dst.execute("SELECT number FROM quotes WHERE session_id=?", (sid,)).fetchall():
            (config.QUOTES_DIR / f"{number}.pdf").unlink(missing_ok=True)
        store.drop_session(sid)
    return len(sessions)


def load(run_dir: Path) -> dict:
    dst, counts = store.db(), {"dialogs": 0, "quotes": 0, "skipped": 0}
    for path in sorted(Path(run_dir).rglob("app.sqlite")):
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        for session in src.execute("SELECT * FROM sessions ORDER BY created_at").fetchall():
            if dst.execute("SELECT 1 FROM sessions WHERE id=?", (session["id"],)).fetchone():
                counts["skipped"] += 1
                continue
            _copy(src, dst, "sessions", "id=?", (session["id"],))
            for table in TABLES:
                _copy(src, dst, table, "session_id=?", (session["id"],))
            _contact(dst, session["id"])
            dst.commit()
            counts["dialogs"] += 1
            counts["quotes"] += _requote(src, dst, session)
        src.close()
    return counts


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--replace"]
    if len(args) != 1:
        sys.exit(__doc__)
    removed = clear() if "--replace" in sys.argv else 0
    print(json.dumps({"removed": removed, **load(Path(args[0]))}, ensure_ascii=False))
