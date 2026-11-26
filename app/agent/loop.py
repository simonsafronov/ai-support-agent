"""Цикл агента: модель выбирает инструмент, получает результат и решает следующий шаг, пока не ответит клиенту.

Вокруг модели стоят проверки кодом: цена в ответе должна быть из каталога, вызовов не больше лимита,
сбой модели или инструмента не оставляет клиента без ответа.
"""
from __future__ import annotations

import datetime
import json
import re
import time
from urllib.parse import urlparse

from app import config, store
from app.agent import prices, prompts, tools
from app.agent.llm import LLMError, get_llm
from app.catalog import search as catalog

MAX_STEPS = 8           # вызовов модели на один ход клиента
HISTORY_MESSAGES = 14   # сколько прошлых реплик диалога видит модель
TOOL_RESULT_LIMIT = 6000

LAST_STEP = ("[Служебное сообщение, клиент его не видит] Это последний шаг хода: инструменты больше не вызывайте, "
             "ответьте клиенту по тому, что уже известно, и скажите, что уточните дальше.")
REPEAT = {"repeat": True, "note": "Этот вызов с теми же аргументами в этом ходе уже был, результат выше. "
                                  "Ответьте клиенту или измените запрос."}
MSK = datetime.timezone(datetime.timedelta(hours=3))

FALLBACK = ("Сейчас не получается ответить автоматически. Передаю вопрос менеджеру, он ответит в рабочее время. "
            "Оставьте, пожалуйста, почту или телефон для связи.")

_MARK = re.compile(r"\[\[(product|offer):\s*([\w-]+)\]\]")
_CHOICES = re.compile(r"\[\[choices:\s*(.+?)\]\]", re.S)
_FACTS = re.compile(r"\[\[facts:\s*(.+?)\]\]", re.S)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_FIELD = re.compile(r"(?<![\w/.-])[a-z]+(?:_[a-z]+)+(?![\w/-])")   # имя поля или инструмента: russian_vendor
_PHONE = re.compile(r"(?<![\w+])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")


def mask_contacts(text: str) -> tuple[str, list[str]]:
    """Почта и телефон клиента в модель не уходят: вместо них метки, сами значения видит только оператор."""
    found = _EMAIL.findall(text) + _PHONE.findall(text)
    return _PHONE.sub("[телефон]", _EMAIL.sub("[email]", text)), found


def field_names(obj) -> set[str]:
    """Имена полей из ответа инструмента: по ним проверка отличает служебное имя от названия товара."""
    if isinstance(obj, dict):
        return {k for k in obj if _FIELD.fullmatch(k)}.union(*(field_names(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(field_names(v) for v in obj))
    return set()


def internal_names(text: str, vocabulary: set[str]) -> list[str]:
    """Служебные имена полей и инструментов в ответе клиенту. Адреса страниц и метки не считаются."""
    found = _FIELD.findall(re.sub(r"https?://\S+|\[\[.*?\]\]", " ", text, flags=re.S))
    return sorted(set(found) & vocabulary)


def drop_internal(text: str, names: list[str]) -> str:
    """Модель не убрала служебные имена сама: убираем скобку с именем или само имя, смысл фразы остается."""
    for name in names:
        text = re.sub(rf"\s*\([^()]*\b{name}\b[^()]*\)", "", text)
        text = re.sub(rf"\s*(?:поле|поля|полю|отметка|отметки|признак)?\s*\b{name}\b", "", text)
    return text


def fit_json(out: dict, limit: int) -> str:
    """Ответ инструмента для модели: всегда целый JSON не длиннее limit.

    Длинный ответ не режется по символам (модель получила бы битый JSON): из самых длинных списков
    по одному убираются последние элементы, в ответе остается пометка, сколько убрано.
    """
    text = json.dumps(out, ensure_ascii=False)
    if len(text) <= limit:
        return text
    out = json.loads(text)
    lists = {k: v for k, v in out.items() if isinstance(v, list) and v}
    cut = dict.fromkeys(lists, 0)
    while len(text) > limit and any(len(v) > 1 for v in lists.values()):
        key = max(lists, key=lambda k: len(json.dumps(lists[k], ensure_ascii=False)) if len(lists[k]) > 1 else -1)
        lists[key].pop()
        cut[key] += 1
        out["truncated"] = {k: n for k, n in cut.items() if n}
        text = json.dumps(out, ensure_ascii=False)
    if len(text) > limit:
        text = json.dumps({"error": "ответ слишком большой, сузьте запрос фильтрами"}, ensure_ascii=False)
    return text


def page_hint(page_url: str) -> str:
    """Подсказка по странице, с которой пишет клиент: на карточке товара вопрос обычно про этот товар."""
    page_url = re.sub(r"[\[\]\s<>\"']", "", page_url)[:200]  # адрес приходит из браузера: в инструкцию только чистый
    if not page_url.startswith(("http://", "https://")):
        return ""
    parts = [p for p in urlparse(page_url).path.split("/") if p]
    hint = f"Клиент пишет со страницы {page_url}."
    if len(parts) >= 2 and parts[0] not in ("search", "basket", "b2b", "personal", "delivery_info"):
        q = re.sub(r"[-_]+", " ", parts[1])
        q = re.sub(r"\b[0-9a-f]{8,}\b|\b\d{4,}\b", " ", q).strip()
        found = catalog.search_products(q, limit=3).get("products", []) if q else []
        if found:
            hint += " Похоже, это страница продукта: " + "; ".join(
                f"{p['title']} ({p['vendor']}, product_id {p['product_id']})" for p in found) + "."
    return hint


def _remember_items(items: dict, name: str, out: dict):
    """Память диалога о найденном: чтобы в следующих ходах модель знала id продуктов и вариантов."""
    if name in ("search_products", "find_analogs") and out.get("products"):
        new = [{"id": p["product_id"], "title": p["title"]} for p in out["products"]]
        old = [p for p in items.get("products", []) if p["id"] not in {n["id"] for n in new}]
        items["products"] = (new + old)[:10]
    if name == "get_product" and out.get("offers"):
        new = [{"id": o["offer_id"], "name": o["name"], "price": o.get("price")} for o in out["offers"]]
        old = [o for o in items.get("offers", []) if o["id"] not in {n["id"] for n in new}]
        items["offers"] = (new + old)[:10]
        p = out.get("product") or {}
        if p.get("product_id"):
            cur = {"id": p["product_id"], "title": p["title"]}
            items["products"] = ([cur] + [x for x in items.get("products", []) if x["id"] != cur["id"]])[:10]


def render(text: str) -> tuple[str, list[dict], list[str]]:
    """Убирает служебные метки из текста и собирает карточки товаров и кнопки выбора."""
    cards, seen = [], set()
    for kind, ident in _MARK.findall(text):
        if (kind, ident) in seen:
            continue
        seen.add((kind, ident))
        try:
            if kind == "offer":
                o = catalog.get_offer(int(ident))
                if o:
                    cards.append({"type": "offer", "id": o["offer_id"], "title": o["name"], "vendor": o.get("vendor"),
                                  "vendor_code": o.get("vendor_code"), "price": o.get("price"),
                                  "available": o.get("available"), "url": o.get("url")})
            else:
                p = (catalog.get_product(ident, limit=0) or {}).get("product")
                if p:
                    cards.append({"type": "product", "id": p["product_id"], "title": p["title"],
                                  "vendor": p.get("vendor"), "price_min": p.get("price_min"),
                                  "price_max": p.get("price_max"), "price_on_request": p.get("price_on_request"),
                                  "n_offers": p.get("n_offers"), "in_ru_registry": p.get("in_ru_registry"),
                                  "url": p.get("url")})
        except (ValueError, KeyError, TypeError):
            pass
    titles = {(c["type"], str(c["id"])): c["title"] for c in cards}

    def title(m):  # метка вместо названия: "[[product:1]] (вендор)"; название уже написано словами: не дублируем
        name = titles.get((m.group(1), m.group(2)), "")
        return "" if m.string[:m.start()].rstrip().lower().endswith(name.lower()) else name

    text = re.sub(_MARK.pattern + r"(?=\s*\()", title, text)
    choices = []
    m = _CHOICES.search(text)
    if m:
        choices = [c.strip() for c in m.group(1).split("|") if c.strip()][:5]
    clean = _CHOICES.sub("", re.sub(r"[ \t]*" + _MARK.pattern, "", _FACTS.sub("", text)))
    clean = re.sub(r"(?<!\.)\.\.(?!\.)", ".", clean)             # метка стояла перед точкой: "руб. [[offer:1]]."
    clean = re.sub(r"[ \t]+([,;])", r"\1", re.sub(r"[ \t]{2,}", " ", clean))
    clean = re.sub(r",(?=[.;])", "", clean)                      # метка стояла после запятой: "за позицию, [[offer:1]]."
    clean = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", clean)).strip()
    return clean, cards, choices


def take_facts(text: str) -> dict:
    """Что модель записала о клиенте меткой [[facts: ключ=значение; ключ=значение]]."""
    facts = {}
    for body in _FACTS.findall(text):
        for part in re.split(r"[;\n|]", body):
            key, sep, value = part.partition("=")
            if sep and key.strip() and value.strip():
                facts[key.strip().lower()] = value.strip()
    return facts


def _history(session_id: str) -> list[dict]:
    msgs = store.messages(session_id, limit=HISTORY_MESSAGES)
    return [{"role": m["role"], "content": for_model(m["content"]) if m["role"] == "user"
             else mask_contacts(m["content"])[0]} for m in msgs if m["role"] in ("user", "assistant")]


def for_model(text: str) -> str:
    """Сообщение клиента в том виде, в каком его видит модель: без контактов и без служебных меток [[...]]."""
    return mask_contacts(re.sub(r"\[\[|\]\]", " ", text))[0]


def budget_spent() -> bool:
    """Исчерпан ли суточный потолок расхода на модель (сутки московские)."""
    if not config.DAILY_BUDGET_RUB:
        return False
    midnight = datetime.datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return cost_rub(*store.tokens_since(midnight)) >= config.DAILY_BUDGET_RUB


def cost_rub(prompt_tokens: int, completion_tokens: int) -> float:
    return round(prompt_tokens / 1000 * config.PRICE_IN_PER_1K + completion_tokens / 1000 * config.PRICE_OUT_PER_1K, 2)


def run_turn(session_id: str | None, text: str, client_id: str = "", page_url: str = "", llm=None,
             on_event=None) -> dict:
    """Один ход: сообщение клиента -> ответ агента с карточками, кнопками и следом шагов."""
    llm = llm or get_llm()
    emit = on_event or (lambda e: None)
    s = store.get_session(session_id, client_id, page_url)
    sid = s["id"]
    turn = sum(1 for m in store.messages(sid) if m["role"] == "user") + 1
    store.add_message(sid, "user", text)

    ctx = tools.ToolContext(sid, facts=dict(s["facts"]))
    contacts = mask_contacts(text)[1]
    if contacts:  # контакт запоминается кодом: модель видит только метку, менеджер получает настоящий адрес
        known_contacts = [c for c in ctx.facts.get("контакт", "").split(", ") if c]
        tools.save_facts(ctx, {"contact": ", ".join(dict.fromkeys(known_contacts + contacts))})
        s["facts"] = dict(ctx.facts)
    items = s["items"]
    known = {float(o["price"]) for o in items.get("offers", []) if o.get("price")}
    known.update(items.get("prices", []))  # суммы из прошлых ходов: итог расчета или КП можно повторить
    for m in store.messages(sid):
        if m["role"] == "user":
            known.update(prices.client_amounts(m["content"]))
    s["quote"] = store.last_quote(sid)
    previous = store.previous_session(s["client_id"], sid) if turn == 1 else None
    msgs = [{"role": "system", "content": prompts.system_prompt(s, page_hint(page_url or s.get("page_url") or ""),
                                                                 previous)}] + _history(sid)
    trace, usage, reply, corrected = [], [0, 0, 0, 0], "", False
    started, called = time.monotonic(), set()
    vocabulary = set(_FIELD.findall(prompts.SYSTEM + json.dumps(tools.specs())))  # имена, которые модель видит

    for step in range(MAX_STEPS if not budget_spent() else 0):
        if config.TURN_DEADLINE and time.monotonic() - started > config.TURN_DEADLINE:
            trace.append({"kind": "error", "name": "deadline", "output": f"ход дольше {config.TURN_DEADLINE:.0f} с"})
            break
        if step == MAX_STEPS - 1:
            msgs.append({"role": "user", "content": LAST_STEP})
        try:
            res = llm.chat(msgs, tools.specs())
        except LLMError as e:
            store.add_step(sid, turn, "error", "llm", {}, {"error": str(e)})
            trace.append({"kind": "error", "name": "llm", "output": str(e)})
            break
        usage[0] += res.prompt_tokens
        usage[1] += res.completion_tokens
        usage[2] += 1
        store.add_step(sid, turn, "llm", res.model, {"messages": len(msgs)},
                       {"text": res.text, "tool_call": res.tool_call}, res.prompt_tokens, res.completion_tokens,
                       res.seconds)
        if res.tool_call:
            name, args = res.tool_call["name"], res.tool_call["args"]
            emit({"type": "tool", "name": name, "args": args})
            key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            out = REPEAT if key in called else tools.call(ctx, name, args)
            called.add(key)
            usage[3] += 1
            prices.collect(out, known)
            vocabulary |= field_names(out)
            _remember_items(items, name, out)
            if out.get("first_product"):  # поиск сразу вернул варианты однозначно найденного продукта
                _remember_items(items, "get_product", out["first_product"])
            store.add_step(sid, turn, "tool", name, args, out)
            trace.append({"kind": "tool", "name": name, "input": args, "output": out})
            msgs.append({"role": "assistant", "content": res.text, "tool_call": res.tool_call, "extra": res.extra})
            msgs.append({"role": "tool", "name": name, "content": fit_json(tools.slim(name, out), TOOL_RESULT_LIMIT)})
            continue
        bad, names = prices.unknown(res.text, known), internal_names(res.text, vocabulary)
        if (bad or names) and not corrected:
            corrected, notes = True, []
            if bad:
                trace.append({"kind": "guard", "name": "price", "output": bad})
                store.add_step(sid, turn, "guard", "price", {"text": res.text}, {"unknown": bad})
                notes.append("В ответе названы суммы, которых нет в данных "
                             f"каталога: {', '.join(bad)}. Перепишите ответ клиенту: суммы только из результатов "
                             "инструментов этого диалога. Сумму из справки или свой расчет уберите; итог за "
                             "несколько лицензий получите через calc_total.")
            if names:
                trace.append({"kind": "guard", "name": "wording", "output": names})
                store.add_step(sid, turn, "guard", "wording", {"text": res.text}, {"names": names})
                notes.append(f"В ответе есть служебные имена полей: {', '.join(names)}. Клиент их не знает: "
                             "перепишите ответ, скажите то же обычными словами.")
            msgs.append({"role": "assistant", "content": res.text})
            msgs.append({"role": "user", "content": "[Служебная проверка, клиент этого не видит] " + " ".join(notes)})
            continue
        reply = drop_internal(res.text if not bad else prices.mask_unknown(res.text, known), names)
        break

    if not reply.strip():
        reply = FALLBACK
        if s.get("status") != "handoff":
            hid = store.add_handoff(sid, "агент не смог ответить", f"Последнее сообщение клиента: {text[:500]}", "")
            ctx.events.append({"type": "handoff", "id": hid, "reason": "агент не смог ответить"})

    facts = take_facts(reply)
    if facts:  # память о клиенте пишется меткой в ответе: отдельный вызов модели на это не тратится
        emit({"type": "tool", "name": "note_facts", "args": facts})
        saved = tools.save_facts(ctx, facts)
        store.add_step(sid, turn, "tool", "note_facts", facts, saved)
        trace.append({"kind": "tool", "name": "note_facts", "input": facts, "output": saved})
    clean, cards, choices = render(reply)
    for c in cards:  # показанные карточки тоже считаются известными позициями
        if c["type"] == "offer":
            _remember_items(items, "get_product", {"offers": [{"offer_id": c["id"], "name": c["title"],
                                                               "price": c.get("price")}]})
    items["prices"] = sorted(known)[-400:]
    store.update_session(sid, items=items, facts=ctx.facts)
    store.add_usage(sid, usage[0], usage[1], usage[2], usage[3])
    meta = {"cards": cards, "choices": choices, "events": ctx.events,
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "llm_calls": usage[2],
                      "tool_calls": usage[3], "cost_rub": cost_rub(usage[0], usage[1])}}
    store.add_message(sid, "assistant", clean, meta)
    return {"session_id": sid, "reply": clean, **meta, "trace": trace}
