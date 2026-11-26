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


def _get_product(ctx, product_id: str, quantity: int | None = None, **filters):
    filters = {k: v for k, v in filters.items() if v not in (None, "", False) and (v != 0 or k == "term_months")}
    if quantity:  # одно число на все виды ступеней: комплект на N пользователей, диапазон узлов, объем покупки
        filters.update(users=quantity, nodes=quantity, qty=quantity)
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


FACT_FIELDS = {  # что агент запоминает о клиенте: ключ -> как показывать в контексте и оператору
    "company": "компания", "inn": "ИНН", "org_type": "тип организации", "users": "пользователей",
    "devices": "устройств", "os": "ОС и инфраструктура", "task": "задача", "budget": "бюджет",
    "term": "срок лицензии", "contact": "контакт", "other": "прочее",
}


_FACT_KEYS = {**{k: v for k, v in FACT_FIELDS.items()}, **{v: v for v in FACT_FIELDS.values()}}


def save_facts(ctx, facts: dict) -> dict:
    """Запоминает сказанное клиентом о себе. Ключи: поля FACT_FIELDS или их русские названия."""
    saved = {}
    for k, v in facts.items():
        key = _FACT_KEYS.get(str(k).strip().lower())
        if key and str(v).strip():
            saved[key] = str(v).strip()[:300]
    if saved:
        ctx.facts.update(saved)
        store.update_session(ctx.session_id, facts=ctx.facts)
    return {"saved": saved, "known": ctx.facts}


def _calc_total(ctx, items: list[dict]):
    """Сумма по позициям считается кодом: модель не умножает и не складывает сама."""
    lines, total, on_request = [], 0.0, []
    for it in items or []:
        o = catalog.get_offer(int(it["offer_id"]))
        if o.get("error"):
            return {"error": f"нет варианта с offer_id {it['offer_id']}"}
        qty = max(1, int(it.get("qty") or 1))
        price = o.get("price")
        lo, hi = o.get("qty_min"), o.get("qty_max")
        if o.get("qty_pricing") == "per_unit" and ((lo and qty < lo) or (hi and qty > hi)):
            return {"error": f"Вариант {o['offer_id']} действует при покупке от {lo} до {hi or 'и больше'} лицензий, "
                             f"а в расчете {qty}. Найдите вариант под это количество: get_product с qty={qty}."}
        line = {"offer_id": o["offer_id"], "name": o["name"], "qty": qty, "price": price,
                "sum": round(price * qty, 2) if price else None}
        if o.get("price_for"):
            line["price_for"] = o["price_for"]
        if price:
            total += price * qty
        else:
            on_request.append(o["name"])
        lines.append(line)
    return {"lines": lines, "total": round(total, 2), "price_on_request": on_request,
            "note": "Цены из каталога, с НДС где он применим. Позиции без цены в сумму не вошли." if on_request
            else "Цены из каталога, с НДС где он применим."}


def _make_quote(ctx, items: list[dict], customer: str = "", inn: str = "", payment: str = "", contact: str = ""):
    res = kp.make_quote(ctx.session_id, customer=customer, items=items, inn=inn, payment=payment, contact=contact)
    if res.get("ok"):
        ctx.events.append({"type": "quote", **{k: res[k] for k in ("number", "url", "total")}})
    return res


def _handoff(ctx, reason: str, summary: str, contact: str = ""):
    old = store.open_handoff(ctx.session_id)
    if old:  # обращение уже у менеджера: дополняем его, а не заводим второе
        store.update_handoff(old["id"], summary or old["summary"], contact or old["contact"])
        return {"ok": True, "ticket": old["id"], "updated": True,
                "note": "Обращение уже у менеджера, сводка и контакт в нем обновлены."}
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
         "query": {"type": "string", "description": "только название продукта, вендор, артикул или тип ПО в "
                   "нормальном написании: без приветствий, количества и слов 'цена', 'лицензия', 'купить'"},
         "vendor": {"type": "string", "description": "вендор, если клиент его назвал"},
         "only_available": {"type": "boolean", "description": "только то, что можно купить сейчас по цене из каталога"},
         "russian_only": {"type": "boolean", "description": "только российские вендоры и реестр отечественного ПО"},
         "category_id": {"type": "integer", "description": "искать внутри категории (id из browse_categories)"},
         "product_type": {"type": "string", "enum": ["софт", "электроника"]},
     }, ["query"])},
    {"name": "get_product", "handler": _get_product,
     "description": "Варианты лицензий одного продукта с ценами и артикулами. Фильтры сужают набор. В ответе "
                    "поле clarify: какие параметры стоит уточнить у клиента, и facets: какие значения бывают. "
                    "Показаны первые 6 вариантов от дешевых, поле shown говорит, сколько всего. matching 0: такого "
                    "сочетания нет, в note сказано, без какого условия варианты есть. price_ambiguous: у артикула "
                    "несколько цен. Вызывайте после search_products и повторно, когда клиент ответил на уточнение.",
     "parameters": _obj({
         "product_id": {"type": "string", "description": "id продукта из search_products"},
         "quantity": {"type": "integer", "description": "сколько нужно клиенту: пользователей, рабочих мест, "
                      "устройств, узлов или лицензий. Останутся комплекты и ступени цены под это количество"},
         "term_months": {"type": "integer", "description": "срок лицензии в месяцах, 0 = бессрочная"},
         "license_type": {"type": "string", "description": "новая, продление, подписка, обновление, техподдержка"},
         "org_type": {"type": "string", "description": "коммерческая, государственная, образовательная, "
                      "некоммерческая, для дома"},
         "user_type": {"type": "string", "description": "физлицо или юрлицо"},
         "version": {"type": "string", "description": "версия или редакция: 'Воронеж', '1.8', '5.0'"},
         "platform": {"type": "string", "description": "платформа или ОС: Windows, Linux, macOS"},
         "text": {"type": "string", "description": "слово или часть артикула, которые должны быть в названии "
                  "варианта: 'Plus', 'сетевая', '256'"},
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
    {"name": "calc_total", "handler": _calc_total,
     "description": "Посчитать стоимость: цена варианта на количество и итог по позициям одной покупки. Любую "
                    "сумму, кроме цены из каталога, называйте клиенту только после этого инструмента. Варианты на "
                    "выбор считайте отдельными вызовами: total складывает все позиции.",
     "parameters": _obj({
         "items": {"type": "array", "description": "позиции расчета",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
     }, ["items"])},
    {"name": "make_quote", "handler": _make_quote,
     "description": "Сформировать черновик коммерческого предложения (PDF по шаблону Softline) для проверки "
                    "менеджером. После того, как клиент согласился с выбором и назвал количество. Название "
                    "организации спросите один раз; не назвал, формируйте без него.",
     "parameters": _obj({
         "customer": {"type": "string", "description": "наименование заказчика, если клиент его назвал"},
         "items": {"type": "array", "description": "позиции КП",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
         "inn": {"type": "string"},
         "payment": {"type": "string", "description": "условия оплаты, если клиент назвал"},
         "contact": {"type": "string", "description": "почта или телефон клиента"},
     }, ["items"])},
    {"name": "handoff_to_manager", "handler": _handoff,
     "description": "Передать обращение менеджеру: цена по запросу, скидка, госзакупка, статус заказа, жалоба, "
                    "нестандартные условия, вопрос вне каталога и базы знаний. В summary кратко: кто клиент, "
                    "что нужно, что уже подобрано. Повторный вызов дополняет то же обращение: так добавляют "
                    "контакт или новые детали.",
     "parameters": _obj({
         "reason": {"type": "string", "description": "причина одной фразой"},
         "summary": {"type": "string"},
         "contact": {"type": "string", "description": "почта или телефон клиента, если известны"},
     }, ["reason", "summary"])},
]

BY_NAME = {t["name"]: t for t in TOOLS}


# Примеры "реплика клиента -> аргументы": GigaChat принимает их вместе с описанием функции
EXAMPLES = {
    "search_products": [
        {"request": "сколько стоит кес на 30 компов", "params": {"query": "Kaspersky Endpoint Security"}},
        {"request": "нужен российский антивирус для организации",
         "params": {"query": "антивирус для бизнеса", "russian_only": True}},
        {"request": "оформите кп на kl4863rapfs 40 штук", "params": {"query": "KL4863RAPFS"}},
    ],
    "get_product": [
        {"request": "на 50 пользователей на год, новая лицензия",
         "params": {"product_id": "3935", "quantity": 50, "term_months": 12, "license_type": "новая"}},
    ],
    "kb_search": [
        {"request": "как я получу лицензию после оплаты", "params": {"query": "доставка электронной лицензии"}},
        {"request": "цены с ндс?", "params": {"query": "НДС"}},
    ],
    "handoff_to_manager": [
        {"request": "мы бюджетники, нужна скидка и закупка по 44-фз, почта zakupki@school.ru",
         "params": {"reason": "госзакупка и скидка", "summary": "Бюджетное учреждение, закупка по 44-ФЗ, "
                    "просит скидку. Продукт пока не назван.", "contact": "zakupki@school.ru"}},
    ],
}


_DROP_KEYS = {"url", "file", "sample_url", "has_children", "n_available", "qty_pricing", "nodes_min"}
_AMBIGUOUS_ONLY = "same_code_prices"  # список цен артикула нужен, только когда выбрать одну нельзя
_ONLY_TRUE = {"in_ru_registry", "russian_vendor", "price_on_request", "price_ambiguous", "has_price_on_request"}


def slim(name: str, out):
    """Ответ инструмента в том виде, в каком его видит модель: без ссылок, путей и пустых полей.

    Признаки реестра и российского вендора остаются, только когда они известны (true): отсутствие
    отметки в каталоге не значит "не российский". Пустая цена пишется словами, чтобы ее нельзя было
    принять за ноль. Ссылки остаются только в базе знаний: на них агент ссылается в ответе.
    """
    if isinstance(out, list):
        return [slim(name, v) for v in out]
    if not isinstance(out, dict):
        return out
    res = {}
    for k, v in out.items():
        if k == "price" and v is None:
            res[k] = "по запросу"
        elif (k in _DROP_KEYS and name != "kb_search") or (k in _ONLY_TRUE and not v):
            continue
        elif k == _AMBIGUOUS_ONLY and not out.get("price_ambiguous"):
            continue
        elif v is None or v == "" or (v in ([], {}) and k not in ("products", "offers", "results", "categories")):
            continue
        else:
            res[k] = slim(name, v)
    return res


def specs() -> list[dict]:
    return [{**{k: t[k] for k in ("name", "description", "parameters")}, "examples": EXAMPLES.get(t["name"], [])}
            for t in TOOLS]


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
