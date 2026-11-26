"""Инструменты агента: описание для модели и исполнение.

Все факты о товарах, ценах и условиях магазина агент получает только отсюда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import config, store
from app.catalog import cards, search as catalog
from app.kb import answers, search as kb
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
    res = catalog.search_products(query, vendor=vendor or None, category_id=category_id,
                                  only_available=only_available, russian_only=russian_only,
                                  product_type=product_type or None, limit=6)
    for p in (res.get("products") or [])[:4]:  # коротко, что это за продукт: только из уже сохраненных описаний
        p["about"] = cards.about(p["product_id"], p.get("url"), limit=ABOUT_IN_SEARCH, fetch=False)
    first = (res.get("products") or [None])[0]
    if first and res.get("match") == "all" and res.get("total", 0) <= 2:
        # продукт найден однозначно: его варианты отдаются сразу, второй вызов модели на get_product не нужен
        top = catalog.get_product(first["product_id"], limit=4)
        res["first_product"] = {"product_id": first["product_id"], **{k: top[k] for k in (
            "matching", "facets", "clarify", "offers", "shown", "all_matching") if top.get(k)}}
    return _with_answers(res, query)


def _with_answers(res: dict, query: str) -> dict:
    """Добавляет к ответу инструмента то, как менеджер уже отвечал на похожий вопрос."""
    learned = answers.find(query)
    if learned:
        res["manager_answers"] = learned
    return res


def _get_product(ctx, product_id: str, quantity: int | None = None, **filters):
    filters = {k: v for k, v in filters.items()
               if v is not None and v != "" and v is not False and (v != 0 or k == "term_months")}
    if quantity:  # одно число на все виды ступеней: комплект на N пользователей, диапазон узлов, объем покупки
        filters.update(users=quantity, nodes=quantity, qty=quantity)
    res = catalog.get_product(str(product_id), filters=filters, limit=6)
    if res.get("product"):  # описание со страницы товара: для чего продукт и чем отличается от соседних
        res["product"]["about"] = cards.about(res["product"]["product_id"], res["product"].get("url"))
    return res


def _browse_categories(ctx, query: str = "", parent_id: int | None = None):
    return catalog.browse_categories(query=query or None, parent_id=parent_id, limit=15)


def _find_analogs(ctx, product_id: str, russian_only: bool = True):
    return catalog.find_analogs(str(product_id), russian_only=russian_only, limit=6)


def _kb_search(ctx, query: str):
    res = kb.kb_search(query, limit=6)
    for r in res.get("results", []):
        r["snippet"] = r["snippet"][:700]
        r.pop("score", None)
    return _with_answers(res, query)


FACT_FIELDS = {  # что агент запоминает о клиенте: ключ -> как показывать в контексте и оператору
    "company": "компания", "inn": "ИНН", "kpp": "КПП", "org_type": "тип организации", "users": "пользователей",
    "devices": "устройств", "os": "ОС и инфраструктура", "task": "задача", "budget": "бюджет",
    "term": "срок лицензии", "contact": "контакт", "discount": "скидка", "other": "прочее",
}


_FACT_KEYS = {**FACT_FIELDS, **{v.lower(): v for v in FACT_FIELDS.values()}}


_CONTACT_LIKE = re.compile(r"@|(?:\d[\s()-]*){7,}")


def merge_contact(known: str, given: str) -> str:
    """Контакт клиента: то, что код сам вынул из его сообщений, плюс новое, похожее на контакт.

    Модель настоящих почты и телефона не видит, поэтому ее слова ("почта получена", метка [email]) контакт
    не заменяют: менеджер получает адрес, который написал клиент.
    """
    parts = [c for c in known.split(", ") if c]
    for c in given.split(", "):
        c = c.strip()
        if c and "[" not in c and _CONTACT_LIKE.search(c) and c not in parts:
            parts.append(c)
    return ", ".join(parts)


MODEL_CANNOT_SET = ("ИНН", "КПП", "контакт")  # их модель не видит: настоящие значения код берет из слов клиента


def save_facts(ctx, facts: dict, from_model: bool = False) -> dict:
    """Запоминает сказанное клиентом о себе. Ключи: поля FACT_FIELDS или их русские названия.

    from_model: факты из метки [[facts: ...]] в ответе модели. Реквизиты и контакт из нее не принимаются
    (модель видела только метки, значит значение выдумано), скидка не больше полномочий агента.
    """
    saved = {}
    for k, v in facts.items():
        key, v = _FACT_KEYS.get(str(k).strip().lower()), str(v).strip()
        if from_model and key in MODEL_CANNOT_SET:
            continue
        if from_model and key == "скидка":
            number = re.search(r"\d+(?:[.,]\d+)?", v)
            given = kp.allowed_discount(number.group().replace(",", ".")) if number else 0
            if not given:
                continue
            v = f"{float(given):g}%"
        if key == "контакт":
            v = merge_contact(ctx.facts.get("контакт", ""), v)
            if v == ctx.facts.get("контакт", ""):
                continue
        if key and v:
            saved[key] = v[:300]
    if saved:
        ctx.facts.update(saved)
        store.update_session(ctx.session_id, facts=ctx.facts)
        ticket = store.open_handoff(ctx.session_id)
        if ticket and saved.get("контакт"):  # обращение уже у менеджера: контакт попадает в него без вызова модели
            store.update_handoff(ticket["id"], ticket["summary"], saved["контакт"])  # остальное покажет карточка
    return {"saved": saved, "known": ctx.facts}


def _contact(ctx, given: str) -> str:
    """Модель видит вместо почты и телефона метки: настоящий контакт подставляется из памяти диалога."""
    return merge_contact(ctx.facts.get("контакт", ""), given or "")


def _discount(ctx, asked) -> float:
    """Скидка расчета: названная в вызове либо уже данная клиенту в этом диалоге, в пределах полномочий агента."""
    given = kp.allowed_discount(asked or str(ctx.facts.get("скидка", "0")).rstrip("% "))
    if given:
        save_facts(ctx, {"discount": f"{float(given):g}%"})
    return float(given)


def _over_limit(asked) -> str:
    try:
        over = float(asked or 0) > config.AGENT_DISCOUNT_MAX
    except (TypeError, ValueError):
        over = False
    return (f"Сами вы даете не больше {config.AGENT_DISCOUNT_MAX:g}%: посчитано с этой скидкой, скидку больше "
            "согласует менеджер, передайте ему запрос.") if over else ""


def _calc_total(ctx, items: list[dict], discount_percent: float = 0):
    """Сумма по позициям считается кодом: модель не умножает и не складывает сама."""
    lines, total, on_request = [], 0.0, []
    discount = _discount(ctx, discount_percent)
    for it in items or []:
        o = catalog.get_offer(int(it["offer_id"]))
        if o.get("error"):
            return {"error": f"нет варианта с offer_id {it['offer_id']}"}
        qty = max(1, int(it.get("qty") or 1))
        price = o.get("price")
        tier = kp.tier_problem(o, qty)  # та же проверка ступени цены, что в КП
        if tier:
            return {"error": tier[0].upper() + tier[1:] + "."}
        line = {"offer_id": o["offer_id"], "name": o["name"], "qty": qty, "price": price,
                "sum": round(price * qty, 2) if price else None}
        if price and discount:  # цена со скидкой считается так же, как в КП: до копейки, затем на количество
            cut = float(kp.discounted(kp.Decimal(str(price)), kp.Decimal(str(discount))))
            line.update(price_with_discount=cut, sum_without_discount=line["sum"], sum=round(cut * qty, 2))
        if o.get("price_for"):
            line["price_for"] = o["price_for"]
        if price:
            total += line["sum"]
        else:
            on_request.append(o["name"])
        lines.append(line)
    if discount:
        return {"lines": lines, "total": round(total, 2), "discount_percent": discount,
                "total_without_discount": round(sum(ln["sum_without_discount"] for ln in lines if ln["sum"]), 2),
                "price_on_request": on_request,
                "note": (f"Суммы посчитаны со скидкой {discount:g}%. " + _over_limit(discount_percent)).strip()}
    return {"lines": lines, "total": round(total, 2), "price_on_request": on_request,
            "note": "Цены из каталога, с НДС где он применим. Позиции без цены в сумму не вошли." if on_request
            else "Цены из каталога, с НДС где он применим."}


def _make_quote(ctx, items: list[dict], customer: str = "", inn: str = "", kpp: str = "", payment: str = "",
                contact: str = "", discount_percent: float = 0):
    """Реквизиты графы «Заказчик» помнит диалог: названные раньше подставляются сами, новые запоминаются."""
    given = {k: v for k, v in (("company", customer), ("inn", inn), ("kpp", kpp))
             if str(v or "").strip() and "[" not in str(v)}  # метка вместо реквизита: настоящий уже записан кодом
    if given:
        save_facts(ctx, given)
    res = kp.make_quote(ctx.session_id, customer=ctx.facts.get("компания", ""), items=items,
                        inn=ctx.facts.get("ИНН", ""), kpp=ctx.facts.get("КПП", ""), payment=payment,
                        contact=_contact(ctx, contact), discount_percent=_discount(ctx, discount_percent))
    if res.get("ok") and _over_limit(discount_percent):
        res["note"] += " " + _over_limit(discount_percent)
    if res.get("ok"):
        ctx.events.append({"type": "quote", **{k: res[k] for k in ("number", "url", "total")}})
    return res


def _handoff(ctx, reason: str, summary: str, contact: str = ""):
    contact = _contact(ctx, contact)
    old = store.open_handoff(ctx.session_id)
    if old:  # обращение уже у менеджера: дополняем его, а не заводим второе
        store.update_handoff(old["id"], summary or old["summary"], contact or old["contact"])
        return {"ok": True, "ticket": old["id"], "updated": True,
                "note": "Обращение уже у менеджера, сводка и контакт в нем обновлены."}
    hid = store.add_handoff(ctx.session_id, reason, summary, contact)
    ctx.events.append({"type": "handoff", "id": hid, "reason": reason})
    return {"ok": True, "ticket": hid,
            "note": "Обращение передано менеджеру. Скажите клиенту, что менеджер ответит в рабочее время"
                    + (": контакт клиента в обращении есть." if contact
                       else ", и попросите почту или телефон для связи.")}


TOOLS = [
    {"name": "search_products", "handler": _search_products,
     "description": "Поиск продуктов в каталоге магазина по названию, вендору или задаче. Возвращает продукты "
                    "(линейки) с числом вариантов лицензий, диапазоном цен, наличием и признаком реестра "
                    "отечественного ПО. Понимает опечатки и разговорные названия. Начинайте с него. Продукт найден "
                    "однозначно: в first_product сразу его варианты, как из get_product без фильтров.",
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
                    "сумму, кроме цены из каталога, называйте клиенту только после этого инструмента. У каждой "
                    "позиции своя сумма в lines: варианты на выбор считайте одним вызовом и берите суммы строк, "
                    "total это все позиции вместе.",
     "parameters": _obj({
         "items": {"type": "array", "description": "позиции расчета",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
         "discount_percent": {"type": "number", "description": "скидка в процентах, только когда клиент сам попросил "
                              "скидку или отсрочку; сами вы даете до 5, больше согласует менеджер"},
     }, ["items"])},
    {"name": "make_quote", "handler": _make_quote,
     "description": "Сформировать черновик коммерческого предложения (PDF по шаблону Softline) для проверки "
                    "менеджером, когда вариант и количество понятны. В графу «Заказчик» идут название "
                    "организации, ИНН и КПП: передайте те, что клиент назвал, без них черновик тоже формируется. "
                    "Повторный вызов в том же диалоге обновляет тот же черновик.",
     "parameters": _obj({
         "customer": {"type": "string", "description": "наименование заказчика, если клиент его назвал"},
         "items": {"type": "array", "description": "позиции КП",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
         "inn": {"type": "string", "description": "ИНН заказчика: 10 цифр, у ИП 12"},
         "kpp": {"type": "string", "description": "КПП заказчика: 9 знаков, у ИП его нет"},
         "payment": {"type": "string", "description": "условия оплаты, если клиент назвал"},
         "contact": {"type": "string", "description": "почта или телефон клиента"},
         "discount_percent": {"type": "number", "description": "скидка в процентах, только когда клиент сам попросил "
                              "скидку или отсрочку; сами вы даете до 5, больше согласует менеджер"},
     }, ["items"])},
    {"name": "handoff_to_manager", "handler": _handoff,
     "description": "Передать обращение менеджеру: продукта нет в каталоге, клиент просит цену или КП на вариант с ценой "
                    "по запросу, скидка больше 5%, госзакупка, жалоба, просьба позвать человека; вопрос вне "
                    "каталога и базы знаний после согласия клиента. В summary кратко: кто клиент, "
                    "что нужно, что уже подобрано. На диалог хватает одного вызова: контакт и новые детали, "
                    "которые клиент сообщит после передачи, менеджер увидит в карточке обращения сам.",
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


ABOUT_IN_SEARCH = 220  # знаков описания на продукт в результатах поиска


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
        elif k in ("price_min", "price_max") and "n_offers" in out:
            continue  # диапазон по всем вариантам продукта клиенту не ориентир: минимум это чужая ступень и тип покупателя
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
