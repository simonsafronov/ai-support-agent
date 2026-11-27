"""Обращение менеджеру в Telegram и ответ менеджера из Telegram в чат клиента.

Заказчик на брифе назвал плюсом "любой удобный вариант передачи вопроса из чата менеджеру и обратно". Карточка
обращения приходит в чат менеджеров в Telegram через несколько секунд после передачи, вместе с черновиком КП,
если он есть. Менеджер отвечает на карточку реплаем: клиент видит ответ в чате сайта. Кнопками под карточкой
обращение берут в работу или возвращают агенту.

Карточка та же, что на экране оператора: ее собирает код (app/agent/handoff_card.py), модель не вызывается.
Пока диалог идет, карточка в Telegram обновляется: новые позиции, сумма и КП попадают в то же сообщение.

В режиме "ответ проверяет менеджер" сюда же приходит черновик ответа агента с кнопками "Отправить" и "Не
отправлять". Текст менеджера реплаем на черновик уходит клиенту вместо черновика.

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
from app.quote import kp
from app.agent import handoff_card

log = logging.getLogger("notify")
API = "https://api.telegram.org"
SETTLE, FRESH, WATCH = 3.0, 900.0, 3600.0  # секунд: дать ходу закончиться; не слать старое; сколько обновлять карточку
RETRY = 60.0  # секунд до повтора карточки, которую Telegram не принял: сбой не превращается в поток запросов
EVERY, LONG_POLL = 2.0, 25
STATUS = {"new": "ждет ответа", "taken": "в работе", "answered": "ответ дан", "returned": "возвращено агенту"}
LIMIT, MAX_OFFERS = 3900, 12  # знаков в карточке (Telegram берет до 4096), позиций в ней
HOWTO = "Ответьте на это сообщение: клиент увидит ответ в чате сайта."
HELLO = ("Сюда приходят обращения из чата магазина. Ответьте на карточку обращения реплаем, и клиент увидит "
         "ответ в чате сайта.")

DRAFT_HOWTO = "Ответьте на это сообщение своим текстом: клиент получит его вместо черновика."
DRAFT_STATUS = {"new": "ждет проверки", "sent": "отправлен клиенту без правок",
                "edited": "клиенту ушел текст менеджера", "dropped": "не отправлен, обращение у менеджера",
                "stale": "устарел, клиент написал снова"}

_invoice_state = _invoice_action = None  # supplied by the server; no model or Telegram document delivery
_answer = None  # как записать ответ менеджера: задает сервер при запуске
_review = None  # как принять решение менеджера по черновику ответа агента: задает сервер (review_with)
_SCHEMA = ("CREATE TABLE IF NOT EXISTS tg_cards(handoff_id INTEGER PRIMARY KEY, chat_id TEXT, message_id INT, "
           "text TEXT, quote TEXT DEFAULT '', ts REAL)",
           "CREATE TABLE IF NOT EXISTS tg_drafts(draft_id INTEGER PRIMARY KEY, chat_id TEXT, message_id INT, "
           "status TEXT, ts REAL)")
# номер обращения или "draft:<номер черновика>" -> когда снова пробовать отправить или обновить сообщение
_retry_at: dict[int | str, float] = {}


def enabled() -> bool:
    return bool(config.TG_TOKEN and config.TG_CHAT)


def _db():
    con = store.db()
    for sql in _SCHEMA:
        con.execute(sql)
    return con


def call(method: str, files: dict | None = None, refusal: bool = False, **params):
    """Вызов Telegram Bot API. Возвращает result или None: сбой Telegram не должен ронять чат магазина.

    С refusal=True отказ Telegram (связь есть, запрос не принят) возвращается как False, а не None.
    Отказ правки "message is not modified" это успех: в Telegram уже этот текст, возвращается True.
    """
    try:
        url = f"{API}/bot{config.TG_TOKEN}/{method}"
        timeout = LONG_POLL + 10 if method == "getUpdates" else 15
        r = httpx.post(url, data=params, files=files, timeout=timeout) if files else \
            httpx.post(url, json=params, timeout=timeout)
        body = r.json()
        if not body.get("ok"):
            if "message is not modified" in str(body.get("description", "")):
                return True
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
    lines = [head, f"Срочность {e(card['priority_name'])} · срок реакции <b>{int(card['sla_minutes'])} мин</b>",
             f"<b>Причина:</b> {e(h['reason'] or '')}"]
    if card['summary']:
        lines += ["<b>Запрос покупателя</b>", f"<blockquote>{e(card['summary'], 700)}</blockquote>"]
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
    for k, v in card["needs"].items():
        lines.append(f"{e(k[:1].upper() + k[1:], 60)}: {e(v, 200)}")
    if card["offers"]:
        lines.append("<b>Позиции:</b>")
        for o in card["offers"][:MAX_OFFERS]:
            qty = f", {o['qty']} шт." if o.get("qty") else ""
            price = f", {_rub(o['price'])}" if o.get("price") else ", цена по запросу"
            if o.get("list_price"):
                price += f" (со скидкой {o['discount_percent']:g}%, по каталогу {_rub(o['list_price'])})"
            if o.get("available") is False:
                price += f" · {kp.FEED_UNAVAILABLE}"
            lines.append(f"- {e(o['name'], 150)}{qty}{price}")
        if len(card["offers"]) > MAX_OFFERS:
            lines.append(f"и еще {len(card['offers']) - MAX_OFFERS}")
    if card["quote"]:
        lines.append(f"<b>Черновик {e(card['quote']['number'], 60)}:</b> {_rub(card['quote']['total'])}")
    elif card["calc_total"]:
        lines.append(f"<b>Сумма по расчету:</b> {_rub(card['calc_total'])}")
    if card["next"]:
        lines.append(f"<b>Дальше:</b> {e(card['next'])}")
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


def _manager_card(h, now):
    state = _invoice_state(h['session_id']) if _invoice_state and h['status'] in ('new', 'taken') else None
    revision = state['revision'] if state and not state['sent'] else ''
    card = handoff_card.build(h, now)
    text = card_text(h, card)
    # Compare both text and keyboard on refresh, even for an equal-price quote revision.
    mark = text + ('\ninv:' + revision if revision else '')
    return card, text, mark, _buttons(h, revision)


def _buttons(h: dict, revision: str = '') -> dict:
    rows = []
    if h["status"] == "new":
        rows.append([{"text": "Взять в работу", "callback_data": f"take:{h['id']}"}])
    if h["status"] in ("new", "taken"):
        rows.append([{"text": "Вернуть агенту", "callback_data": f"back:{h['id']}"}])
    if revision:
        rows.append([{"text": "Выдать образец вручную (не для оплаты)", "callback_data": f"inv:{h['id']}:{revision}"}])
    if config.PUBLIC_URL:
        base = config.PUBLIC_URL.rstrip('/')
        managers = {v.strip() for v in (config.env('TG_MANAGER_IDS', '') or '').split(',')}
        chat = str(config.TG_CHAT)
        if base.startswith('https://') and chat.isdecimal() and chat in managers:
            rows.append([{'text': 'Открыть обращение в Mini App', 'web_app': {'url': f"{base}/tgdesk?handoff={h['id']}"}}])
        else:
            rows.append([{"text": "Экран оператора", "url": base + "/admin"}])
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


def _since(con, now: float) -> float:
    """Когда доставка в Telegram впервые заработала на этой базе: заявки задолго до этого менеджеру не шлем."""
    row = con.execute("SELECT value FROM settings WHERE key='tg_delivery_since'").fetchone()
    if row is None:
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('tg_delivery_since', ?)", (str(now),))
        con.commit()
        row = con.execute("SELECT value FROM settings WHERE key='tg_delivery_since'").fetchone()
    return float(row[0])


def _later(key: int | str, now: float) -> bool:
    return _retry_at.get(key, 0) > now


def sync() -> int:
    """Отправляет карточки открытых обращений и обновляет отправленные, если карточка изменилась.

    Неотправленная открытая заявка уходит, как только Telegram снова доступен, сколько бы ни длился сбой.
    Старые заявки, лежавшие в базе до первого запуска доставки, не шлются.
    """
    con, now, done = _db(), time.time(), 0
    unsent = con.execute(  # диалоги проверочного прогона менеджеру не шлем: это не посетители
        f"SELECT * FROM handoffs WHERE status IN ('new', 'taken') AND ts BETWEEN ? AND ? "
        f"AND id NOT IN (SELECT handoff_id FROM tg_cards) "
        f"AND session_id NOT IN (SELECT id FROM sessions WHERE {store.RUN_CLIENTS}) ORDER BY id",
        (_since(con, now) - FRESH, now - SETTLE)).fetchall()
    for h in map(dict, unsent):
        if _later(h["id"], now):
            continue
        card, text, mark, buttons = _manager_card(h, now)
        sent = _card_call("sendMessage", text, chat_id=config.TG_CHAT, reply_markup=buttons)
        if sent is None:
            _retry_at[h["id"]] = now + RETRY  # Telegram не принял: повтор на одном из следующих кругов
            continue
        _retry_at.pop(h["id"], None)
        con.execute("INSERT OR REPLACE INTO tg_cards(handoff_id, chat_id, message_id, text, quote, ts) "
                    "VALUES(?,?,?,?,?,?)", (h["id"], str(config.TG_CHAT), sent["message_id"], mark,
                                            _send_quote(h, card, sent["message_id"]), now))
        con.commit()
        done += 1
    shown = con.execute("SELECT t.*, h.session_id FROM tg_cards t JOIN handoffs h ON h.id = t.handoff_id "
                        "WHERE t.ts > ? AND t.chat_id=?", (now - WATCH, str(config.TG_CHAT))).fetchall()
    for t in shown:
        h = _handoff(t["handoff_id"])
        card, text, mark, buttons = _manager_card(h, now)
        quote = t["quote"]
        if mark != t["text"] and _later(h["id"], now):
            mark = t["text"]
        elif mark != t["text"]:
            if _card_call("editMessageText", text, chat_id=t["chat_id"], message_id=t["message_id"],
                          reply_markup=buttons) is None:
                _retry_at[h["id"]], mark = now + RETRY, t["text"]
            else:
                _retry_at.pop(h["id"], None)
                done += 1
        if card["quote"] and _quote_mark(card) != quote:
            quote = _send_quote(h, card, t["message_id"]) or quote
        if mark != t["text"] or quote != t["quote"]:
            con.execute("UPDATE tg_cards SET text=?, quote=? WHERE handoff_id=?", (mark, quote, t["handoff_id"]))
            con.commit()
    return done + _sync_drafts(con, now)


def _card_price(c: dict) -> str:
    if c.get("price"):
        return _rub(c["price"]) + (f" · {kp.FEED_UNAVAILABLE}" if c.get("available") is False else "")
    if c.get("price_min"):
        return ("" if c["price_min"] == c.get("price_max") else "от ") + _rub(c["price_min"])
    return "цена по запросу"


def draft_text(d: dict) -> str:
    """Черновик ответа агента для Telegram (HTML): реплика клиента, ответ агента и все, что клиент получит с ним."""
    e = lambda text, n=300: html.escape(_cut(text, n))
    meta = d["meta"]
    lines = [f"<b>Черновик ответа агента №{d['id']}</b>: {DRAFT_STATUS[d['status']]}"]
    if d["status"] == "new":
        lines.append(f"Клиент ждет в чате сайта, срок проверки {d['minutes']} мин")
    lines += ["", f"<b>Клиент:</b> {e(d['asked'] or '', 800)}", "", "<b>Ответ агента:</b>", e(d["content"], 2500)]
    cards = meta.get("cards") or []
    if cards:
        lines.append("<b>Карточки под ответом:</b>")
        lines += [f"- {e(c['title'], 150)}, {_card_price(c)}" for c in cards[:MAX_OFFERS]]
    if meta.get("choices"):
        lines.append("<b>Кнопки выбора:</b> " + e("; ".join(meta["choices"])))
    for ev in meta.get("events") or []:
        if ev.get("type") == "quote":
            lines.append(f"<b>Черновик {e(ev['number'], 60)}:</b> {_rub(ev['total'])}")
        elif ev.get("type") == "handoff":
            lines.append(f"<b>Передача менеджеру:</b> обращение №{ev['id']}")
    tail = howto = ["", DRAFT_HOWTO] if d["status"] == "new" else []
    while len("\n".join(lines + tail)) > LIMIT:  # строки целые: каждая закрывает свои теги сама
        lines.pop()
        tail = ["…полностью на экране оператора", *howto]
    return "\n".join(lines + tail)


def _draft_buttons(d: dict) -> dict:
    rows = [[{"text": "Отправить", "callback_data": f"send:{d['id']}"},
             {"text": "Не отправлять", "callback_data": f"drop:{d['id']}"}]] if d["status"] == "new" else []
    if config.PUBLIC_URL:
        rows.append([{"text": "Экран оператора", "url": config.PUBLIC_URL.rstrip("/") + "/admin"}])
    return {"inline_keyboard": rows}


def _sync_drafts(con, now: float) -> int:
    """Черновики ответов агента: новый уходит с кнопками, разобранный или устаревший теряет их и показывает итог."""
    done = 0
    unsent = store.drafts(  # черновики до первого запуска доставки и диалоги проверочного прогона не шлем
        f"d.status='new' AND d.ts>=? AND d.id NOT IN (SELECT draft_id FROM tg_drafts) "
        f"AND d.session_id NOT IN (SELECT id FROM sessions WHERE {store.RUN_CLIENTS})", (_since(con, now) - FRESH,))
    for d in unsent:
        key = f"draft:{d['id']}"
        if _later(key, now):
            continue
        sent = _card_call("sendMessage", draft_text(d), chat_id=config.TG_CHAT, reply_markup=_draft_buttons(d))
        if sent is None:
            _retry_at[key] = now + RETRY
            continue
        _retry_at.pop(key, None)
        con.execute("INSERT OR REPLACE INTO tg_drafts(draft_id, chat_id, message_id, status, ts) VALUES(?,?,?,?,?)",
                    (d["id"], str(config.TG_CHAT), sent["message_id"], d["status"], now))
        con.commit()
        done += 1
    changed = con.execute("SELECT t.* FROM tg_drafts t JOIN drafts d ON d.id = t.draft_id "
                          "WHERE t.status <> d.status AND t.ts > ?", (now - WATCH,)).fetchall()
    for t in changed:
        d, key = store.draft(t["draft_id"]), f"draft:{t['draft_id']}"
        if _later(key, now):
            continue
        if _card_call("editMessageText", draft_text(d), chat_id=t["chat_id"], message_id=t["message_id"],
                      reply_markup=_draft_buttons(d)) is None:
            _retry_at[key] = now + RETRY
            continue
        _retry_at.pop(key, None)
        con.execute("UPDATE tg_drafts SET status=? WHERE draft_id=?", (d["status"], t["draft_id"]))
        con.commit()
        done += 1
    return done


def _who(user: dict) -> str:
    return (" ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "менеджер")[:60]


def invoice_with(state, approve):
    global _invoice_state, _invoice_action
    _invoice_state, _invoice_action = state, approve


def _invoice_click(cb, chat, data):
    match = re.fullmatch(r"([1-9][0-9]{0,18}):([a-f0-9]{24})", data)
    user = cb.get('from') or {}
    if not (_invoice_action and config.TG_CHAT and chat == str(config.TG_CHAT) and match and
            user.get('id') and not user.get('is_bot') and (chat.startswith('-') or str(user['id']) == chat)):
        return 'ignored'
    hid, rev = match.groups()
    row = _db().execute("SELECT t.text,h.session_id,h.status FROM tg_cards t JOIN handoffs h ON h.id=t.handoff_id "
                        "WHERE t.chat_id=? AND t.message_id=? AND t.handoff_id=?",
                        (chat, cb['message'].get('message_id'), hid)).fetchone()
    if not row:
        return 'ignored'
    if row['status'] not in ('new', 'taken') or not row['text'].endswith('\ninv:' + rev):
        result = {'ok': False, 'detail': 'Карточка устарела.'}
    else:
        result = _invoice_action(row['session_id'], rev, 'telegram:' + str(user['id']))
    call('answerCallbackQuery', callback_query_id=cb['id'], text=result['detail'][:180])
    return 'invoice' if result['ok'] else 'stale'


def handle(update: dict) -> str:
    """Одно событие из Telegram: ответ менеджера реплаем на карточку или кнопка под ней. Возвращает, что сделано."""
    cb = update.get("callback_query")
    if cb:
        chat = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        action, _, hid = (cb.get("data") or "").partition(":")
        if action == 'inv':
            return _invoice_click(cb, chat, hid)
        if chat != str(config.TG_CHAT) or not hid.isdigit():
            return "ignored"
        if action in ("send", "drop"):  # кнопки под черновиком ответа агента
            done = _review(int(hid), action)
            if done and done["ok"]:
                said = "Ответ отправлен клиенту" if action == "send" else "Черновик снят, обращение у менеджера"
            else:  # повтор нажатия или черновик устарел: клиенту ничего не уходит
                said = f"Черновик №{hid}: {DRAFT_STATUS[done['status']]}" if done else f"Черновика №{hid} больше нет"
            call("answerCallbackQuery", callback_query_id=cb["id"], text=said)
            return action if done and done["ok"] else "stale"
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
    drafted = _db().execute("SELECT draft_id FROM tg_drafts WHERE chat_id=? AND message_id=?",
                            (chat, reply_to)).fetchone() if reply_to and row is None else None
    if drafted:  # текст менеджера реплаем на черновик уходит клиенту вместо черновика
        n = drafted["draft_id"]
        done = _review(n, "edit", text[:4000])
        ok = bool(done and done["ok"])
        call("sendMessage", chat_id=chat, reply_to_message_id=msg["message_id"],
             text=f"Клиенту отправлен ваш текст вместо черновика №{n}." if ok else
             f"Черновик №{n}: {DRAFT_STATUS[done['status']]}. Ваш текст клиенту не ушел." if done else
             f"Черновика №{n} больше нет: ваш текст клиенту не ушел.")
        return "edited" if ok else "stale"
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


def review_with(review):
    """review(draft_id, action, text) принимает решение менеджера по черновику ответа агента (send, edit или drop)
    так же, как экран оператора: функцию дает сервер до запуска."""
    global _review
    _review = review


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
