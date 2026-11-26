"""Хранилище приложения: диалоги, шаги агента, память о клиенте, передачи менеджеру, КП."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid

from app import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, client_id TEXT, created_at REAL, updated_at REAL, page_url TEXT,
  facts_json TEXT DEFAULT '{}', items_json TEXT DEFAULT '{}', status TEXT DEFAULT 'bot',
  prompt_tokens INT DEFAULT 0, completion_tokens INT DEFAULT 0, llm_calls INT DEFAULT 0, tool_calls INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL, role TEXT, content TEXT, meta_json TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INT, ts REAL, kind TEXT, name TEXT,
  input_json TEXT, output_json TEXT, prompt_tokens INT DEFAULT 0, completion_tokens INT DEFAULT 0, seconds REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS handoffs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL, reason TEXT, summary TEXT, contact TEXT,
  status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS quotes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL, number TEXT, customer TEXT, items_json TEXT,
  total REAL, file TEXT);
CREATE TABLE IF NOT EXISTS answers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, handoff_id INT, session_id TEXT, question TEXT, answer TEXT,
  teach INT DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS answers_fts USING fts5(text);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS ix_steps_session ON steps(session_id, id);
CREATE INDEX IF NOT EXISTS ix_sessions_client ON sessions(client_id, updated_at);
"""

RUN_CLIENTS = "client_id LIKE 'eval-%' OR client_id = 'demo'"  # диалоги, которые создал прогон, а не посетитель

log = logging.getLogger("store")
_local = threading.local()


def db() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        config.APP_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(config.APP_DB, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        _migrate(con)
        _local.con = con
    return con


_COLUMNS = (  # колонки, которых не было в первых версиях базы: добавляются на месте, данные остаются
    ("handoffs", "assignee", "TEXT DEFAULT ''"), ("handoffs", "taken_at", "REAL"),
)


def _migrate(con: sqlite3.Connection):
    for table, column, kind in _COLUMNS:
        if column not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    con.commit()
    try:  # у диалога не больше одной открытой заявки: правило держит сама база
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_handoffs_open ON handoffs(session_id) "
                    "WHERE status IN ('new', 'taken')")
        con.commit()
    except sqlite3.IntegrityError:  # в базе уже есть дубли из старых версий: работаем без индекса, заявки не теряем
        con.rollback()
        log.warning("индекс ux_handoffs_open не создан: у части диалогов несколько открытых заявок")


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def find_session(session_id: str | None) -> dict | None:
    """Диалог по номеру или None: чтение ничего не создает, так сервер проверяет, чей это диалог."""
    row = db().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone() if session_id else None
    if row is None:
        return None
    s = dict(row)
    s["facts"], s["items"] = json.loads(s.pop("facts_json")), json.loads(s.pop("items_json"))
    return s


def get_session(session_id: str | None, client_id: str = "", page_url: str = "") -> dict:
    """Возвращает диалог, создает новый, если такого нет."""
    found = find_session(session_id)
    if found is not None:
        return found
    con, now = db(), time.time()
    session_id = session_id or uuid.uuid4().hex[:16]
    con.execute("INSERT INTO sessions(id, client_id, created_at, updated_at, page_url) VALUES(?,?,?,?,?)",
                (session_id, client_id, now, now, page_url))
    con.commit()
    return find_session(session_id)


def update_session(session_id: str, **fields):
    for k in ("facts", "items"):
        if k in fields:
            fields[k + "_json"] = _j(fields.pop(k))
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), session_id))
    db().commit()


def add_usage(session_id: str, prompt_tokens: int, completion_tokens: int, llm_calls: int, tool_calls: int):
    db().execute("UPDATE sessions SET prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+?, "
                 "llm_calls=llm_calls+?, tool_calls=tool_calls+?, updated_at=? WHERE id=?",
                 (prompt_tokens, completion_tokens, llm_calls, tool_calls, time.time(), session_id))
    db().commit()


def add_message(session_id: str, role: str, content: str, meta: dict | None = None) -> int:
    cur = db().execute("INSERT INTO messages(session_id, ts, role, content, meta_json) VALUES(?,?,?,?,?)",
                       (session_id, time.time(), role, content, _j(meta or {})))
    db().commit()
    return cur.lastrowid


def messages(session_id: str, limit: int = 200) -> list[dict]:
    rows = db().execute("SELECT * FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?) "
                        "ORDER BY id", (session_id, limit)).fetchall()
    return [{**dict(r), "meta": json.loads(r["meta_json"])} for r in rows]


def add_step(session_id: str, turn: int, kind: str, name: str, inp, out, prompt_tokens: int = 0,
             completion_tokens: int = 0, seconds: float = 0.0):
    db().execute("INSERT INTO steps(session_id, turn, ts, kind, name, input_json, output_json, prompt_tokens, "
                 "completion_tokens, seconds) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (session_id, turn, time.time(), kind, name, _j(inp), _j(out), prompt_tokens, completion_tokens,
                  seconds))
    db().commit()


def steps(session_id: str) -> list[dict]:
    rows = db().execute("SELECT * FROM steps WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    return [{**dict(r), "input": json.loads(r["input_json"]), "output": json.loads(r["output_json"])} for r in rows]


def handoff(session_id: str, reason: str, summary: str, contact: str) -> tuple[int, bool]:
    """Открытая заявка диалога или новая, одной транзакцией. Возвращает (номер, создана ли новая).

    Если у диалога уже есть заявка new или taken, новая не заводится: непустые сводка и контакт дополняют
    открытую, номер и первая причина остаются. Так сбой после передачи не дает менеджеру второй заявки.
    """
    con = db()
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")  # между проверкой и записью другой поток заявку не заведет
    try:
        row = con.execute("SELECT id, summary, contact FROM handoffs WHERE session_id=? AND status IN ('new', 'taken') "
                          "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if row:
            hid, created = row["id"], False
            con.execute("UPDATE handoffs SET summary=?, contact=? WHERE id=?",
                        (summary or row["summary"], contact or row["contact"], hid))
        else:
            hid, created = con.execute("INSERT INTO handoffs(session_id, ts, reason, summary, contact) "
                                       "VALUES(?,?,?,?,?)",
                                       (session_id, time.time(), reason, summary, contact)).lastrowid, True
        con.execute("UPDATE sessions SET status='handoff' WHERE id=?", (session_id,))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return hid, created


def add_handoff(session_id: str, reason: str, summary: str, contact: str) -> int:
    """Номер открытой заявки диалога: существующей или только что заведенной (см. handoff)."""
    return handoff(session_id, reason, summary, contact)[0]


def open_handoff(session_id: str) -> dict | None:
    row = db().execute("SELECT id, reason, summary, contact FROM handoffs WHERE session_id=? "
                       "AND status IN ('new', 'taken') ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    return dict(row) if row else None


def update_handoff(handoff_id: int, summary: str, contact: str):
    db().execute("UPDATE handoffs SET summary=?, contact=? WHERE id=?", (summary, contact, handoff_id))
    db().commit()


def take_handoff(handoff_id: int, who: str) -> bool:
    """Менеджер берет обращение в работу: остальные видят, что оно занято."""
    cur = db().execute("UPDATE handoffs SET status='taken', assignee=?, taken_at=? WHERE id=? AND status='new'",
                       (who[:60], time.time(), handoff_id))
    db().commit()
    return cur.rowcount == 1


def return_handoff(handoff_id: int) -> bool:
    """Менеджер возвращает диалог агенту: вопрос решается без человека."""
    con = db()
    row = con.execute("SELECT session_id FROM handoffs WHERE id=? AND status IN ('new', 'taken')",
                      (handoff_id,)).fetchone()
    if row is None:
        return False
    con.execute("UPDATE handoffs SET status='returned' WHERE id=?", (handoff_id,))
    con.execute("UPDATE sessions SET status='bot' WHERE id=?", (row["session_id"],))
    con.commit()
    return True


def last_total(session_id: str) -> float | None:
    """Последняя сумма, посчитанная кодом в этом диалоге (расчет или КП): для карточки менеджера."""
    for r in db().execute("SELECT output_json FROM steps WHERE session_id=? AND kind='tool' "
                          "AND name IN ('calc_total', 'make_quote') ORDER BY id DESC", (session_id,)):
        total = json.loads(r["output_json"]).get("total")
        if total:
            return float(total)
    return None


def drop_session(session_id: str):
    con = db()
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_cards'").fetchone():
        # копия текста карточки Telegram: в ней контакт и реквизиты клиента, она уходит вместе с заявкой
        con.execute("DELETE FROM tg_cards WHERE handoff_id IN (SELECT id FROM handoffs WHERE session_id=?)",
                    (session_id,))
    for table in ("messages", "steps", "handoffs", "quotes"):
        con.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
    con.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    con.commit()


def add_quote(session_id: str, number: str, customer: str, items: list[dict], total: float, file: str) -> int:
    cur = db().execute("INSERT INTO quotes(session_id, ts, number, customer, items_json, total, file) "
                       "VALUES(?,?,?,?,?,?,?)", (session_id, time.time(), number, customer, _j(items), total, file))
    db().commit()
    return cur.lastrowid


def last_quote(session_id: str) -> dict | None:
    row = db().execute("SELECT id, number, customer, total, items_json FROM quotes WHERE session_id=? ORDER BY id DESC LIMIT 1",
                       (session_id,)).fetchone()
    return dict(row) if row else None


def update_quote(quote_id: int, customer: str, items: list[dict], total: float):
    db().execute("UPDATE quotes SET ts=?, customer=?, items_json=?, total=? WHERE id=?",
                 (time.time(), customer, _j(items), total, quote_id))
    db().commit()


def previous_session(client_id: str, exclude: str) -> dict | None:
    """Прошлый диалог того же клиента: память между визитами. Старые визиты не вспоминаются: цены уже не те."""
    if not client_id:
        return None
    row = db().execute("SELECT id FROM sessions WHERE client_id=? AND id<>? AND updated_at>=? "
                       "ORDER BY updated_at DESC LIMIT 1",
                       (client_id, exclude, time.time() - config.PREVIOUS_VISIT_DAYS * 86400)).fetchone()
    return get_session(row["id"]) if row else None


def tokens_since(ts: float) -> tuple[int, int]:
    """Сколько токенов ушло в модель с момента ts: для суточного потолка расхода."""
    row = db().execute("SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM steps "
                       "WHERE kind='llm' AND ts>=?", (ts,)).fetchone()
    return row[0], row[1]


def purge(days: int) -> int:
    """Удаляет диалоги старше days дней вместе с шагами, обращениями, КП и их файлами. Знания из ответов менеджеров
    остаются."""
    con = db()
    old = [r[0] for r in con.execute("SELECT id FROM sessions WHERE updated_at<?", (time.time() - days * 86400,))]
    for sid in old:
        # в PDF черновика название компании и ИНН клиента: файл уходит вместе с диалогом
        for (number,) in con.execute("SELECT number FROM quotes WHERE session_id=?", (sid,)).fetchall():
            (config.QUOTES_DIR / f"{number}.pdf").unlink(missing_ok=True)
        drop_session(sid)
    return len(old)
