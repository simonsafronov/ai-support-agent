"""Чат сайта в Webim: агент отвечает посетителю как бот через External Bot API 2.0 ("Умный бот").

Webim шлет события на адрес бота /api/webim/<WEBIM_SECRET> и ждет только подтверждения приема {"result": "ok"}:
ответ посетителю уходит позже отдельными вызовами send_message. Передача менеджеру переводит чат вызовом
redirect_chat в отдел WEBIM_DEP_KEY, сбой агента в общую очередь операторов. Ход агента тот же, что у чата
сайта: та же очередь к модели, тот же срок хода, те же проверки. Документация Webim:
https://webim.ru/kb/bots/smart-bot/v-2-0.html, подключение описано в docs/webim.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import config, store

log = logging.getLogger("webim")
router = APIRouter()

OK = {"result": "ok"}  # другой ответ на событие Webim считает отказом бота и переводит чат в общую очередь
CALL_TIMEOUT = 15.0    # секунд на вызов API Webim
# Своя кнопка под вариантами выбора: отдельного события "посетитель зовет оператора" в API Webim нет
MANAGER = {"id": "manager", "text": "Позвать менеджера"}
FILE_REPLY = "Файлы в этом чате я не открываю. Напишите, пожалуйста, вопрос текстом."
SEEN_TTL = 3600  # секунд: столько помнится принятое сообщение, повторы доставки Webim укладываются в 30 с

_seen: dict[str, float] = {}
_seen_lock = threading.Lock()


def _web():
    """Сервер стенда: ход агента, очередь к модели и подпись ссылок на КП те же, что у чата сайта.

    Импорт по месту: сервер сам подключает этот модуль.
    """
    from app.web import server
    return server


def session_id(chat_id: int) -> str:
    """Диалог агента для чата Webim, он же client_id. Номера чатов Webim идут подряд, поэтому к номеру добавлена
    подпись ключом бота: по номеру чата диалог через чат сайта не открыть и в память о прошлом визите не подставить."""
    tag = hmac.new(config.WEBIM_TOKEN.encode(), f"webim:{chat_id}".encode(), hashlib.sha256).hexdigest()[:12]
    return f"webim-{chat_id}-{tag}"


def first_time(chat_id: int, message: dict) -> bool:
    """Сообщение принято впервые. Webim повторяет доставку события, если не дождался ответа: второго хода нет."""
    key = f"{chat_id}:{message.get('id') or hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()}"
    now = time.time()
    with _seen_lock:
        if len(_seen) > 5000:
            for k in [k for k, t in _seen.items() if now - t > SEEN_TTL]:
                del _seen[k]
        if key in _seen:
            return False
        _seen[key] = now
        return True


def accept(body: dict) -> tuple[int, str, str] | None:
    """Что делать по событию: (номер чата, действие, текст) или None, если отвечать не на что.

    Действия: "turn" ход агента на текст посетителя, "manager" посетитель нажал кнопку менеджера, "file" посетитель
    прислал файл. new_chat несет сообщения, накопленные до назначения чата боту: на них один ход.
    """
    event = body.get("event")
    if event == "new_chat":
        chat_id = int(body["chat"]["id"])
        fresh = [m for m in body.get("messages") or [] if m.get("kind") == "visitor" and first_time(chat_id, m)]
        text = "\n".join(str(m.get("text") or "").strip() for m in fresh).strip()
        return (chat_id, "turn", text) if text else None
    if event not in ("new_message", "message_updated"):
        return None
    chat_id, m = int(body["chat_id"]), body["message"]
    kind, data = m.get("kind"), m.get("data") or {}
    if event == "new_message" and kind == "visitor":
        text = str(m.get("text") or "").strip()
        return (chat_id, "turn", text) if text and first_time(chat_id, m) else None
    if event == "new_message" and kind == "keyboard_response" and first_time(chat_id, m):
        button = data.get("button") or {}
        if button.get("id") == MANAGER["id"]:
            return chat_id, "manager", ""
        text = str(button.get("text") or "").strip()  # вариант выбора: как в чате сайта, его текст это реплика
        return (chat_id, "turn", text) if text else None
    # файл приходит несколькими запросами по ходу загрузки, готовый файл помечен state=ready
    if kind == "file_visitor" and data.get("state") == "ready" and first_time(chat_id, m):
        return chat_id, "file", ""
    return None


@router.post("/api/webim/{secret}")
async def webim_event(secret: str, request: Request):
    """Событие Webim. Ответ сразу: Webim ждет только подтверждения приема, ход агента идет в фоне."""
    if not config.WEBIM_SECRET or not hmac.compare_digest(secret.encode(), config.WEBIM_SECRET.encode()):
        raise HTTPException(403, "неверный адрес бота")
    received = time.monotonic()  # срок хода идет с приема сообщения, как у чата сайта
    try:
        job = accept(await request.json())
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"error": "incorrect-request"}, status_code=400)
    if job:
        _background(work, *job, received)
    return OK


def _background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def work(chat_id: int, action: str, text: str, received: float):
    """Фоновая часть события: ход агента и ответ посетителю вызовами Webim. Любой сбой отдает чат операторам."""
    try:
        if action == "turn":
            answer(chat_id, text, received)
        elif action == "manager":
            call_manager(chat_id)
        else:
            send(chat_id, {"kind": "operator", "text": FILE_REPLY})
    except Exception:
        log.exception("webim: чат %s", chat_id)
        redirect(chat_id, "")


def answer(chat_id: int, text: str, received: float):
    """Ход агента и его ответ: текст с карточками, КП файлом, варианты кнопками или перевод к людям."""
    web, sid = _web(), session_id(chat_id)
    body = web.ChatIn(message=text[:4000], session_id=sid, client_id=sid)
    with web.session_lock(sid):
        res = web.turn_in_slot(sid, body.message, body, received, None)
    failed = any(t["kind"] == "error" for t in res["trace"])  # модель не ответила, срок хода вышел, сбой кода
    handed = any(e["type"] == "handoff" for e in res["events"])
    delivered = send(chat_id, {"kind": "operator", "text": message_text(res)})
    for e in res["events"]:
        if e["type"] == "quote":
            send_quote(chat_id, e)
    if failed or not delivered:
        redirect(chat_id, "")
    elif handed:
        redirect(chat_id, config.WEBIM_DEP_KEY)
    elif res["choices"]:
        send(chat_id, {"kind": "keyboard", "buttons": [[{"id": f"choice-{i}", "text": c}]
                                                       for i, c in enumerate(res["choices"], 1)] + [[MANAGER]]})


def call_manager(chat_id: int):
    """Кнопка "Позвать менеджера": обращение заводит тот же код, что у кнопки в чате сайта, модель не вызывается."""
    web, sid = _web(), session_id(chat_id)
    store.get_session(sid, sid)  # кнопку могли нажать раньше первого хода: диалог заводится с номером этого чата
    res = web.call_manager(web.CallIn(session_id=sid, client_id=sid))
    send(chat_id, {"kind": "operator", "text": res["reply"]})
    redirect(chat_id, config.WEBIM_DEP_KEY)


def send_quote(chat_id: int, event: dict):
    """Черновик КП файлом: Webim сам скачивает файл по ссылке, поэтому ссылка полная и с подписью, как в чате сайта."""
    if not config.PUBLIC_URL:
        log.warning("webim: не задан PUBLIC_URL, черновик %s не отправлен", event["number"])
        return
    name = event["url"].rsplit("/", 1)[1]
    url = f"{config.PUBLIC_URL.rstrip('/')}/quotes/{quote(name)}?k={_web().quote_key(name)}"
    file = {"kind": "file_operator", "data": {"url": url, "name": name, "media_type": "application/pdf"}}
    if not send(chat_id, file):
        send(chat_id, {"kind": "operator", "text": f"Черновик {event['number']}: {url}"})  # PDF не разрешен в аккаунте


def message_text(res: dict) -> str:
    """Ответ агента простым текстом, карточки товаров строками под ним: окно Webim не рисует карточки чата сайта."""
    return "\n\n".join([plain(res["reply"])] + [card_text(c) for c in res["cards"]])


def plain(text: str) -> str:
    """Разметка, которую чат сайта рисует сам (жирный, заголовок, ссылка [текст](адрес)), уходит простым текстом."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1: \2", text)
    return re.sub(r"(?m)^#{1,4}\s+", "", re.sub(r"\*\*(.+?)\*\*", r"\1", text))


def rub(value) -> str:
    v = float(value)
    return (f"{v:,.0f}" if v.is_integer() else f"{v:,.2f}").replace(",", " ").replace(".", ",") + " ₽"


def card_text(c: dict) -> str:
    """Карточка товара строкой: название, цена из каталога, артикул и ссылка на страницу магазина."""
    if c["type"] == "offer":
        line = f"{c['title']}: {rub(c['price']) if c.get('price') else 'цена по запросу'}"
        line += f", артикул {c['vendor_code']}" if c.get("vendor_code") else ""
    else:
        low, high = c.get("price_min"), c.get("price_max")
        line = f"{c['title']}: {(rub(low) if low == high else 'от ' + rub(low)) if low else 'цена по запросу'}"
    return line + (f"\nОткрыть в магазине: {c['url']}" if c.get("url") else "")


def redirect(chat_id: int, dep_key: str) -> bool:
    """Перевод чата к людям: в отдел, если он задан, иначе в общую очередь. Отдел не принял чат (все операторы
    офлайн, неверный ключ): общая очередь."""
    if dep_key and call("redirect_chat", {"chat_id": chat_id, "dep_key": dep_key}):
        return True
    return call("redirect_chat", {"chat_id": chat_id})


def send(chat_id: int, message: dict) -> bool:
    return call("send_message", {"chat_id": chat_id, "message": message})


def call(method: str, payload: dict) -> bool:
    """Вызов API бота Webim. Успех это код 200 без поля error: прочие ошибки Webim отдает с кодом 200."""
    try:
        r = httpx.post(f"{config.WEBIM_API_URL.rstrip('/')}/api/bot/v2/{method}", json=payload, timeout=CALL_TIMEOUT,
                       headers={"Authorization": f"Token {config.WEBIM_TOKEN}"})
        data = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("webim %s: %s", method, e)
        return False
    if r.status_code != 200 or not isinstance(data, dict) or data.get("error"):
        log.warning("webim %s: %s %s", method, r.status_code, str(data)[:300])
        return False
    return True
