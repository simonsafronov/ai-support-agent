"""Обращение менеджеру в Telegram и ответ менеджера из Telegram в чат клиента.

Заказчик на брифе назвал плюсом "любой удобный вариант передачи вопроса из чата менеджеру и обратно". Карточка
обращения приходит в чат менеджеров в Telegram через несколько секунд после передачи, вместе с черновиком КП,
если он есть. Менеджер отвечает на карточку реплаем: клиент видит ответ в чате сайта. Кнопками под карточкой
обращение берут в работу или возвращают агенту.

Карточка та же, что на экране оператора: ее собирает код (app/agent/handoff_card.py), модель не вызывается.
Пока диалог идет, карточка в Telegram обновляется: новые позиции, сумма и КП попадают в то же сообщение.

Включается двумя настройками: TG_TOKEN (ключ бота) и TG_CHAT (номер чата менеджеров). Без них сервер
работает как раньше, обращения видны на экране оператора.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time

import httpx

from app import config, store
from app.agent import handoff_card

log = logging.getLogger("notify")
API = "https://api.telegram.org"
SETTLE, FRESH, WATCH = 3.0, 900.0, 3600.0  # секунд: дать ходу закончиться; не слать старое; сколько обновлять карточку
EVERY, LONG_POLL = 2.0, 25
STATUS = {"new": "ждет ответа", "taken": "в работе", "answered": "ответ дан", "returned": "возвращено агенту"}
LIMIT, MAX_OFFERS = 3900, 12  # знаков в карточке (Telegram берет до 4096), позиций в ней
HOWTO = "Ответьте на это сообщение: клиент увидит ответ в чате сайта."
HELLO = ("Сюда приходят обращения из чата магазина. Ответьте на карточку обращения реплаем, и клиент увидит "
         "ответ в чате сайта.")

_answer = None  # как записать ответ менеджера: задает сервер при запуске
_SCHEMA = ("CREATE TABLE IF NOT EXISTS tg_cards(handoff_id INTEGER PRIMARY KEY, chat_id TEXT, message_id INT, "
           "text TEXT, quote TEXT DEFAULT '', ts REAL)")


def enabled() -> bool:
    return bool(config.TG_TOKEN and config.TG_CHAT)


def _db():
    con = store.db()
    con.execute(_SCHEMA)
    return con


def call(method: str, files: dict | None = None, refusal: bool = False, **params):
    """Вызов Telegram Bot API. Возвращает result или None: сбой Telegram не должен ронять чат магазина.

    С refusal=True отказ Telegram (связь есть, запрос не принят) возвращается как False, а не None.
    """
    try:
        url = f"{API}/bot{config.TG_TOKEN}/{method}"
        timeout = LONG_POLL + 10 if method == "getUpdates" else 15
        r = httpx.post(url, data=params, files=files, timeout=timeout) if files else \
            httpx.post(url, json=params, timeout=timeout)
        body = r.json()
        if not body.get("ok"):
            log.warning("telegram %s: %s", method, body.get("description"))
            return False if refusal else None
        return body["result"]
    except (httpx.HTTPError, ValueError) as e:
        log.warning("telegram %s: %s", method, type(e).__name__)
        return None


def _rub(x) -> str:
    return f"{float(x):,.2f}".replace(",", " ").replace(".", ",") + " руб."


def _cut(text, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def card_text(h: dict, card: dict) -> str:
    """Текст карточки для Telegram (HTML): то же, что видит менеджер на экране оператора.

    Длинные поля укорачиваются до разметки, поэтому теги всегда целые, а карточка не длиннее LIMIT знаков.
    """
    e = lambda text, n=300: html.escape(_cut(text, n))
    head = f"<b>Обращение №{h['id']}</b>: {STATUS.get(h['status'], h['status'])}"
    if h.get("assignee"):
        head += f", {e(h['assignee'], 60)}"
    lines = [head, f"Срочность {card['priority_name']}, срок реакции {card['sla_minutes']} мин"]
    if card["flags"]:
        lines.append("Пометки: " + e(", ".join(card["flags"])))
    lines.append("")
    client = ", ".join(f"{k} {v}" if k in ("ИНН", "КПП") else str(v) for k, v in card["client"].items()
                       if k != "контакт")
    if client:
        lines.append(f"<b>Клиент:</b> {e(client)}")
    contact = h.get("contact") or card["client"].get("контакт")
    if contact:
        lines.append(f"<b>Контакт:</b> {e(contact)}")
    lines.append(f"<b>Причина:</b> {e(h['reason'] or '')}")
    if h.get("summary"):
        lines.append(f"<b>Запрос:</b> {e(h['summary'], 1000)}")
    for k, v in card["needs"].items():
        lines.append(f"{e(k[:1].upper() + k[1:], 60)}: {e(v, 200)}")
    if card["offers"]:
        lines.append("<b>Позиции:</b>")
        for o in card["offers"][:MAX_OFFERS]:
            qty = f", {o['qty']} шт." if o.get("qty") else ""
            price = f", {_rub(o['price'])}" if o.get("price") else ", цена по запросу"
            lines.append(f"- {e(o['name'], 150)}{qty}{price}")
        if len(card["offers"]) > MAX_OFFERS:
            lines.append(f"и еще {len(card['offers']) - MAX_OFFERS}")
    if card["quote"]:
        lines.append(f"<b>Черновик {e(card['quote']['number'], 60)}:</b> {_rub(card['quote']['total'])}")
    elif card["calc_total"]:
        lines.append(f"<b>Сумма по расчету:</b> {_rub(card['calc_total'])}")
    if card["last_message"]:
        lines.append(f"<b>Последнее сообщение:</b> {e(card['last_message'], 800)}")
    tail = ["", HOWTO]
    while len("\n".join(lines + tail)) > LIMIT:  # строки целые: каждая закрывает свои теги сама
        lines.pop()
        tail = ["…полностью на экране оператора", "", HOWTO]
    return "\n".join(lines + tail)


def plain(text: str) -> str:
    """Карточка без разметки: теги убраны, сущности раскодированы."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _card_call(method: str, text: str, **params):
    """Карточка с разметкой. Если Telegram ее не принял, тот же текст один раз уходит без разметки."""
    res = call(method, refusal=True, text=text, parse_mode="HTML", **params)
    if res is False:
        res = call(method, text=plain(text), **params)
    return res


def _buttons(h: dict) -> dict:
    rows = []
    if h["status"] == "new":
        rows.append([{"text": "Взять в работу", "callback_data": f"take:{h['id']}"}])
    if h["status"] in ("new", "taken"):
        rows.append([{"text": "Вернуть агенту", "callback_data": f"back:{h['id']}"}])
    if config.PUBLIC_URL:
        rows.append([{"text": "Экран оператора", "url": config.PUBLIC_URL.rstrip("/") + "/admin"}])
    return {"inline_keyboard": rows}


def _handoff(handoff_id: int) -> dict | None:
    row = _db().execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    return dict(row) if row else None


def _quote_mark(card: dict) -> str:
    """Номер и сумма КП: по ним видно, что менеджеру нужно прислать новый файл."""
    return f"{card['quote']['number']}:{card['quote']['total']}" if card["quote"] else ""


def _send_quote(h: dict, card: dict, message_id: int) -> str:
    """Черновик КП файлом в ответ на карточку. Возвращает отметку отправленного КП или пустую строку."""
    if not card["quote"]:
        return ""
    number = card["quote"]["number"]
    path = config.QUOTES_DIR / f"{number}.pdf"
    if not path.is_file():
        return ""
    with path.open("rb") as f:
        ok = call("sendDocument", files={"document": (path.name, f, "application/pdf")}, chat_id=config.TG_CHAT,
                  reply_to_message_id=message_id, caption=f"Черновик КП {number} к обращению №{h['id']}")
    return _quote_mark(card) if ok else ""


def sync() -> int:
    """Отправляет карточки новых обращений и обновляет отправленные, если карточка изменилась."""
    con, now, done = _db(), time.time(), 0
    fresh = con.execute(  # диалоги проверочного прогона менеджеру не шлем: это не посетители
        f"SELECT * FROM handoffs WHERE ts BETWEEN ? AND ? AND id NOT IN (SELECT handoff_id FROM tg_cards) "
        f"AND session_id NOT IN (SELECT id FROM sessions WHERE {store.RUN_CLIENTS}) ORDER BY id",
        (now - FRESH, now - SETTLE)).fetchall()
    for h in map(dict, fresh):
        card = handoff_card.build(h, now)
        text = card_text(h, card)
        sent = _card_call("sendMessage", text, chat_id=config.TG_CHAT, reply_markup=_buttons(h))
        if sent is None:
            continue  # Telegram не ответил: попробуем на следующем круге, пока обращение свежее
        con.execute("INSERT OR REPLACE INTO tg_cards(handoff_id, chat_id, message_id, text, quote, ts) "
                    "VALUES(?,?,?,?,?,?)", (h["id"], str(config.TG_CHAT), sent["message_id"], text,
                                            _send_quote(h, card, sent["message_id"]), now))
        con.commit()
        done += 1
    shown = con.execute("SELECT t.*, h.session_id FROM tg_cards t JOIN handoffs h ON h.id = t.handoff_id "
                        "WHERE t.ts > ?", (now - WATCH,)).fetchall()
    for t in shown:
        h = _handoff(t["handoff_id"])
        card = handoff_card.build(h, now)
        text, quote = card_text(h, card), t["quote"]
        if text != t["text"]:
            _card_call("editMessageText", text, chat_id=t["chat_id"], message_id=t["message_id"],
                       reply_markup=_buttons(h))
            done += 1
        if card["quote"] and _quote_mark(card) != quote:
            quote = _send_quote(h, card, t["message_id"]) or quote
        if text != t["text"] or quote != t["quote"]:
            con.execute("UPDATE tg_cards SET text=?, quote=? WHERE handoff_id=?", (text, quote, t["handoff_id"]))
            con.commit()
    return done


def _who(user: dict) -> str:
    return (" ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "менеджер")[:60]


def handle(update: dict) -> str:
    """Одно событие из Telegram: ответ менеджера реплаем на карточку или кнопка под ней. Возвращает, что сделано."""
    cb = update.get("callback_query")
    if cb:
        chat = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        action, _, hid = (cb.get("data") or "").partition(":")
        if chat != str(config.TG_CHAT) or not hid.isdigit():
            return "ignored"
        ok = store.take_handoff(int(hid), _who(cb["from"])) if action == "take" else \
            store.return_handoff(int(hid)) if action == "back" else False
        said = {"take": "Обращение за вами", "back": "Диалог возвращен агенту"}.get(action, "") if ok else \
            "Обращение уже занято или закрыто"
        call("answerCallbackQuery", callback_query_id=cb["id"], text=said)
        return action if ok else "stale"
    msg = update.get("message") or {}
    chat, text = str(msg.get("chat", {}).get("id", "")), (msg.get("text") or "").strip()
    if not chat or not text:
        return "ignored"
    if chat != str(config.TG_CHAT):  # чужой чат обращений не видит и клиентам не отвечает
        call("sendMessage", chat_id=chat, text=f"Этот чат не подключен к обращениям магазина. Номер чата: {chat}")
        return "foreign"
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    row = _db().execute("SELECT handoff_id FROM tg_cards WHERE chat_id=? AND message_id=?",
                        (chat, reply_to)).fetchone() if reply_to else None
    if row is None:
        call("sendMessage", chat_id=chat, text=HELLO)
        return "hint"
    h = _handoff(row["handoff_id"])
    if h["status"] == "new":
        store.take_handoff(h["id"], _who(msg.get("from", {})))
    _answer(h["id"], text[:4000])
    call("sendMessage", chat_id=chat, reply_to_message_id=msg["message_id"],
         text=f"Ответ по обращению №{h['id']} отправлен клиенту в чат сайта.")
    return "answered"


def _watch():
    while True:
        try:
            sync()
        except Exception:  # одна сломанная карточка не останавливает остальные
            log.exception("notify sync")
        time.sleep(EVERY)


def _poll():
    offset = 0
    while True:
        updates = call("getUpdates", offset=offset, timeout=LONG_POLL, allowed_updates=["message", "callback_query"])
        if updates is None:
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception:
                log.exception("notify update")


def start(answer):
    """Запускает отправку карточек и прием ответов, если бот настроен.

    answer(handoff_id, text) записывает ответ менеджера так же, как экран оператора: функцию дает сервер.
    """
    global _answer
    _answer = answer
    if not enabled():
        return
    for target in (_watch, _poll):
        threading.Thread(target=target, daemon=True, name="notify" + target.__name__).start()
