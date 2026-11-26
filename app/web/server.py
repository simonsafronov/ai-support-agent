"""Веб-сервер стенда: чат покупателя, экран оператора, файлы КП."""
from __future__ import annotations

import contextlib
import csv
import datetime
import hashlib
import hmac
import io
import json
import logging
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

from app import cases, config, economy, notify, store
from app.agent import handoff_card, llm, loop, proof
from app.catalog import search as catalog, update as catalog_update
from app.kb import answers, search as kb

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("server")


@contextlib.asynccontextmanager
async def lifespan(_app):
    # раз в сутки: чистка старых диалогов всегда, обновление каталога с фида, если он задан
    threading.Thread(target=_daily_update, daemon=True).start()
    notify.start(manager_answer)  # карточки обращений в Telegram и ответы менеджеров оттуда, если бот настроен
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


def client_address(request) -> str:
    """Адрес клиента для лимита частоты.

    X-Forwarded-For дописывает каждый прокси, а начало списка присылает сам клиент и может подделать.
    Поэтому берется значение, которое добавил наш прокси: PROXY_HOPS-е с конца. Без прокси или без
    заголовка берется адрес соединения.
    """
    chain = [a.strip() for a in request.headers.get("x-forwarded-for", "").split(",") if a.strip()]
    if config.PROXY_HOPS and len(chain) >= config.PROXY_HOPS:
        return chain[-config.PROXY_HOPS]
    return request.client.host if request.client else "?"


LIMITED = ("/api/chat", "/api/chat/handoff", "/api/proof")  # каждый такой запрос запускает ход агента


@app.middleware("http")
async def guard(request, call_next):
    """Лимит сообщений в чат, заголовки безопасности и перепроверка страниц стенда браузером."""
    if request.url.path in LIMITED and request.method == "POST" and too_often(client_address(request)):
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
        _daily_work()


def _daily_work():
    """Работа раз в сутки. Сбой пишется в журнал, а поток живет до следующих суток."""
    try:
        store.purge(config.RETENTION_DAYS)  # диалоги с контактами клиентов не хранятся дольше срока
    except Exception:
        log.exception("purge")
    try:
        if config.FEED_URL:
            update_catalog()
    except Exception:
        log.exception("catalog update")


def is_operator(request: Request) -> bool:
    given = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    return bool(config.ADMIN_TOKEN) and secrets.compare_digest(given.encode(), config.ADMIN_TOKEN.encode())


def admin(request: Request):
    """Действия оператора и данные клиентов целиком: только с ключом ADMIN_TOKEN."""
    if not is_operator(request):
        raise HTTPException(403, "нужен ключ оператора: откройте экран по адресу /admin?token=..." if config.ADMIN_TOKEN
                            else "ключ оператора не задан: укажите ADMIN_TOKEN в .env")


def view(request: Request, payload: dict) -> dict:
    """Экран оператора без ключа открыт только на чтение: контакты, реквизиты и файлы КП в нем скрыты."""
    if is_operator(request):
        return {**sign_links(payload), "role": "operator"}
    return {**_hide(json.loads(json.dumps(payload, ensure_ascii=False, default=str))), "role": "demo"}


_REQUISITE = re.compile(r"(?<!\d)(?<!\d[.,])\d{9,12}(?!\d|[.,]\d)")  # ИНН, КПП, ОГРН и номера счетов в тексте
_KEEP = {"id", "session_id", "number", "url"}  # служебные поля: по ним экран открывает диалог
_OPERATOR_ONLY = {"client_id", "file"}  # по client_id чат отдает клиенту его переписку; file это путь на сервере
_QUOTE_URL = re.compile(r"/quotes/([^/?#\s]+\.pdf)")


def _hide(obj, key: str = ""):
    """Режим просмотра: почта, телефоны и реквизиты клиентов заменяются метками, ссылок на файлы КП нет."""
    if isinstance(obj, dict):
        return {k: _hide(v, k) for k, v in obj.items() if k not in _OPERATOR_ONLY}
    if isinstance(obj, list):
        return [_hide(v, key) for v in obj]
    if isinstance(obj, str) and key == "url" and _QUOTE_URL.fullmatch(obj):
        return ""  # в КП реквизиты клиента: файл открывается только оператору
    if isinstance(obj, str) and key not in _KEEP:
        return _REQUISITE.sub("[реквизит]", loop.mask_contacts(obj)[0])
    return obj


_secret = b""


def _quote_secret() -> bytes:
    """Секрет подписи ссылок на КП: ключ оператора, а пока он не задан, случайная строка в data/runtime."""
    global _secret
    if config.ADMIN_TOKEN:
        return config.ADMIN_TOKEN.encode()
    if not _secret:
        f = config.ROOT / "data" / "runtime" / "quote_secret"
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        _secret = f.read_text(encoding="utf-8").strip().encode()
    return _secret


def quote_key(name: str) -> str:
    """Подпись имени файла КП. Номера КП идут подряд, поэтому одного имени для доступа к файлу мало."""
    return hmac.new(_quote_secret(), ("quote:" + name).encode(), hashlib.sha256).hexdigest()[:32]


def sign_links(obj, key: str = ""):
    """Дописывает подпись к ссылкам на КП, которые поставил код (поле url), перед отдачей владельцу или оператору.

    Текст ответа и сообщений не трогается: иначе клиент получил бы подпись к чужому КП, назвав его номер.
    """
    if isinstance(obj, dict):
        return {k: sign_links(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sign_links(v, key) for v in obj]
    if isinstance(obj, str) and key == "url" and _QUOTE_URL.fullmatch(obj):
        return f"{obj}?k={quote_key(obj.rsplit('/', 1)[1])}"
    return obj


def _for_knowledge(text: str) -> str:
    """Текст для общих знаний агента: без контактов, реквизитов и меток, как сообщение клиента для модели."""
    return _REQUISITE.sub("[реквизит]", loop.for_model(text))


def _brief(name: str, out) -> dict:
    """Итог инструмента для панели «Ход работы агента»: только поля, из которых чат собирает подпись шага.

    Полный ответ инструмента в браузер не уходит: в нем пути файлов на сервере и то, что нашлось в знаниях.
    """
    if not isinstance(out, dict):
        return {}
    if out.get("error"):
        return {"error": str(out["error"])[:200]}
    if name in ("search_products", "find_analogs"):
        return {"total": out.get("total"),
                "products": [{"title": p.get("title")} for p in (out.get("products") or [])[:3]]}
    if name == "get_product":
        return {"matching": out.get("matching"), "total_offers": out.get("total_offers"),
                "clarify": (out.get("clarify") or [])[:3]}
    if name == "browse_categories":
        return {"categories": [{"name": c.get("name")} for c in (out.get("categories") or [])[:4]]}
    if name == "kb_search":
        return {"results": [{"title": r.get("title"), "heading": r.get("heading")}
                            for r in (out.get("results") or [])[:2]]}
    if name == "note_facts":
        return {"saved": out.get("saved") or {}}
    return {k: out[k] for k in ("ok", "number", "total", "ticket") if k in out}


FAILED = {"llm": "модель не ответила", "deadline": "ход занял слишком много времени"}


def public_trace(trace: list[dict]) -> list[dict]:
    """След хода в том виде, в каком его получает браузер клиента. Полный след видит оператор на своем экране."""
    out = []
    for t in trace:
        if t["kind"] == "tool":
            out.append({"kind": "tool", "name": t["name"], "output": _brief(t["name"], t.get("output"))})
        elif t["kind"] == "guard":
            out.append({"kind": "guard", "name": t["name"], "output": t.get("output") if t["name"] == "price" else []})
        else:  # текст ошибки остается в журнале шагов: в нем бывают пути и внутренние имена
            out.append({"kind": "error", "name": t["name"], "output": FAILED.get(t["name"], "сбой на сервере")})
    return out


def public_reply(res: dict) -> dict:
    """Ответ хода для браузера: ссылки на КП подписаны, след шагов сокращен."""
    return {**res, "events": sign_links(res.get("events", [])), "trace": public_trace(res.get("trace", []))}


def own_session(session_id: str | None, client_id: str) -> str | None:
    """Номер диалога, если это диалог этого клиента. Чужой или придуманный номер не принимается."""
    s = store.find_session(session_id)
    return session_id if s and client_id and s["client_id"] == client_id else None


_slots = threading.BoundedSemaphore(max(1, config.MAX_PARALLEL_TURNS))  # столько ходов модель обслуживает разом
_session_locks: dict[str, threading.Lock] = {}
_session_guard = threading.Lock()


def session_lock(key: str) -> threading.Lock:
    """Ходы одного диалога идут по очереди, разные диалоги друг друга не ждут."""
    with _session_guard:
        if len(_session_locks) > 5000:
            for k in [k for k, lock in _session_locks.items() if not lock.locked()]:
                del _session_locks[k]
        return _session_locks.setdefault(key, threading.Lock())


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    client_id: str = Field(default="", max_length=64)
    page_url: str = Field(default="", max_length=500)


@app.get("/api/health")
def health():
    c = catalog.stats()
    return {"ok": True, "offers": c["offers"], "products": c["products"], "feed_date": c.get("feed_date"),
            "kb_chunks": kb.stats()["chunks"], "model": config.LLM_MODEL, "provider": config.LLM_PROVIDER,
            "label": config.LLM_LABEL, "online": llm.model_online()}


@app.post("/api/chat")
def chat(body: ChatIn):
    """Ответ потоком строк JSON: сначала шаги агента (какой инструмент вызван), в конце ответ клиенту."""
    q: queue.Queue = queue.Queue()
    text = body.message.strip()
    # диалог заводится здесь, а не в цикле агента: при любом сбое известно, куда записать ответ и обращение
    sid = own_session(body.session_id, body.client_id) or store.get_session(None, body.client_id, body.page_url)["id"]

    def work():
        try:
            with session_lock(sid), _slots:
                try:
                    res = loop.run_turn(sid, text, client_id=body.client_id, page_url=body.page_url, on_event=q.put)
                except Exception as e:  # клиент получает ответ, а менеджер обращение при любом сбое
                    res = failed_turn(sid, text, e)
            q.put({"type": "reply", **public_reply(res)})
        except Exception as e:  # не записалось и обращение: ответ клиенту все равно уходит
            q.put({"type": "reply", "session_id": sid, "reply": loop.FALLBACK, "cards": [], "choices": [],
                   "events": [], "trace": public_trace([{"kind": "error", "name": type(e).__name__}])})
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


def failed_turn(sid: str, text: str, error: Exception) -> dict:
    """Ход оборвался не по вине модели (ошибку модели цикл агента обрабатывает сам): ответ и обращение менеджеру."""
    last = store.messages(sid, limit=1)
    if not last or (last[0]["role"], last[0]["content"]) != ("user", text):
        store.add_message(sid, "user", text)  # сбой случился раньше, чем сообщение клиента попало в журнал
    events = []
    if not store.open_handoff(sid):
        contact = store.get_session(sid)["facts"].get("контакт") or ", ".join(loop.mask_contacts(text)[1])
        hid, created = store.handoff(sid, "сбой при ответе агента", f"Последнее сообщение клиента: {text[:500]}",
                                     contact)
        if created:
            events.append({"type": "handoff", "id": hid, "reason": "сбой при ответе агента"})
    store.add_step(sid, sum(1 for m in store.messages(sid) if m["role"] == "user"), "error", type(error).__name__,
                   {}, {"error": str(error)})
    meta = {"cards": [], "choices": [], "events": events}
    store.add_message(sid, "assistant", loop.FALLBACK, meta)
    return {"session_id": sid, "reply": loop.FALLBACK, **meta,
            "trace": [{"kind": "error", "name": type(error).__name__, "output": str(error)}]}


class CallIn(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    client_id: str = Field(default="", max_length=64)
    page_url: str = Field(default="", max_length=500)


CALLED = "Позвал менеджера: он ответит в этом чате в рабочее время."


@app.post("/api/chat/handoff")
def call_manager(body: CallIn):
    """Клиент сам зовет менеджера кнопкой: обращение заводит код, модель для этого не вызывается."""
    own = own_session(body.session_id, body.client_id)  # в чужой диалог обращение не заводится
    with session_lock(own or body.client_id or "new"):
        s = store.get_session(own, body.client_id, body.page_url)
        sid, ticket = s["id"], store.open_handoff(s["id"])
        created = False
        if not ticket:
            asked = [m["content"] for m in store.messages(sid) if m["role"] == "user"]
            said = f"Последнее сообщение клиента: {asked[-1][:500]}" if asked else "Клиент еще ничего не написал."
            hid, created = store.handoff(sid, "клиент позвал менеджера кнопкой", said, s["facts"].get("контакт", ""))
        else:
            hid = ticket["id"]
        reply = CALLED + ("" if s["facts"].get("контакт") else " Оставьте, пожалуйста, почту или телефон для связи.")
        events = [{"type": "handoff", "id": hid, "reason": "клиент позвал менеджера кнопкой"}] if created else []
        store.add_message(sid, "assistant", reply, {"cards": [], "choices": [], "events": events})
    return {"session_id": sid, "reply": reply, "cards": [], "choices": [], "events": events}


@app.get("/api/session/{session_id}")
def session(session_id: str, client_id: str = Query("", max_length=64)):
    """История диалога для чата после перезагрузки страницы: только его владельцу, чтение ничего не создает."""
    s = store.find_session(own_session(session_id, client_id))
    if s is None:
        raise HTTPException(404, "нет такого диалога")
    return {"session_id": s["id"], "status": s["status"],
            "messages": [{"id": m["id"], "role": m["role"], "content": m["content"], "from": m["meta"].get("from", ""),
                          "cards": m["meta"].get("cards", []), "choices": m["meta"].get("choices", []),
                          "events": sign_links(m["meta"].get("events", []))}
                         for m in store.messages(session_id)]}


@app.get("/api/session/{session_id}/answers")
def session_answers(session_id: str, client_id: str = Query("", max_length=64), after: int = 0):
    """Новые ответы менеджера для открытого чата: чат спрашивает о них, пока клиент на странице."""
    s = store.find_session(own_session(session_id, client_id))
    if s is None:
        raise HTTPException(404, "нет такого диалога")
    return {"answers": [{"id": m["id"], "content": m["content"]} for m in store.messages(session_id)
                        if m["id"] > after and m["meta"].get("from") == "manager"]}


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
    # сырые копии *_json не отдаются: рядом лежат разобранные поля, а в строке их не скрыть по имени поля
    rows = lambda items: [{k: r[k] for k in r.keys() if not k.endswith("_json") and k != "file"} for r in items]
    return view(request, {
        "session": s, "messages": rows(store.messages(session_id)), "steps": rows(store.steps(session_id)),
        "handoffs": _handoffs("WHERE session_id=?", (session_id,)),
        "quotes": [{**r, "url": f"/quotes/{r['number']}.pdf"}
                   for r in rows(con.execute("SELECT * FROM quotes WHERE session_id=?", (session_id,)).fetchall())]})


OPEN = "status IN ('new', 'taken')"
OPEN_MAX = 500  # открытых обращений на экране не больше: закрытые добираются до размера страницы
FROM_RUN = f"session_id IN (SELECT id FROM sessions WHERE {store.RUN_CLIENTS})"


def _handoffs(where: str = "", args: tuple = (), limit: int = 50) -> list[dict]:
    """Обращения с карточками: сначала открытые, внутри них срочные и те, у кого срок реакции ближе.

    Открытые выбираются все (до OPEN_MAX), закрытые добирают страницу до limit: старое открытое обращение
    не вытесняется более новыми закрытыми.
    """
    con, cond = store.db(), f"{where} AND" if where else "WHERE"
    rows = [dict(r) for r in con.execute(f"SELECT * FROM handoffs {cond} {OPEN} ORDER BY {FROM_RUN}, id DESC "
                                         f"LIMIT ?", (*args, OPEN_MAX))]
    rows += [dict(r) for r in con.execute(f"SELECT * FROM handoffs {cond} NOT {OPEN} ORDER BY id DESC LIMIT ?",
                                          (*args, max(0, limit - len(rows))))]
    run = {r[0] for r in con.execute(f"SELECT id FROM sessions WHERE {store.RUN_CLIENTS}")}
    now, order = time.time(), {"high": 0, "normal": 1, "low": 2}
    for h in rows:
        h["card"] = handoff_card.build(h, now)
        h["from_run"] = h["session_id"] in run  # диалог проверочного прогона: менеджер на него не отвечает
    return sorted(rows, key=lambda h: (h["status"] in ("answered", "returned"), h["from_run"],
                                       order[h["card"]["priority"]], h["card"]["sla_due"]))


def _queue(now: float) -> dict:
    """Счетчики очереди по всем открытым обращениям, а не по показанной странице."""
    con = store.db()
    count = lambda sql, args=(): con.execute(f"SELECT COUNT(*) FROM handoffs WHERE {OPEN} AND {sql}",
                                             args).fetchone()[0]
    late = now - max(handoff_card.SLA_MINUTES.values()) * 60  # старше самого долгого срока реакции: уже просрочено
    recent = con.execute(f"SELECT * FROM handoffs WHERE {OPEN} AND NOT {FROM_RUN} AND ts>=?", (late,)).fetchall()
    return {"waiting": count(f"NOT {FROM_RUN}"), "from_run": count(FROM_RUN),
            "overdue": count(f"NOT {FROM_RUN} AND ts<?", (late,))
                       + sum(handoff_card.build(dict(h), now)["overdue"] for h in recent)}


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


REPEAT_SEC = 10  # тот же ответ на то же обращение за это время считается повтором (двойной клик, повтор запроса)
_answered: dict[tuple[int, str], tuple[float, dict]] = {}
_answer_lock = threading.Lock()


def manager_answer(handoff_id: int, answer: str, teach: bool = False) -> dict | None:
    """Ответ менеджера с экрана оператора или из Telegram: клиент видит его в чате, общий ответ становится знанием.

    Повтор того же текста по тому же обращению в течение REPEAT_SEC второго сообщения клиенту не дает.
    """
    key, now = (handoff_id, answer), time.time()
    with _answer_lock:
        for k in [k for k, (ts, _) in _answered.items() if now - ts > REPEAT_SEC]:
            del _answered[k]
        if key in _answered:
            return _answered[key][1]
        done = _manager_answer(handoff_id, answer, teach)
        if done is not None:
            _answered[key] = (now, done)
        return done


def _manager_answer(handoff_id: int, answer: str, teach: bool) -> dict | None:
    h = store.db().execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    if h is None:
        return None
    asked = [m["content"] for m in store.messages(h["session_id"]) if m["role"] == "user"][-3:]
    # вопрос и ответ остаются в знаниях дольше диалога и уходят в модель: контакты и реквизиты из них убираются
    question = _for_knowledge(" ".join(asked))[:600] + " | " + h["reason"] + ": " + _for_knowledge(h["summary"])
    return answers.add(handoff_id, answer, teach, question, _for_knowledge(answer))


@app.post("/api/admin/handoff/{handoff_id}/answer", dependencies=[Depends(admin)])
def admin_answer(handoff_id: int, body: AnswerIn):
    """Ответ менеджера на обращение с экрана оператора."""
    done = manager_answer(handoff_id, body.answer.strip(), body.teach)
    if done is None:
        raise HTTPException(404, "нет такого обращения")
    return done


FORMULA = ("=", "+", "-", "@", "\t", "\r")  # с этого Excel начинает формулу: такая ячейка идет с апострофом


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
    cell = lambda v: "'" + v if isinstance(v, str) and v[:1] in FORMULA else v
    w.writerow(["время (МСК)", "диалог", "кто", "текст", "инструменты хода", "диалог передан менеджеру"])
    turn = {}
    for m in con.execute("SELECT session_id, ts, role, content, meta_json FROM messages ORDER BY id"):
        sid = m["session_id"]
        if m["role"] == "user":
            turn[sid] = turn.get(sid, 0) + 1
        who = "клиент" if m["role"] == "user" else ("менеджер" if '"from": "manager"' in m["meta_json"] else "агент")
        w.writerow(map(cell, [datetime.datetime.fromtimestamp(m["ts"], MSK).strftime("%d.%m.%Y %H:%M:%S"), sid,
                              who, m["content"], tools.get((sid, turn.get(sid, 0)), "") if who == "агент" else "",
                              "да" if sid in handed else "нет"]))
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
    # диалоги и переданные менеджеру из одной выборки: переданных не больше диалогов, доля от 0 до 1
    dialogs = one("SELECT COUNT(*) FROM sessions WHERE llm_calls>0")
    pt, ct = one("SELECT SUM(prompt_tokens) FROM sessions"), one("SELECT SUM(completion_tokens) FROM sessions")
    turns = one("SELECT COUNT(*) FROM messages WHERE role='user'")
    handoff = one("SELECT COUNT(DISTINCT session_id) FROM handoffs "
                  "WHERE session_id IN (SELECT id FROM sessions WHERE llm_calls>0)")
    lat = one("SELECT AVG(t) FROM (SELECT SUM(seconds) AS t FROM steps WHERE kind='llm' GROUP BY session_id, turn) "
              "WHERE t > 0")  # ходы пакетных прогонов времени не несут и в среднее не идут
    total, now = loop.cost_rub(pt, ct), time.time()
    return view(request, {"dialogs": dialogs, "turns": turns, "handoff_dialogs": handoff,
            "without_human_share": round(1 - handoff / dialogs, 3) if dialogs else None,
            "quotes": one("SELECT COUNT(*) FROM quotes"), "learned_answers": answers.count(),
            "prompt_tokens": pt, "completion_tokens": ct,
            "cost_rub_total": total, "cost_rub_per_turn": round(total / turns, 2) if turns else None,
            "cost_rub_per_dialog": round(total / dialogs, 2) if dialogs else None,
            "avg_model_seconds_per_turn": round(lat, 1) if lat else None,
            "handoffs": _handoffs(), "queue": _queue(now), "added_cases": len(cases.load()),
            "stand_note": config.STAND_NOTE,
            "economy": economy.calc(dialogs, handoff, total, minutes, rub_hour, card_share, volume, pt + ct),
            "price_per_1k": {"in": config.PRICE_IN_PER_1K, "out": config.PRICE_OUT_PER_1K}, "now": now})


@app.get("/quotes/{name}")
def quote_file(request: Request, name: str, k: str = Query("", max_length=64)):
    """Файл КП: по подписанной ссылке, которую клиент получил в чате, или с ключом оператора."""
    f = (config.QUOTES_DIR / name).resolve()
    allowed = is_operator(request) or secrets.compare_digest(k.encode(), quote_key(name).encode())
    if not allowed or f.parent != config.QUOTES_DIR.resolve() or f.suffix != ".pdf" or not f.exists() or \
            store.db().execute("SELECT 1 FROM quotes WHERE number=?", (f.stem,)).fetchone() is None:
        # без подписи ответ тот же, что и для несуществующего номера; файл без записи КП (диалог удален) не отдается
        raise HTTPException(404, "нет такого файла")
    return FileResponse(f, media_type="application/pdf", filename=name)


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/proof")
def proof_page():
    return FileResponse(STATIC / "proof.html")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
