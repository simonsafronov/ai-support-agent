"""Веб-сервер стенда: чат покупателя, экран оператора, файлы КП."""
from __future__ import annotations

import contextlib
import csv
import datetime
import io
import json
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import cases, config, economy, store
from app.agent import handoff_card, loop, proof
from app.catalog import search as catalog, update as catalog_update
from app.kb import answers, search as kb

STATIC = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(_app):
    if config.FEED_URL:  # каталог обновляется с фида раз в сутки, пока работает сервер
        threading.Thread(target=_daily_update, daemon=True).start()
    yield


app = FastAPI(title="ИИ-консультант Softline Store", docs_url=None, redoc_url=None, lifespan=lifespan)


_recent: dict[str, list[float]] = {}  # адрес клиента -> время его последних сообщений в чат
_recent_lock = threading.Lock()


def too_often(key: str) -> bool:
    """Больше CHAT_RATE_PER_MIN сообщений в минуту с одного адреса: защита расхода на модель."""
    if not config.CHAT_RATE_PER_MIN:
        return False
    now = time.time()
    with _recent_lock:
        if len(_recent) > 5000:  # старые адреса не копятся
            for k in [k for k, v in _recent.items() if not v or now - v[-1] > 60]:
                del _recent[k]
        times = _recent[key] = [t for t in _recent.get(key, []) if now - t < 60]
        if len(times) >= config.CHAT_RATE_PER_MIN:
            return True
        times.append(now)
        return False


@app.middleware("http")
async def guard(request, call_next):
    """Лимит сообщений в чат, заголовки безопасности и перепроверка страниц стенда браузером."""
    if request.url.path == "/api/chat" and request.method == "POST":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if too_often(forwarded or (request.client.host if request.client else "?")):
            return JSONResponse({"detail": "слишком много сообщений подряд"}, status_code=429)
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/quotes/", "/fonts/")):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if not request.url.path.startswith("/quotes/"):  # PDF открывает встроенный просмотрщик браузера
        response.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                       "img-src 'self' data:; frame-ancestors 'none'")
    return response


MSK = datetime.timezone(datetime.timedelta(hours=3))
_update_lock = threading.Lock()


def update_catalog() -> bool:
    """Пересобирает каталог с фида отдельным процессом: чат в это время отвечает по прежней базе."""
    if not config.FEED_URL or not _update_lock.acquire(blocking=False):
        return False

    def work():
        try:
            subprocess.run([sys.executable, "-m", "app.catalog.update"], cwd=config.ROOT, timeout=3600)
        finally:
            _update_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def _daily_update():
    while True:
        now = datetime.datetime.now(MSK)
        run_at = now.replace(hour=config.FEED_UPDATE_HOUR, minute=0, second=0, microsecond=0)
        time.sleep(((run_at - now).total_seconds() - 1) % 86400 + 1)
        store.purge(config.RETENTION_DAYS)  # диалоги с контактами клиентов не хранятся дольше срока
        update_catalog()


def is_operator(request: Request) -> bool:
    given = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    return bool(config.ADMIN_TOKEN) and secrets.compare_digest(given.encode(), config.ADMIN_TOKEN.encode())


def admin(request: Request):
    """Действия оператора и данные клиентов целиком: только с ключом ADMIN_TOKEN."""
    if not is_operator(request):
        raise HTTPException(403, "нужен ключ оператора: откройте экран по адресу /admin?token=..." if config.ADMIN_TOKEN
                            else "ключ оператора не задан: укажите ADMIN_TOKEN в .env")


def view(request: Request, payload: dict) -> dict:
    """Экран оператора без ключа открыт только на чтение, почта и телефоны клиентов в нем скрыты."""
    if is_operator(request):
        return {**payload, "role": "operator"}
    return {**_hide(json.loads(json.dumps(payload, ensure_ascii=False, default=str))), "role": "demo"}


_REQUISITE = re.compile(r"(?<!\d)(?<!\d[.,])\d{9,12}(?!\d|[.,]\d)")  # ИНН, КПП, ОГРН и номера счетов в тексте
_KEEP = {"id", "session_id", "number", "url", "file"}  # служебные поля: по ним экран открывает диалог и КП


def _hide(obj, key: str = ""):
    """Режим просмотра: почта, телефоны и реквизиты клиентов заменяются метками во всех текстах ответа."""
    if isinstance(obj, dict):
        return {k: _hide(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hide(v, key) for v in obj]
    if isinstance(obj, str) and key not in _KEEP:
        return _REQUISITE.sub("[реквизит]", loop.mask_contacts(obj)[0])
    return obj


_slots = threading.BoundedSemaphore(max(1, config.MAX_PARALLEL_TURNS))  # столько ходов модель обслуживает разом
_session_locks: dict[str, threading.Lock] = {}
_session_guard = threading.Lock()


def session_lock(key: str) -> threading.Lock:
    """Ходы одного диалога идут по очереди, разные диалоги друг друга не ждут."""
    with _session_guard:
        if len(_session_locks) > 5000:
            for k in [k for k, lock in _session_locks.items() if not lock.locked()]:
                del _session_locks[k]
        return _session_locks.setdefault(key, threading.Lock())  # GigaChat для физлиц работает в один поток: ходы идут по очереди


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
            with session_lock(body.session_id or body.client_id or "new"), _slots:
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
            "messages": [{"role": m["role"], "content": m["content"], "from": m["meta"].get("from", ""),
                          **{k: m["meta"].get(k, []) for k in ("cards", "choices", "events")}}
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


# --- экран оператора ---

def _session_row(r) -> dict:
    d = dict(r)
    d["cost_rub"] = loop.cost_rub(d["prompt_tokens"], d["completion_tokens"])
    return d


@app.get("/api/admin/sessions")
def admin_sessions(request: Request, limit: int = 100):
    con = store.db()
    rows = con.execute(
        "SELECT s.id, s.created_at, s.updated_at, s.status, s.page_url, s.prompt_tokens, s.completion_tokens, "
        "s.llm_calls, s.tool_calls, "
        "(SELECT content FROM messages m WHERE m.session_id=s.id AND m.role='user' ORDER BY m.id LIMIT 1) AS first, "
        "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.role='user') AS turns, "
        "(SELECT COUNT(*) FROM quotes q WHERE q.session_id=s.id) AS quotes "
        "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", (min(limit, 500),)).fetchall()
    return view(request, {"sessions": [_session_row(r) for r in rows]})


@app.get("/api/admin/session/{session_id}")
def admin_session(request: Request, session_id: str):
    con = store.db()
    row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "нет такого диалога")
    s = _session_row(row)
    s["facts"], s["items"] = json.loads(s.pop("facts_json")), json.loads(s.pop("items_json"))
    return view(request, {
        "session": s, "messages": store.messages(session_id), "steps": store.steps(session_id),
        "handoffs": _handoffs("WHERE session_id=?", (session_id,)),
        "quotes": [dict(r) for r in con.execute("SELECT * FROM quotes WHERE session_id=?", (session_id,))]})


def _handoffs(where: str = "", args: tuple = (), limit: int = 50) -> list[dict]:
    """Обращения с карточками: сначала открытые, внутри них срочные и те, у кого срок реакции ближе."""
    rows = [dict(r) for r in store.db().execute(f"SELECT * FROM handoffs {where} ORDER BY id DESC LIMIT ?",
                                                (*args, limit))]
    now, order = time.time(), {"high": 0, "normal": 1, "low": 2}
    for h in rows:
        h["card"] = handoff_card.build(h, now)
    return sorted(rows, key=lambda h: (h["status"] in ("answered", "returned"), order[h["card"]["priority"]],
                                       h["card"]["sla_due"]))


class TakeIn(BaseModel):
    who: str = Field(default="менеджер", max_length=60)


@app.post("/api/admin/handoff/{handoff_id}/take", dependencies=[Depends(admin)])
def admin_take(handoff_id: int, body: TakeIn):
    """Менеджер берет обращение в работу."""
    return {"ok": store.take_handoff(handoff_id, body.who.strip() or "менеджер")}


@app.post("/api/admin/handoff/{handoff_id}/return", dependencies=[Depends(admin)])
def admin_return(handoff_id: int):
    """Менеджер возвращает диалог агенту: человек здесь не нужен."""
    return {"ok": store.return_handoff(handoff_id)}


class CaseIn(BaseModel):
    handoff: bool | None = None  # как должно было быть: передать менеджеру, не передавать, None = допустимо и так и так
    quote: bool = False
    note: str = Field(default="", max_length=500)


@app.post("/api/admin/session/{session_id}/case", dependencies=[Depends(admin)])
def admin_case(session_id: str, body: CaseIn):
    """Диалог, где агент ошибся, становится кейсом проверочного набора: следующий прогон его проиграет."""
    case = cases.from_session(session_id, "any" if body.handoff is None else body.handoff, body.quote, body.note)
    if case is None:
        raise HTTPException(404, "в диалоге нет реплик клиента")
    return {"ok": True, "case_id": case["id"], "added_cases": cases.add(case)}


@app.post("/api/proof")
def proof_run():
    """Показ проверки цен: подставная модель называет выдуманную цену, настоящая проверка ее ловит."""
    return proof.price_guard()


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    teach: bool = False


@app.post("/api/admin/handoff/{handoff_id}/answer", dependencies=[Depends(admin)])
def admin_answer(handoff_id: int, body: AnswerIn):
    """Ответ менеджера на обращение: клиент видит его в чате; общий ответ становится знанием агента."""
    h = store.db().execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    if h is None:
        raise HTTPException(404, "нет такого обращения")
    asked = [m["content"] for m in store.messages(h["session_id"]) if m["role"] == "user"][-3:]
    question = loop.mask_contacts(" ".join(asked))[0][:600] + " | " + h["reason"] + ": " + h["summary"]
    return answers.add(handoff_id, body.answer.strip(), body.teach, question)


@app.get("/api/admin/export.csv", dependencies=[Depends(admin)])
def admin_export():
    """Журнал всех вопросов клиентов и ответов агента для контроля: одна строка на сообщение."""
    con = store.db()
    tools = {}
    for r in con.execute("SELECT session_id, turn, GROUP_CONCAT(name, ', ') AS names FROM steps WHERE kind='tool' "
                         "GROUP BY session_id, turn"):
        tools[(r["session_id"], r["turn"])] = r["names"]
    handed = {r[0] for r in con.execute("SELECT DISTINCT session_id FROM handoffs")}
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["время (МСК)", "диалог", "кто", "текст", "инструменты хода", "диалог передан менеджеру"])
    turn = {}
    for m in con.execute("SELECT session_id, ts, role, content, meta_json FROM messages ORDER BY id"):
        sid = m["session_id"]
        if m["role"] == "user":
            turn[sid] = turn.get(sid, 0) + 1
        who = "клиент" if m["role"] == "user" else ("менеджер" if '"from": "manager"' in m["meta_json"] else "агент")
        w.writerow([datetime.datetime.fromtimestamp(m["ts"], MSK).strftime("%d.%m.%Y %H:%M:%S"), sid, who,
                    m["content"], tools.get((sid, turn.get(sid, 0)), "") if who == "агент" else "",
                    "да" if sid in handed else "нет"])
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="dialogs.csv"'})


@app.get("/api/admin/catalog")
def admin_catalog():
    c = catalog.stats()
    return {"offers": c["offers"], "products": c["products"], "feed_date": c.get("feed_date"),
            "feed_url": config.FEED_URL, "update_hour_msk": config.FEED_UPDATE_HOUR,
            "updating": _update_lock.locked(), "last_update": catalog_update.last()}


@app.post("/api/admin/catalog/update", dependencies=[Depends(admin)])
def admin_catalog_update():
    return {"started": update_catalog()}


@app.get("/api/admin/stats")
def admin_stats(request: Request, minutes: float | None = Query(None, gt=0, le=600),
                rub_hour: float | None = Query(None, ge=0, le=100000),
                card_share: float | None = Query(None, ge=0, le=1), volume: int | None = Query(None, ge=1, le=10**7)):
    con = store.db()
    one = lambda sql: con.execute(sql).fetchone()[0] or 0
    dialogs = one("SELECT COUNT(*) FROM sessions WHERE llm_calls>0")
    pt, ct = one("SELECT SUM(prompt_tokens) FROM sessions"), one("SELECT SUM(completion_tokens) FROM sessions")
    turns = one("SELECT COUNT(*) FROM messages WHERE role='user'")
    handoff = one("SELECT COUNT(DISTINCT session_id) FROM handoffs")
    lat = one("SELECT AVG(t) FROM (SELECT SUM(seconds) AS t FROM steps WHERE kind='llm' GROUP BY session_id, turn)")
    total = loop.cost_rub(pt, ct)
    return view(request, {"dialogs": dialogs, "turns": turns, "handoff_dialogs": handoff,
            "without_human_share": round(1 - handoff / dialogs, 3) if dialogs else None,
            "quotes": one("SELECT COUNT(*) FROM quotes"), "learned_answers": answers.count(),
            "prompt_tokens": pt, "completion_tokens": ct,
            "cost_rub_total": total, "cost_rub_per_turn": round(total / turns, 2) if turns else None,
            "cost_rub_per_dialog": round(total / dialogs, 2) if dialogs else None,
            "avg_model_seconds_per_turn": round(lat, 1) if lat else None,
            "handoffs": _handoffs(), "added_cases": len(cases.load()), "stand_note": config.STAND_NOTE,
            "economy": economy.calc(dialogs, handoff, total, minutes, rub_hour, card_share, volume),
            "price_per_1k": {"in": config.PRICE_IN_PER_1K, "out": config.PRICE_OUT_PER_1K}, "now": time.time()})


@app.get("/quotes/{name}")
def quote_file(name: str):
    f = (config.QUOTES_DIR / name).resolve()
    if f.parent != config.QUOTES_DIR.resolve() or f.suffix != ".pdf" or not f.exists():
        raise HTTPException(404, "нет такого файла")
    return FileResponse(f, media_type="application/pdf", filename=name)


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/proof")
def proof_page():
    return FileResponse(STATIC / "proof.html")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
