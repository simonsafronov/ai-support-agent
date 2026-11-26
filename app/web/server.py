"""Веб-сервер стенда: чат покупателя, экран оператора, файлы КП."""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, store
from app.agent import loop
from app.catalog import search as catalog
from app.kb import search as kb

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="ИИ-консультант Softline Store", docs_url=None, redoc_url=None)


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Страницы и скрипты стенда браузер перепроверяет при каждом заходе: правки видны сразу."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/quotes/", "/fonts/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


_turn_lock = threading.Lock()  # GigaChat для физлиц работает в один поток: ходы идут по очереди


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    client_id: str = Field(default="", max_length=64)
    page_url: str = Field(default="", max_length=500)


@app.get("/api/health")
def health():
    c = catalog.stats()
    return {"ok": True, "offers": c["offers"], "products": c["products"], "feed_date": c.get("feed_date"),
            "kb_chunks": kb.stats()["chunks"], "model": config.LLM_MODEL, "provider": config.LLM_PROVIDER}


@app.post("/api/chat")
def chat(body: ChatIn):
    """Ответ потоком строк JSON: сначала шаги агента (какой инструмент вызван), в конце ответ клиенту."""
    q: queue.Queue = queue.Queue()

    def work():
        try:
            with _turn_lock:
                res = loop.run_turn(body.session_id, body.message.strip(), client_id=body.client_id,
                                    page_url=body.page_url, on_event=q.put)
            q.put({"type": "reply", **res})
        except Exception as e:  # клиент должен получить ответ при любом сбое
            q.put({"type": "reply", "session_id": body.session_id, "reply": loop.FALLBACK, "cards": [],
                   "choices": [], "events": [], "trace": [{"kind": "error", "name": type(e).__name__,
                                                            "output": str(e)}]})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            try:
                ev = q.get(timeout=20)
            except queue.Empty:
                yield json.dumps({"type": "wait"}) + "\n"
                continue
            if ev is None:
                return
            yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/session/{session_id}")
def session(session_id: str):
    s = store.get_session(session_id)
    return {"session_id": s["id"], "status": s["status"],
            "messages": [{"role": m["role"], "content": m["content"], **{k: m["meta"].get(k, []) for k in
                                                                         ("cards", "choices", "events")}}
                         for m in store.messages(session_id)]}


# Витрина без запроса: продукты, о которых чаще всего спрашивают в обращениях (docs/chat_analysis.md)
FEATURED = ("Kaspersky Endpoint Security для бизнеса", "Astra Linux Special Edition", "КриптоПро CSP",
            "Microsoft 365", "Dr.Web Security Space", "МойОфис Стандартный", "Windows 11 Pro", "РЕД ОС")


@app.get("/api/products")
def products(q: str = "", limit: int = 12):
    """Поиск по каталогу для витрины стенда."""
    if q.strip():
        return catalog.search_products(q, limit=min(limit, 24))
    found, seen = [], set()
    for name in FEATURED:
        for p in catalog.search_products(name, limit=1).get("products", []):
            if p["product_id"] not in seen:
                seen.add(p["product_id"])
                found.append(p)
    return {"query": "", "total": len(found), "products": found}


# --- экран оператора: только чтение ---

def _session_row(r) -> dict:
    d = dict(r)
    d["cost_rub"] = loop.cost_rub(d["prompt_tokens"], d["completion_tokens"])
    return d


@app.get("/api/admin/sessions")
def admin_sessions(limit: int = 100):
    con = store.db()
    rows = con.execute(
        "SELECT s.id, s.created_at, s.updated_at, s.status, s.page_url, s.prompt_tokens, s.completion_tokens, "
        "s.llm_calls, s.tool_calls, "
        "(SELECT content FROM messages m WHERE m.session_id=s.id AND m.role='user' ORDER BY m.id LIMIT 1) AS first, "
        "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.role='user') AS turns, "
        "(SELECT COUNT(*) FROM quotes q WHERE q.session_id=s.id) AS quotes "
        "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", (min(limit, 500),)).fetchall()
    return {"sessions": [_session_row(r) for r in rows]}


@app.get("/api/admin/session/{session_id}")
def admin_session(session_id: str):
    con = store.db()
    row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "нет такого диалога")
    s = _session_row(row)
    s["facts"], s["items"] = json.loads(s.pop("facts_json")), json.loads(s.pop("items_json"))
    return {"session": s, "messages": store.messages(session_id), "steps": store.steps(session_id),
            "handoffs": [dict(r) for r in con.execute("SELECT * FROM handoffs WHERE session_id=?", (session_id,))],
            "quotes": [dict(r) for r in con.execute("SELECT * FROM quotes WHERE session_id=?", (session_id,))]}


@app.get("/api/admin/stats")
def admin_stats():
    con = store.db()
    one = lambda sql: con.execute(sql).fetchone()[0] or 0
    dialogs = one("SELECT COUNT(*) FROM sessions WHERE llm_calls>0")
    pt, ct = one("SELECT SUM(prompt_tokens) FROM sessions"), one("SELECT SUM(completion_tokens) FROM sessions")
    turns = one("SELECT COUNT(*) FROM messages WHERE role='user'")
    handoff = one("SELECT COUNT(DISTINCT session_id) FROM handoffs")
    lat = one("SELECT AVG(t) FROM (SELECT SUM(seconds) AS t FROM steps WHERE kind='llm' GROUP BY session_id, turn)")
    total = loop.cost_rub(pt, ct)
    return {"dialogs": dialogs, "turns": turns, "handoff_dialogs": handoff,
            "without_human_share": round(1 - handoff / dialogs, 3) if dialogs else None,
            "quotes": one("SELECT COUNT(*) FROM quotes"), "prompt_tokens": pt, "completion_tokens": ct,
            "cost_rub_total": total, "cost_rub_per_turn": round(total / turns, 2) if turns else None,
            "cost_rub_per_dialog": round(total / dialogs, 2) if dialogs else None,
            "avg_model_seconds_per_turn": round(lat, 1) if lat else None,
            "handoffs": [dict(r) for r in con.execute("SELECT * FROM handoffs ORDER BY id DESC LIMIT 50")],
            "price_per_1k": {"in": config.PRICE_IN_PER_1K, "out": config.PRICE_OUT_PER_1K}, "now": time.time()}


@app.get("/quotes/{name}")
def quote_file(name: str):
    f = (config.QUOTES_DIR / name).resolve()
    if f.parent != config.QUOTES_DIR.resolve() or f.suffix != ".pdf" or not f.exists():
        raise HTTPException(404, "нет такого файла")
    return FileResponse(f, media_type="application/pdf", filename=name)


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
