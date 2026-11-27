"""Инструменты агента: описание для модели и исполнение.

Все факты о товарах, ценах и условиях магазина агент получает только отсюда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import config, store
from app.catalog import cards, search as catalog
from app.kb import answers, search as kb
from app.quote import delivery as invoice_delivery, kp


@dataclass
class ToolContext:
    session_id: str
    facts: dict = field(default_factory=dict)
    events: list = field(default_factory=list)  # что показать оператору и интерфейсу: КП, передача менеджеру
    exact_products: dict[str, str] = field(default_factory=dict, init=False)  # для условий с карточки в этом ходе
    checked_help: bool = field(default=False, init=False)
    user_request: str = ""  # текущий ход: просьба об отсрочке сама по себе не даёт основания менять цену

    def deferral_without_discount(self) -> bool:
        return bool(re.search(r"отсрочк|постоплат|оплат\w* после (?:поставки|получения|отгрузки)", self.user_request, re.I)
                    and not re.search(r"скидк|дисконт|дешевле|снизить цену", self.user_request, re.I))


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
        # Это сокращение для подбора вариантов, само по себе не доказательство совпадения карточки.
        title = first["title"].casefold()
        identifiers = {title, f"{first.get('vendor', '')} {title}".strip().casefold(),
                       (first.get("offer_hit") or {}).get("vendor_code", "").casefold()}
        name = query.strip().casefold()
        if res.get("total") == 1 and name and name in identifiers:
            ctx.exact_products[str(first["product_id"])] = name
        top = catalog.get_product(first["product_id"], limit=4)
        res["first_product"] = {"product_id": first["product_id"], **{k: top[k] for k in (
            "matching", "facets", "clarify", "offers", "delivery", "shown", "all_matching") if top.get(k)}}
    return _with_answers(res, query)


def _with_answers(res: dict, query: str) -> dict:
    """Добавляет к ответу инструмента то, как менеджер уже отвечал на похожий вопрос."""
    learned = answers.find(query)
    if learned:
        res["manager_answers"] = learned
    return res


def _named_by_buyer(ctx, name: str) -> bool:
    """Повторный поиск полного названия, взятого лишь из нечеткой выдачи, не подтверждает товар."""
    if not name:
        return False
    mention = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
    return bool(mention.search(ctx.user_request)) or any(
        m["role"] == "user" and mention.search(m["content"]) for m in store.messages(ctx.session_id, limit=12))


def _get_product(ctx, product_id: str, quantity: int | None = None, card_section: str = "", **filters):
    if card_section:
        if card_section not in ("licensing", "requirements"):
            return {"error": "Неизвестный раздел карточки."}
        if not ctx.checked_help or not _named_by_buyer(ctx, ctx.exact_products.get(str(product_id), "")):
            return {"error": "Сначала проверьте kb_search и найдите конкретный товар через search_products "
                             "по полному названию или артикулу клиента. Неоднозначно: уточните товар."}
        if filters or quantity:
            return {"error": "Карточка относится к продукту, не к отфильтрованным вариантам. "
                             "Редакцию и условия сверяйте по самому тексту карточки."}
        res = catalog.get_product(str(product_id), limit=0)
        product = res.get("product")
        if not product:
            return res
        text = cards.terms(str(product_id), product.get("url"), card_section)
        out = {"product_id": str(product_id), "title": product["title"]}
        if text:
            return {**out, card_section: text}
        return {**out, "note": "Полного текста этого раздела нет. Не додумывайте условия."}

    filters = {k: v for k, v in filters.items()
               if v is not None and v != "" and v is not False and (v != 0 or k == "term_months")}
    if quantity:  # одно число на все виды ступеней: комплект на N пользователей, диапазон узлов, объем покупки
        filters.update(users=quantity, nodes=quantity, qty=quantity)
    res = catalog.get_product(str(product_id), filters=filters, limit=6)
    if res.get("product"):  # описание со страницы товара: для чего продукт и чем отличается от соседних
        res["product"]["about"] = cards.about(res["product"]["product_id"], res["product"].get("url"), fetch=not config.COMPACT_LOCAL)
    return res


def _browse_categories(ctx, query: str = "", parent_id: int | None = None):
    return catalog.browse_categories(query=query or None, parent_id=parent_id, limit=15)


def _find_analogs(ctx, product_id: str, russian_only: bool = True):
    return catalog.find_analogs(str(product_id), russian_only=russian_only, limit=6)


def _kb_search(ctx, query: str, buyer_help: bool = False):
    res = kb.kb_search(query, limit=6, buyer_help=True) if buyer_help else kb.kb_search(query, limit=6)
    ctx.checked_help = bool(query.strip()) and "error" not in res
    for r in res.get("results", []):
        r["snippet"] = r["snippet"][:700]
        r.pop("score", None)
    if buyer_help:
        if not res.get("results"):
            res["note"] = "Без безопасного шага в справке передайте вопрос менеджеру."
        return res  # прошлый ответ о чужом заказе не служит инструкцией
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
            if ctx.deferral_without_discount():
                continue
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
    percent, _ = kp.discount_arg(asked or ctx.facts.get("скидка", "0"))
    given = kp.allowed_discount(percent)
    if given:
        save_facts(ctx, {"discount": f"{float(given):g}%"})
    return float(given)


def _over_limit(asked) -> str:
    percent, _ = kp.discount_arg(asked)
    over = percent > config.AGENT_DISCOUNT_MAX
    return (f"Сами вы даете не больше {config.AGENT_DISCOUNT_MAX:g}%: посчитано с этой скидкой, скидку больше "
            "согласует менеджер, передайте ему запрос.") if over else ""


def _calc_total(ctx, items: list[dict], discount_percent: float = 0):
    """Сумма по позициям считается кодом: модель не умножает и не складывает сама."""
    lines, total, on_request, unsure = [], 0.0, [], []
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
        if o.get("available") is False:
            line.update(available=False, availability_note=kp.FEED_UNAVAILABLE)
        if price and discount:  # цена со скидкой считается так же, как в КП: до копейки, затем на количество
            cut = float(kp.discounted(kp.Decimal(str(price)), kp.Decimal(str(discount))))
            line.update(price_with_discount=cut, sum_without_discount=line["sum"], sum=round(cut * qty, 2))
        if o.get("price_for"):
            line["price_for"] = o["price_for"]
        if price:
            total += line["sum"]
            if kp.price_to_confirm(o):  # та же проверка, что дает сноску о цене в КП
                unsure.append(o["name"])
        else:
            on_request.append(o["name"])
        lines.append(line)
    check = (f" У артикула позиции {', '.join(unsure)} в каталоге несколько цен, в расчете цена выбранного варианта. "
             "Скажите клиенту, что цену этой позиции подтвердит менеджер.") if unsure else ""
    if sum(1 for ln in lines if ln["sum"]) > 1:  # сумму двух вариантов на выбор модель выдавала за итог заказа
        check += " total это сумма всех строк: если строки это варианты на выбор, называйте суммы строк, а не total."
    if discount:
        return {"lines": lines, "total": round(total, 2), "discount_percent": discount,
                "total_without_discount": round(sum(ln["sum_without_discount"] for ln in lines if ln["sum"]), 2),
                "price_on_request": on_request,
                "note": (f"Суммы посчитаны со скидкой {discount:g}%. " + _over_limit(discount_percent)).strip() + check}
    return {"lines": lines, "total": round(total, 2), "price_on_request": on_request,
            "note": ("Цены из каталога, с НДС где он применим. Позиции без цены в сумму не вошли." if on_request
                     else "Цены из каталога, с НДС где он применим.") + check}


def _words(text: str) -> str:
    """Текст для сравнения названий: нижний регистр, без кавычек и знаков препинания."""
    return " ".join(re.findall(r"[0-9a-zа-я]+", str(text).lower().replace("ё", "е")))


def _said_at(said: list[str], key: str, value: str) -> tuple[int, int] | None:
    """Где клиент в последний раз написал значение реквизита: номер его сообщения и место в нем; не писал: None.

    ИНН и КПП сверяются по знакам (цифры, разбитые пробелами или дефисами, тоже считаются), название по словам.
    """
    last = None
    if key == "company":
        name = _words(value)
        for n, t in enumerate(said):
            if name and f" {name} " in f" {_words(t)} ":
                last = (n, 0)
        return last
    code = re.sub(r"[^0-9A-Z]", "", str(value).upper())
    for n, t in enumerate(said):
        for group in re.finditer(r"[0-9A-Z]+(?:[ -][0-9A-Z]+)*", t.upper()):
            if code and re.sub(r"[ -]", "", group.group()) == code:
                last = (n, group.start())
            for part in re.finditer(r"[0-9A-Z]+", group.group()):
                if code and part.group() == code:
                    last = (n, group.start() + part.start())
    return last


def _make_quote(ctx, items: list[dict], customer: str = "", inn: str = "", kpp: str = "", contact: str = "",
                discount_percent: float = 0):
    """Реквизиты графы «Заказчик» помнит диалог: названные раньше подставляются сами, новые запоминаются.

    Зачем сверка со словами клиента: модель могла переписать реквизит с ошибкой, взять его не из слов клиента или
    вернуть ИНН, который клиент потом исправил. Реквизит от модели принимается, только если клиент сам его написал;
    ИНН и КПП, кроме того, только если клиент назвал их не раньше сохраненных: в КП идет последнее названное.
    """
    said = [m["content"] for m in store.messages(ctx.session_id, limit=10_000) if m["role"] == "user"]
    given = {}
    for key, value in (("company", customer), ("inn", inn), ("kpp", kpp)):
        if not str(value or "").strip() or "[" in str(value):  # метка вместо реквизита: настоящий уже записан кодом
            continue
        at, saved = _said_at(said, key, value), ctx.facts.get(FACT_FIELDS[key])
        if at and (key == "company" or not saved or at > (_said_at(said, key, saved) or (-1, 0))):
            given[key] = value
    if given:
        save_facts(ctx, given)
    res = kp.make_quote(ctx.session_id, customer=ctx.facts.get("компания", ""), items=items,
                        inn=ctx.facts.get("ИНН", ""), kpp=ctx.facts.get("КПП", ""),
                        contact=_contact(ctx, contact), discount_percent=_discount(ctx, discount_percent))
    if res.get("ok") and _over_limit(discount_percent):
        res["note"] += " " + _over_limit(discount_percent)
    if res.get("ok"):
        ctx.events.append({"type": "quote", **{k: res[k] for k in ("number", "url", "total")}})
    return res


def _handoff(ctx, reason: str, summary: str, contact: str = ""):
    contact = _contact(ctx, contact)
    hid, created = store.handoff(ctx.session_id, reason, summary, contact)
    if not created:  # обращение уже у менеджера: оно дополнено, второе не заводится
        return {"ok": True, "ticket": hid, "updated": True,
                "note": "Обращение уже у менеджера, сводка и контакт в нем обновлены."}
    ctx.events.append({"type": "handoff", "id": hid, "reason": reason})
    return {"ok": True, "ticket": hid,
            "note": "Обращение передано менеджеру. Скажите клиенту, что менеджер ответит в рабочее время"
                    + (": контакт клиента в обращении есть." if contact
                       else ", и попросите почту или телефон для связи.")}


def _make_invoice(ctx):
    if not invoice_delivery.requested(ctx.session_id):
        return {'ok': False, 'error': 'Учебный счёт выдаётся только по явной просьбе покупателя об образце.'}
    result = invoice_delivery.publish(ctx.session_id, 'agent')
    if result.get('customer_missing'):
        return {'ok': False, 'customer_missing': result['customer_missing']}
    if not (result['ok'] or result.get('already_sent')):
        return {'ok': False, 'error': result['detail']}
    return {'ok': True, 'status': 'issued', 'note': 'Учебный PDF доступен покупателю в этом чате. Не для оплаты; заказ не создан.'}


TOOLS = [
    {"name": "search_products", "handler": _search_products,
     "description": "Поиск продуктов в каталоге магазина по названию, вендору или задаче. Возвращает продукты "
                    "(линейки) с числом вариантов лицензий, наличием и признаком реестра "
                    "отечественного ПО. Понимает опечатки и разговорные названия. Начинайте с него. Продукт найден "
                    "однозначно: в first_product сразу его варианты, как из get_product без фильтров.",
     "parameters": _obj({
         "query": {"type": "string", "description": "только название продукта, вендор, артикул или тип ПО в "
                   "нормальном написании: без приветствий, количества и слов 'цена', 'лицензия', 'купить'"},
         "vendor": {"type": "string", "description": "вендор, если клиент его назвал"},
         "only_available": {"type": "boolean", "description": "только варианты с отметкой наличия в фиде (для КП фильтр не обязателен)"},
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
         "card_section": {"type": "string", "enum": ["licensing", "requirements"],
                          "description": "Только если каталог и kb_search не дали прямого ответа и продукт точно "
                                         "определен. Вернет текст вкладки о лицензировании или системных требованиях "
                                         "вместо вариантов. Не сочетайте с фильтрами."},
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
                    "по ней. После покупки сначала найдите безопасные шаги для покупателя; инструмент не "
                    "проверяет конкретный заказ и не оформляет возврат.",
     "parameters": _obj({"query": {"type": "string", "description": "тема существительными, как в справке: "
                                   "'доставка электронной лицензии', 'способы оплаты', 'закрывающие документы'"},
                         "buyer_help": {"type": "boolean", "description": "true для безопасной инструкции после покупки: "
                                        "только справка покупателя, без API и чужих обращений"}},
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
                              "скидку; до 5 включительно без купона. Отсрочку не заменяйте скидкой"},
     }, ["items"])},
    {"name": "make_quote", "handler": _make_quote,
     "description": "Сформировать коммерческое предложение (PDF по шаблону Softline). При известной цене выдайте "
                    "его клиенту без подтверждения менеджера, когда вариант и количество понятны. В графу «Заказчик» идут название "
                    "организации, ИНН и КПП: передайте те, что клиент назвал, без них черновик тоже формируется. "
                    "Повторный вызов в том же диалоге обновляет тот же черновик.",
     "parameters": _obj({
         "customer": {"type": "string", "description": "наименование заказчика, если клиент его назвал"},
         "items": {"type": "array", "description": "позиции КП",
                   "items": _obj({"offer_id": {"type": "integer"}, "qty": {"type": "integer"}},
                                 ["offer_id", "qty"])},
         "inn": {"type": "string", "description": "ИНН заказчика: 10 цифр, у ИП 12"},
         "kpp": {"type": "string", "description": "КПП заказчика: 9 знаков, у ИП его нет"},
         "contact": {"type": "string", "description": "почта или телефон клиента"},
         "discount_percent": {"type": "number", "description": "скидка в процентах, только когда клиент сам попросил "
                              "скидку; до 5 включительно без купона. Отсрочку не заменяйте скидкой"},
     }, ["items"])},
    {"name": "make_invoice", "handler": _make_invoice,
     "description": "Только по явной просьбе об учебном образце счета. По проверенным ценам последнего КП автоматически выдаёт PDF только владельцу чата. Поставку не подтверждает. Не для оплаты; вопросы о способах оплаты направляйте в kb_search.",
     "parameters": _obj({})},
    {"name": "handoff_to_manager", "handler": _handoff,
     "description": "Передать обращение менеджеру: продукта нет в каталоге, клиент просит цену или КП на вариант с ценой "
                    "по запросу, отсрочка или постоплата, скидка больше 5%, госзакупка, жалоба, просьба позвать человека; вопрос вне "
                    "каталога и базы знаний после согласия клиента. После покупки передайте без ожидания согласия, "
                    "если нет безопасного шага в справке, он не помог или просят персональную проверку либо "
                    "оформление возврата. В summary кратко: кто клиент,"
                    "что нужно, что уже подобрано. На диалог хватает одного вызова: контакт и новые детали, "
                    "которые клиент сообщит после передачи, менеджер увидит в карточке обращения сам.",
     "parameters": _obj({
         "reason": {"type": "string", "description": "причина одной фразой по-русски"},
         "summary": {"type": "string", "description": "по-русски, без имени и контактов клиента"},
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


# Каталог называет количество в facets и clarify по виду ступени (users, nodes, qty), а у get_product это одно
# поле quantity: фильтр, названный так, как его назвал сам каталог, не должен теряться.
ALIASES = {"get_product": {"users": "quantity", "nodes": "quantity", "qty": "quantity"}}


def call(ctx: ToolContext, name: str, args: dict) -> dict:
    """Исполняет инструмент; ошибку возвращает модели текстом, чтобы она могла исправиться."""
    tool = BY_NAME.get(name)
    if tool is None:
        return {"error": f"нет инструмента {name}", "available": list(BY_NAME)}
    allowed, args = tool["parameters"]["properties"], dict(args or {})
    if name in ("calc_total", "make_quote") and args.get("discount_percent") and ctx.deferral_without_discount():
        return {"error": "Клиент просил отсрочку, а не скидку. Передайте просьбу менеджеру через handoff_to_manager; "
                         "для расчёта повторите вызов без новой скидки."}
    for alias, to in ALIASES.get(name, {}).items():  # имя измерения из facets и clarify самого ответа каталога
        if alias in args and to not in args:
            args[to] = args[alias]
    ignored = sorted(k for k in args if k not in allowed and k not in ALIASES.get(name, {}))
    try:
        res = tool["handler"](ctx, **{k: v for k, v in args.items() if k in allowed})
        if ignored and isinstance(res, dict):  # молча выброшенный фильтр модель приняла бы за примененный
            res["ignored_args"] = ignored
        return res
    except TypeError as e:
        return {"error": f"неверные аргументы: {e}"}
    except Exception as e:  # сбой инструмента не должен ронять диалог
        return {"error": f"{type(e).__name__}: {e}"}
