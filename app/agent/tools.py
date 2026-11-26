"""Инструменты агента: описание для модели и исполнение.

Все факты о товарах, ценах и условиях магазина агент получает только отсюда.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app import store
from app.catalog import search as catalog
from app.kb import search as kb
from app.quote import kp


@dataclass
class ToolContext:
    session_id: str
    facts: dict = field(default_factory=dict)
    events: list = field(default_factory=list)  # что показать оператору и интерфейсу: КП, передача менеджеру


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _search_products(ctx, query: str, vendor: str = "", only_available: bool = False, russian_only: bool = False,
                     category_id: int | None = None, product_type: str = ""):
    return catalog.search_products(query, vendor=vendor or None, category_id=category_id,
                                   only_available=only_available, russian_only=russian_only,
                                   product_type=product_type or None, limit=6)


def _get_product(ctx, product_id: str, **filters):
    filters = {k: v for k, v in filters.items() if v not in (None, "", False) and (v != 0 or k == "term_months")}
    return catalog.get_product(str(product_id), filters=filters, limit=6)


def _browse_categories(ctx, query: str = "", parent_id: int | None = None):
    return catalog.browse_categories(query=query or None, parent_id=parent_id, limit=15)


def _find_analogs(ctx, product_id: str, russian_only: bool = True):
    return catalog.find_analogs(str(product_id), russian_only=russian_only, limit=6)


def _kb_search(ctx, query: str):
    res = kb.kb_search(query, limit=6)
    for r in res.get("results", []):
        r["snippet"] = r["snippet"][:700]
        r.pop("score", None)
    return res


def _note_facts(ctx, facts: dict):
    facts = {str(k): str(v) for k, v in (facts or {}).items() if v not in (None, "")}
    ctx.facts.update(facts)
    store.update_session(ctx.session_id, facts=ctx.facts)
    return {"saved": facts, "known": ctx.facts}


def _calc_total(ctx, items: list[dict]):
    """Сумма по позициям считается кодом: модель не умножает и не складывает сама."""
    lines, total, on_request = [], 0.0, []
    for it in items or []:
        o = catalog.get_offer(int(it["offer_id"]))
        if not o:
            return {"error": f"нет варианта с offer_id {it['offer_id']}"}
        qty = max(1, int(it.get("qty") or 1))
        price = o.get("price")
        line = {"offer_id": o["offer_id"], "name": o["name"], "qty": qty, "price": price,
                "sum": round(price * qty, 2) if price else None}
        if price:
            total += price * qty
        else:
            on_request.append(o["name"])
        lines.append(line)
    return {"lines": lines, "total": round(total, 2), "price_on_request": on_request,
            "note": "Цены из каталога, с НДС где он применим. Позиции без цены в сумму не вошли." if on_request
            else "Цены из каталога, с НДС где он применим."}


def _make_quote(ctx, customer: str, items: list[dict], inn: str = "", payment: str = "", contact: str = ""):
    res = kp.make_quote(ctx.session_id, customer=customer, items=items, inn=inn, payment=payment, contact=contact)
    if res.get("ok"):
        ctx.events.append({"type": "quote", **{k: res[k] for k in ("number", "url", "total")}})
    return res


def _handoff(ctx, reason: str, summary: str, contact: str = ""):
    hid = store.add_handoff(ctx.session_id, reason, summary, contact)
    ctx.events.append({"type": "handoff", "id": hid, "reason": reason})
    return {"ok": True, "ticket": hid,
            "note": "Обращение передано менеджеру. Скажите клиенту, что менеджер ответит в рабочее время, "
                    "и уточните контакт для связи, если его еще нет."}


TOOLS = [
    {"name": "search_products", "handler": _search_products,
     "description": "Поиск продуктов в каталоге магазина по названию, вендору или задаче. Возвращает продукты "
                    "(линейки) с числом вариантов лицензий, диапазоном цен, наличием и признаком реестра "
                    "отечественного ПО. Понимает опечатки и разговорные названия. Начинайте с него.",
     "parameters": _obj({
         "query": {"type": "string", "description": "что ищем: название продукта, вендор или тип ПО"},
         "vendor": {"type": "string", "description": "вендор, если клиент его назвал"},
         "only_available": {"type": "boolean", "description": "только то, что можно купить сейчас по цене из каталога"},
         "russian_only": {"type": "boolean", "description": "только российские вендоры и реестр отечественного ПО"},
         "category_id": {"type": "integer", "description": "искать внутри категории (id из browse_categories)"},
         "product_type": {"type": "string", "enum": ["софт", "электроника"]},
     }, ["query"])},
    {"name": "get_product", "handler": _get_product,
     "description": "Варианты лицензий одного продукта с ценами и артикулами. Фильтры сужают набор. В ответе "
                    "поле clarify: какие параметры стоит уточнить у клиента, и facets: какие значения бывают. "
                    "matching 0: такого сочетания в каталоге нет, смотрите facets. price_ambiguous: у артикула "
                    "несколько цен. Вызывайте после search_products и повторно, когда клиент ответил на уточнение.",
     "parameters": _obj({
         "product_id": {"type": "string", "description": "id продукта из search_products"},
         "users": {"type": "integer", "description": "сколько пользователей или рабочих мест"},
         "nodes": {"type": "integer", "description": "сколько узлов, устройств или серверов"},
         "qty": {"type": "integer", "description": "сколько лицензий покупают, если цена зависит от объема"},
         "term_months": {"type": "integer", "description": "срок лицензии в месяцах, 0 = бессрочная"},
         "license_type": {"type": "string", "description": "тип лицензии: новая, продление, подписка и т.п."},
         "org_type": {"type": "string", "description": "тип организации: коммерческая, государственная, образовательная"},
         "user_type": {"type": "string", "description": "физлицо или юрлицо"},
         "version": {"type": "string", "description": "версия или редакция продукта"},
         "platform": {"type": "string", "description": "платформа или ОС"},
         "text": {"type": "string", "description": "слово, которое должно быть в названии варианта"},
         "only_available": {"type": "boolean"},
     }, ["product_id"])},
    {"name": "browse_categories", "handler": _browse_categories,
     "description": "Категории каталога: поиск по названию (например, 'антивирус', 'резервное копирование') или "
                    "список подкатегорий. Используйте, когда клиент описывает задачу и не называет продукт.",
     "parameters": _obj({
         "query": {"type": "string", "description": "тип ПО или задача"},
         "parent_id": {"type": "integer", "description": "показать подкатегории этой категории"},
     })},
    {"name": "find_analogs", "handler": _find_analogs,
     "description": "Аналоги продукта из той же категории от других вендоров, по умолчанию только российских "
                    "(реестр отечественного ПО или российский вендор). Для вопросов 'есть ли российский аналог' "
                    "и когда продукта нет в продаже.",
     "parameters": _obj({
         "product_id": {"type": "string"},
         "russian_only": {"type": "boolean", "description": "true = только отечественное ПО"},
     }, ["product_id"])},
    {"name": "kb_search", "handler": _kb_search,
     "description": "База знаний магазина: доставка и получение лицензий, оплата, НДС, документы, возврат, "
                    "сроки, условия для юрлиц и физлиц, личный кабинет, акции. Отвечайте на такие вопросы только "
                    "по ней.",
     "parameters": _obj({"query": {"type": "string", "description": "тема существительными, как в справке: "
                                   "'доставка электронной лицензии', 'способы оплаты', 'закрывающие документы'"}},
                        ["query"])},
    {"name": "note_facts", "handler": _note_facts,
     "description": "Запомнить, что стало известно о клиенте и его задаче, чтобы не спрашивать повторно: "
                    "компания, ИНН, тип организации, число пользователей, устройства, ОС, бюджет, задача, контакт.",
     "parameters": _obj({"facts": {"type": "object", "description": "пары 'что': 'значение'"}}, ["facts"])},
    {"name": "calc_total", "handler": _calc_total,
     "description": "Посчитать стоимость: цена варианта на количество и итог по нескольким позициям. Любую сумму, "
                    "кроме цены из каталога, называйте клиенту только после этого инструмента.",
     "parameters": _obj({
         "items": {"type": "array", "description": "позиции расчета",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
     }, ["items"])},
    {"name": "make_quote", "handler": _make_quote,
     "description": "Сформировать черновик коммерческого предложения (PDF по шаблону Softline) для проверки "
                    "менеджером. Только после того, как клиент согласился с выбором и назвал количество и компанию.",
     "parameters": _obj({
         "customer": {"type": "string", "description": "наименование заказчика"},
         "items": {"type": "array", "description": "позиции КП",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
         "inn": {"type": "string"},
         "payment": {"type": "string", "description": "условия оплаты, если клиент назвал"},
         "contact": {"type": "string", "description": "почта или телефон клиента"},
     }, ["customer", "items"])},
    {"name": "handoff_to_manager", "handler": _handoff,
     "description": "Передать обращение менеджеру: цена по запросу, скидка, госзакупка, статус заказа, жалоба, "
                    "нестандартные условия, вопрос вне каталога и базы знаний. В summary кратко: кто клиент, "
                    "что нужно, что уже подобрано.",
     "parameters": _obj({
         "reason": {"type": "string", "description": "причина одной фразой"},
         "summary": {"type": "string"},
         "contact": {"type": "string", "description": "почта или телефон клиента, если известны"},
     }, ["reason", "summary"])},
]

BY_NAME = {t["name"]: t for t in TOOLS}


def specs() -> list[dict]:
    return [{k: t[k] for k in ("name", "description", "parameters")} for t in TOOLS]


def call(ctx: ToolContext, name: str, args: dict) -> dict:
    """Исполняет инструмент; ошибку возвращает модели текстом, чтобы она могла исправиться."""
    tool = BY_NAME.get(name)
    if tool is None:
        return {"error": f"нет инструмента {name}", "available": list(BY_NAME)}
    allowed = tool["parameters"]["properties"]
    try:
        return tool["handler"](ctx, **{k: v for k, v in (args or {}).items() if k in allowed})
    except TypeError as e:
        return {"error": f"неверные аргументы: {e}"}
    except Exception as e:  # сбой инструмента не должен ронять диалог
        return {"error": f"{type(e).__name__}: {e}"}
