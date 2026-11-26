"""Цикл агента: модель выбирает инструмент, получает результат и решает следующий шаг, пока не ответит клиенту.

Вокруг модели стоят проверки кодом: цена в ответе должна быть из каталога, вызовов не больше лимита,
сбой модели или инструмента не оставляет клиента без ответа.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from app import config, store
from app.agent import prompts, tools
from app.agent.llm import LLMError, get_llm
from app.catalog import search as catalog

MAX_STEPS = 8           # вызовов модели на один ход клиента
HISTORY_MESSAGES = 14   # сколько прошлых реплик диалога видит модель
TOOL_RESULT_LIMIT = 6000

FALLBACK = ("Сейчас не получается ответить автоматически. Передаю вопрос менеджеру, он ответит в рабочее время. "
            "Оставьте, пожалуйста, почту или телефон для связи.")

_MONEY = re.compile(r"(\d[\d\s  ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб|р\.)", re.I)
_NUMBER = re.compile(r"\d[\d\s\u00a0\u202f]*(?:[.,]\d{1,2})?")
_MARK = re.compile(r"\[\[(product|offer):\s*([\w-]+)\]\]")
_CHOICES = re.compile(r"\[\[choices:\s*(.+?)\]\]", re.S)


def _num(s: str) -> float | None:
    s = re.sub(r"[\s  ]", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _collect_prices(obj, acc: set):
    """Все числа из полей с ценами и суммами в ответе инструмента."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and re.search(r"price|total|sum|vat|amount", k):
                acc.add(round(float(v)))
            else:
                _collect_prices(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_prices(v, acc)


def unknown_prices(text: str, known: set) -> list[str]:
    """Суммы в тексте ответа, которых нет в данных каталога этого диалога."""
    bad = []
    for m in _MONEY.finditer(text):
        v = _num(m.group(1))
        if v is not None and v >= 1 and round(v) not in known:
            bad.append(m.group(0).strip())
    return bad


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
    if not page_url:
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
    choices = []
    m = _CHOICES.search(text)
    if m:
        choices = [c.strip() for c in m.group(1).split("|") if c.strip()][:5]
    clean = _CHOICES.sub("", _MARK.sub("", text))
    clean = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", clean)).strip()
    return clean, cards, choices


def _history(session_id: str) -> list[dict]:
    msgs = store.messages(session_id, limit=HISTORY_MESSAGES)
    return [{"role": m["role"], "content": m["content"]} for m in msgs if m["role"] in ("user", "assistant")]


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
    items = s["items"]
    known = {round(o["price"]) for o in items.get("offers", []) if o.get("price")}
    # суммы, которые назвал сам клиент (бюджет, цена из чужого счета), агент вправе повторить
    for m in store.messages(sid):
        if m["role"] == "user":
            known.update(round(v) for v in map(_num, _NUMBER.findall(m["content"])) if v)
    previous = store.previous_session(s["client_id"], sid) if turn == 1 else None
    msgs = [{"role": "system", "content": prompts.system_prompt(s, page_hint(page_url or s.get("page_url") or ""),
                                                                 previous)}] + _history(sid)
    trace, usage, reply, corrected = [], [0, 0, 0, 0], "", False

    for _ in range(MAX_STEPS):
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
            out = tools.call(ctx, name, args)
            usage[3] += 1
            _collect_prices(out, known)
            _remember_items(items, name, out)
            store.add_step(sid, turn, "tool", name, args, out)
            trace.append({"kind": "tool", "name": name, "input": args, "output": out})
            msgs.append({"role": "assistant", "content": res.text, "tool_call": res.tool_call, "extra": res.extra})
            msgs.append({"role": "tool", "name": name, "content": fit_json(out, TOOL_RESULT_LIMIT)})
            continue
        bad = unknown_prices(res.text, known)
        if bad and not corrected:
            corrected = True
            trace.append({"kind": "guard", "name": "price", "output": bad})
            store.add_step(sid, turn, "guard", "price", {"text": res.text}, {"unknown": bad})
            msgs.append({"role": "assistant", "content": res.text})
            msgs.append({"role": "user", "content":
                         "[Служебная проверка, клиент этого не видит] В ответе названы суммы, которых нет в данных "
                         f"каталога: {', '.join(bad)}. Перепишите ответ клиенту: цены только из результатов "
                         "инструментов; где цены нет, напишите 'цена по запросу'."})
            continue
        reply = res.text if not bad else _MONEY.sub("цена по запросу", res.text)
        break

    if not reply.strip():
        reply = FALLBACK
        if s.get("status") != "handoff":
            hid = store.add_handoff(sid, "агент не смог ответить", f"Последнее сообщение клиента: {text[:500]}", "")
            ctx.events.append({"type": "handoff", "id": hid, "reason": "агент не смог ответить"})

    clean, cards, choices = render(reply)
    for c in cards:  # показанные карточки тоже считаются известными позициями
        if c["type"] == "offer":
            _remember_items(items, "get_product", {"offers": [{"offer_id": c["id"], "name": c["title"],
                                                               "price": c.get("price")}]})
    store.update_session(sid, items=items, facts=ctx.facts)
    store.add_usage(sid, usage[0], usage[1], usage[2], usage[3])
    meta = {"cards": cards, "choices": choices, "events": ctx.events,
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "llm_calls": usage[2],
                      "tool_calls": usage[3], "cost_rub": cost_rub(usage[0], usage[1])}}
    store.add_message(sid, "assistant", clean, meta)
    return {"session_id": sid, "reply": clean, **meta, "trace": trace}
