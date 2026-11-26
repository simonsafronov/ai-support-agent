"""Карточка обращения для менеджера.

Собирается кодом из того, что диалог уже знает (память о клиенте, показанные варианты, расчет, КП),
в момент открытия экрана оператора: модель для этого не вызывается, а все, что клиент дописал после
передачи, попадает в карточку само. Менеджеру не нужно читать переписку, чтобы начать работу.
"""
from __future__ import annotations

import json
import re
import time

from app import config, store

# Срочность по теме обращения: первая подошедшая строка. Сроки реакции в минутах задает заказчик.
PRIORITY = (
    ("high", r"госзакуп|44-?\s?фз|223-?\s?фз|тендер|конкурентн\w+ закуп|котировк|жалоб|претензи|дозвон|"
             r"статус\w* (?:заказ|запрос)|утер|возврат|не работает|уже оплати"),
    ("normal", r"скидк|спецуслов|спеццен|договор|эдо|по запросу|счет|счёт|\bкп\b|коммерческ"),
)
SLA_MINUTES = dict(zip(("high", "normal", "low"), (int(x) for x in config.SLA_MINUTES.split(","))))
PRIORITY_NAME = {"high": "высокая", "normal": "обычная", "low": "низкая"}
FLAGS = (
    ("просит скидку", r"скидк|спеццен|спецуслов|дешевле"),
    ("госзакупка", r"госзакуп|44-?\s?фз|223-?\s?фз|тендер|котировк|еис|березк"),
    ("цена по запросу", r"по запросу"),
    ("жалоба", r"жалоб|претензи|дозвон|безобраз|никто не отве"),
)
CLIENT_FIELDS = ("компания", "ИНН", "КПП", "тип организации", "контакт")
NEED_FIELDS = ("задача", "пользователей", "устройств", "ОС и инфраструктура", "срок лицензии", "бюджет", "скидка",
               "прочее")
TEMPLATES = (  # заготовки ответа: менеджер правит текст перед отправкой
    ("Уточню наличие", "Здравствуйте. Уточняю наличие и срок поставки у вендора, вернусь с ответом сегодня."),
    ("Подготовлю счет", "Здравствуйте. Черновик КП проверил, готовлю счет. Пришлите, пожалуйста, карточку "
                         "организации, если реквизиты еще не отправляли."),
    ("Перезвоню", "Здравствуйте. Вопрос проще обсудить голосом: перезвоню вам сегодня в рабочее время. "
                  "Если удобнее другое время, напишите его здесь."),
)


_PERCENT = re.compile(r"(?<![\d,.])(\d{1,2}(?:[.,]\d)?)\s?%")
SHOWN_AS = {"скидка": "дана скидка"}  # в карточке рядом стоят данная агентом и запрошенная клиентом скидка


def asked_discount(messages: list[str]) -> str:
    """Самый большой процент из слов клиента: менеджер видит, о какой скидке шла речь."""
    found = [float(x.replace(",", ".")) for text in messages if "ндс" not in text.lower()
             for x in _PERCENT.findall(text)]
    return f"{max(found):g}%" if found else ""


def priority(text: str) -> str:
    text = text.lower()
    return next((p for p, rx in PRIORITY if re.search(rx, text)), "low")


def build(h: dict, now: float | None = None) -> dict:
    """h это строка таблицы handoffs. Возвращает карточку: срочность, срок реакции, клиент, запрос, позиции, сумма."""
    now = now or time.time()
    sid = h["session_id"]
    s = store.get_session(sid)
    asked = [m["content"] for m in store.messages(sid) if m["role"] == "user"]
    text = " ".join([h["reason"] or "", h["summary"] or "", *asked])
    level = priority(text)
    due = h["ts"] + SLA_MINUTES[level] * 60
    facts, items = s["facts"], s["items"]
    quote = store.last_quote(sid)
    calc = store.last_total(sid)
    done = h["status"] in ("answered", "returned")
    shown = [{"id": o["id"], "name": o["name"], "price": o.get("price")} for o in items.get("offers", [])[:5]]
    in_quote = [{"id": o.get("offer_id"), "name": o["name"], "price": o.get("price"), "qty": o.get("qty")}
                for o in json.loads(quote["items_json"] or "[]")] if quote else []
    flags = [name for name, rx in FLAGS if re.search(rx, text.lower())]
    needs = {SHOWN_AS.get(k, k): facts[k] for k in NEED_FIELDS if facts.get(k)}
    if "просит скидку" in flags and asked_discount(asked):
        needs["запрошена скидка"] = asked_discount(asked)
    return {
        "priority": level, "priority_name": PRIORITY_NAME[level], "sla_minutes": SLA_MINUTES[level],
        "sla_due": due, "overdue": not done and now > due,
        "flags": flags,
        "client": {k: facts[k] for k in CLIENT_FIELDS if facts.get(k)},
        "needs": needs,
        "offers": in_quote or shown,  # есть КП: позиции из него, иначе последние показанные клиенту
        "quote": {"number": quote["number"], "total": quote["total"], "customer": quote["customer"],
                  "url": f"/quotes/{quote['number']}.pdf"} if quote else None,
        "calc_total": calc,
        "turns": len(asked), "last_message": asked[-1][:300] if asked else "",
        "assignee": h.get("assignee") or "", "taken_at": h.get("taken_at"),
        "templates": [{"title": t, "text": body} for t, body in TEMPLATES],
    }
