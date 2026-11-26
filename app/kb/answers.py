"""Ответы менеджеров как знание агента.

Менеджер отвечает на переданное обращение на экране оператора. Клиент видит ответ в чате. Если менеджер
отметил ответ как общий, он попадает в поиск: на похожий вопрос следующего клиента агент получит его
вместе с результатами каталога или справки и ответит сам. Модель при этом не переобучается.
"""
from __future__ import annotations

import time

from app import store
from app.kb.search import fts_query, index_text, query_terms

MIN_SHARE = 0.6  # какая доля слов вопроса должна совпасть, чтобы прошлый ответ считался ответом на него


def add(handoff_id: int, answer: str, teach: bool, question: str) -> dict | None:
    """Записывает ответ менеджера, показывает его клиенту и закрывает обращение."""
    con = store.db()
    h = con.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    if h is None:
        return None
    store.add_message(h["session_id"], "assistant", "Ответ менеджера: " + answer, {"from": "manager"})
    cur = con.execute("INSERT INTO answers(ts, handoff_id, session_id, question, answer, teach) VALUES(?,?,?,?,?,?)",
                      (time.time(), handoff_id, h["session_id"], question, answer, int(teach)))
    if teach:
        con.execute("INSERT INTO answers_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, index_text(question)))
    con.execute("UPDATE handoffs SET status='answered' WHERE id=?", (handoff_id,))
    con.execute("UPDATE sessions SET status='bot' WHERE id=?", (h["session_id"],))
    con.commit()
    return {"id": cur.lastrowid, "session_id": h["session_id"], "teach": bool(teach)}


def find(query: str, limit: int = 2) -> list[dict]:
    """Прошлые ответы менеджеров на похожие вопросы, самые близкие первыми."""
    terms = query_terms(query)
    if not terms:
        return []
    rows = store.db().execute(
        "SELECT a.question, a.answer, a.ts, f.text FROM answers_fts f JOIN answers a ON a.id = f.rowid "
        "WHERE answers_fts MATCH ? ORDER BY bm25(answers_fts) LIMIT 20", (fts_query(terms),)).fetchall()
    found = []
    for r in rows:
        words = r["text"].split()
        hit = sum(1 for s, by_prefix in terms if any(w.startswith(s) if by_prefix else w == s for w in words))
        if hit / len(terms) >= MIN_SHARE:
            found.append({"question": r["question"][:300], "answer": r["answer"][:1200],
                          "date": time.strftime("%d.%m.%Y", time.localtime(r["ts"]))})
    return found[:limit]


def count() -> int:
    return store.db().execute("SELECT COUNT(*) FROM answers WHERE teach=1").fetchone()[0]
