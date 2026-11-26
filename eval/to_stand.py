"""Перенос диалогов прогона на стенд: экран оператора показывает их как обычные диалоги.

    python -m eval.to_stand eval/runs/full4     # каталог прогона: в нем или в его подкаталогах лежат app.sqlite

Диалоги, шаги агента и обращения копируются как есть. Черновики КП формируются заново кодом стенда, чтобы
номер, файл PDF и запись в базе совпадали: в прогоне, разбитом на части, номера КП у частей пересекаются.
Повторный запуск уже перенесенные диалоги не трогает.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from app import store
from app.quote import kp

TABLES = ("messages", "steps", "handoffs")


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
    args, facts = json.loads(step["input_json"]), json.loads(session["facts_json"])
    res = kp.make_quote(sid, facts.get("компания") or args.get("customer", ""), args.get("items") or [],
                        inn=facts.get("ИНН", ""), kpp=facts.get("КПП", ""), payment=args.get("payment", ""),
                        contact=facts.get("контакт", ""))
    if not res.get("ok"):
        return 0
    for table, column in (("messages", "meta_json"), ("messages", "content"), ("steps", "output_json")):
        dst.execute(f"UPDATE {table} SET {column}=REPLACE({column}, ?, ?) WHERE session_id=?",
                    (old["number"], res["number"], sid))
    dst.commit()
    return 1


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
            dst.commit()
            counts["dialogs"] += 1
            counts["quotes"] += _requote(src, dst, session)
        src.close()
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(load(Path(sys.argv[1])), ensure_ascii=False))
