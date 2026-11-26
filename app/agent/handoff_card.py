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
from app.agent import spec
from app.catalog import search as catalog

# Срочность по теме обращения: первая подошедшая строка. Сроки реакции в минутах задает заказчик.
PRIORITY = (
    ("high", r"госзакуп|44-?\s?фз|223-?\s?фз|тендер|конкурентн\w+ закуп|котировк|жалоб|претензи|дозвон|"
             r"статус\w* (?:заказ|запрос)|утер|возврат|не работает|уже оплати"),
    ("normal", r"скидк|отсрочк|постоплат|оплат\w* после|спецуслов|спеццен|договор|эдо|по запросу|счет|счёт|"
               r"\bкп\b|коммерческ|" + (
        ASKS_HUMAN := r"поз[ао]?в\w+ (?:менеджер|человек|оператор)|\b(?:хочу|нужен|дайте|можно) (?:с |говорить с |к )?"
                      r"(?:менеджер|человек|оператор)|не хочу с ботом|живо\w+ (?:человек|менеджер|оператор)") +
     r"|агент не смог ответить|сбой при ответе"),  # клиент ждет человека или остался без ответа агента
)
SLA_MINUTES = dict(zip(("high", "normal", "low"), (int(x) for x in config.SLA_MINUTES.split(","))))
PRIORITY_NAME = {"high": "высокая", "normal": "обычная", "low": "низкая"}
FLAGS = (
    ("просит скидку", r"скидк|спеццен|спецуслов|дешевле"),
    ("просит отсрочку", r"отсрочк|постоплат|оплат\w* после"),  # срочность как у скидки: тоже условия оплаты
    ("госзакупка", r"госзакуп|44-?\s?фз|223-?\s?фз|тендер|котировк|еис|березк"),
    ("цена по запросу", r"по запросу"),
    ("жалоба", r"жалоб|претензи|дозвон|безобраз|никто не отве"),
    ("просит человека", ASKS_HUMAN),
)
CLIENT_FIELDS = ("компания", "ИНН", "КПП", "тип организации", "контакт")
NEED_FIELDS = ("задача", "пользователей", "устройств", "ОС и инфраструктура", "срок лицензии", "бюджет", "скидка",
               "прочее")
TEMPLATES = (  # заготовки ответа: менеджер правит текст перед отправкой. Сумм и цен в них нет: их называет КП
    ("Уточню наличие", "Здравствуйте. Уточняю наличие и срок поставки у вендора, вернусь с ответом сегодня."),
    ("Подготовлю счет", "Здравствуйте. Черновик КП проверил, готовлю счет. Пришлите, пожалуйста, карточку "
                         "организации, если реквизиты еще не отправляли."),
    ("Перезвоню", "Здравствуйте. Вопрос проще обсудить голосом: перезвоню вам сегодня в рабочее время. "
                  "Если удобнее другое время, напишите его здесь."),
)
# По пометке обращения: следующий шаг менеджера и заготовка ответа. Шаг берется по первой подошедшей строке,
# заготовки подошедших строк встают перед общими в том же порядке.
BY_FLAG = (
    ("жалоба", "Позвонить клиенту сегодня",
     ("Позвоню сегодня", "Здравствуйте. Жаль, что так вышло. Разбираюсь с вашим вопросом и позвоню вам сегодня "
                         "в рабочее время. Если удобнее другое время, напишите его здесь.")),
    ("госзакупка", "Запросить ТЗ и условия закупки, проверить срок подачи заявок",
     ("Условия закупки", "Здравствуйте. Чтобы подготовить предложение для закупки, пришлите, пожалуйста, "
                         "техническое задание или номер закупки и срок подачи заявок.")),
    ("просит скидку", "Согласовать скидку{asked} с вендором или подтвердить {given}%",
     ("Скидка", "Здравствуйте. Запрос на скидку отправил вендору на согласование, вернусь с ответом сегодня.")),
    ("просит отсрочку", "Согласовать отсрочку платежа",
     ("Отсрочка", "Здравствуйте. Уточняю, возможна ли отсрочка платежа, и вернусь с ответом сегодня.")),
    ("цена по запросу", "Запросить цену у вендора",
     ("Цена у вендора", "Здравствуйте. Запросил цену у вендора, вернусь с ответом сегодня.")),
    ("просит человека", "Ответить клиенту в чате", None),
)


def next_step(flags: list[str], asked: str, has_quote: bool) -> str:
    """Следующий шаг менеджера по пометкам обращения: первая подошедшая строка BY_FLAG."""
    for flag, step, _ in BY_FLAG:
        if flag in flags:
            return step.format(asked=f" {asked}" if asked else "", given=f"{config.AGENT_DISCOUNT_MAX:g}")
    return "Проверить черновик КП и подготовить счет" if has_quote else "Ответить клиенту в чате"


def templates(flags: list[str]) -> list[dict]:
    """Заготовки ответа: сначала по пометкам обращения, затем общие."""
    own = [t for flag, _, t in BY_FLAG if t and flag in flags]
    return [{"title": title, "text": body} for title, body in own + list(TEMPLATES)]


_AMOUNT = re.compile(r"(?:,\s*)?(?:(?:итого|всего|на сумму|сумма)\s*:?\s*)?"
                     r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(?:руб(?:лей|ля|\.)?|₽)", re.I)


def _amount(m: re.Match) -> float:
    return round(float(re.sub(r"\D", "", m.group(1)) + "." + (m.group(2) or "0")), 2)


def agreed_summary(summary: str, quote: dict | None, items: list[dict], discount: float) -> str:
    """Сводка модели, согласованная с последним КП.

    КП мог собраться после передачи в том же ходе (клиент попросил скидку): тогда суммы в сводке модели уже
    не те, что в КП. Суммы, которых нет в КП, из сводки убираются, итог КП дописывает код.
    """
    if not quote or not summary:
        return summary or ""
    total = round(float(quote["total"]), 2)
    known = {total} | {round(float(o[k]), 2) for o in items for k in ("price", "sum") if o.get(k)}

    def drop(m):
        if _amount(m) in known:
            return m.group(0)
        tail = m.string[m.end():]  # сумма стояла в конце фразы: точка остается
        return "." if m.group(0).endswith(".") and (not tail.strip() or re.match(r"\s+[А-ЯЁA-Z]", tail)) else ""

    text = _AMOUNT.sub(drop, summary).strip()
    if total not in {_amount(m) for m in _AMOUNT.finditer(text)}:
        rub = f"{total:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")
        text += f" Последний черновик {quote['number']}: итого {rub} руб." + (
            f" со скидкой {discount:g}%." if discount else "")
    return text


_PERCENT = re.compile(r"(?<![\d,.])(\d{1,2}(?:[.,]\d)?)\s?%")
SHOWN_AS = {"скидка": "дана скидка"}  # в карточке рядом стоят данная агентом и запрошенная клиентом скидка


def asked_discount(messages: list[str]) -> str:
    """Самый большой процент из слов клиента: менеджер видит, о какой скидке шла речь."""
    found = [float(x.replace(",", ".")) for text in messages if "ндс" not in text.lower()
             for x in _PERCENT.findall(text)]
    return f"{max(found):g}%" if found else ""


def given_discount(value) -> str:
    """Скидка, которую дал агент: в карточке не больше его полномочий, что бы ни лежало в памяти диалога."""
    found = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    if not found:
        return ""
    return f"{min(float(found.group().replace(',', '.')), config.AGENT_DISCOUNT_MAX):g}%"


def priority(text: str) -> str:
    text = text.lower()
    return next((p for p, rx in PRIORITY if re.search(rx, text)), "low")


def _about(h: dict) -> tuple[list[str], str]:
    """Реплики клиента и весь текст обращения (причина, сводка, реплики): по нему срочность и пометки."""
    asked = [m["content"] for m in store.messages(h["session_id"]) if m["role"] == "user"]
    return asked, " ".join([h["reason"] or "", h["summary"] or "", *asked])


def sla_minutes(h: dict) -> int:
    """Срок реакции менеджера на обращение h (строка таблицы handoffs): тот же, что в его карточке."""
    return SLA_MINUTES[priority(_about(h)[1])]


def _list_price(item: dict, discount: float) -> dict:
    """Цена позиции КП со скидкой рядом с ценой каталога: менеджер видит, откуда расхождение с каталогом."""
    if not discount or not item.get("price") or not item.get("offer_id"):
        return {}
    listed = catalog.get_offer(item["offer_id"]).get("price")
    return {"list_price": listed, "discount_percent": discount} if listed and listed > item["price"] else {}


REST = (spec.ABSENT, spec.CHECK, spec.ON_REQUEST, spec.SKIPPED)  # строки списка, которые решает менеджер


def spec_rest(session_id: str, quote_items: list[dict]) -> dict:
    """Строки последнего списка позиций клиента, которых нет в КП: по статусам, с количествами.

    Строка "проверьте", вариант которой клиент подтвердил и агент внес в КП, менеджеру уже не нужна.
    """
    step = next((st for st in reversed(store.steps(session_id))
                 if st["kind"] == "tool" and st["name"] == spec.TOOL), None)
    if step is None:
        return {}
    asked, in_quote, rest = step["input"].get("items") or [], {o.get("offer_id") for o in quote_items}, {}
    for ln in step["output"].get("lines") or []:
        if ln["status"] not in REST or (ln.get("offer_id") and ln["offer_id"] in in_quote):
            continue
        query = asked[ln["n"] - 1]["query"] if ln["n"] <= len(asked) else ln["line"]
        code = ln.get("vendor_code") or ""
        rest.setdefault(ln["status"], []).append(
            query + (f" ({code})" if code and code.lower() not in query.lower() else "")
            + (", количество не указано" if ln.get("qty_assumed") else f", {ln['qty']} шт."))
    return {status: "; ".join(rest[status]) for status in REST if status in rest}


def build(h: dict, now: float | None = None) -> dict:
    """h это строка таблицы handoffs. Возвращает карточку: срочность, срок реакции, клиент, запрос, позиции, сумма."""
    now = now or time.time()
    sid = h["session_id"]
    s = store.get_session(sid)
    asked, text = _about(h)
    level = priority(text)
    due = h["ts"] + SLA_MINUTES[level] * 60
    facts, items = s["facts"], s["items"]
    quote = store.last_quote(sid)
    calc = store.last_total(sid)
    done = h["status"] in ("answered", "returned")
    shown = [{"id": o["id"], "name": o["name"], "price": o.get("price")} for o in items.get("offers", [])[:5]]
    quote_items = json.loads(quote["items_json"] or "[]") if quote else []
    discount = store.quote_discount(sid) if quote else 0.0
    in_quote = [{"id": o.get("offer_id"), "name": o["name"], "price": o.get("price"), "qty": o.get("qty"),
                 **_list_price(o, discount)} for o in quote_items]
    flags = [name for name, rx in FLAGS if re.search(rx, text.lower())]
    needs = {SHOWN_AS.get(k, k): facts[k] for k in NEED_FIELDS if facts.get(k)}
    if "дана скидка" in needs:  # зачем: запись "дана скидка 30%" в карточке менеджер прочтет как обещание клиенту
        needs["дана скидка"] = given_discount(needs["дана скидка"])
        if not needs["дана скидка"]:
            del needs["дана скидка"]
    if "просит скидку" in flags and asked_discount(asked):
        needs["запрошена скидка"] = asked_discount(asked)
    needs.update(spec_rest(sid, quote_items))  # клиент прислал список: что из него не вошло в КП
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
        "summary": agreed_summary(h["summary"], quote, quote_items, discount),
        "next": "" if done else next_step(flags, needs.get("запрошена скидка", ""), bool(quote)),
        "templates": templates(flags),
    }
