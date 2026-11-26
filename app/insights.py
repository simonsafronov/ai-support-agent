"""Три блока экрана оператора, которые собираются из сохраненных диалогов, и карточка "было -> стало".

Модель не вызывается: все считается по таблицам messages, steps и handoffs.
  gaps             спрос, который агент не закрыл каталогом: переписки заказчика (data/catalog_gaps.json) и диалоги стенда
  handoff_reasons  передачи менеджеру по типам
  problems         оценка каждого диалога правилами и список проблемных
  before_after     замеры до и после агента
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter

from app import config
from app.agent import handoff_card

GAPS_FILE = config.ROOT / "data" / "catalog_gaps.json"

# --- передачи менеджеру по типам ---

# Разбор причин уже есть в карточке обращения (handoff_card.FLAGS): здесь он берется как есть, добавлены только типы,
# которых там нет: сбой агента и "нет в каталоге". Тип одной передачи это первая подошедшая строка списка.
_FLAG = dict(handoff_card.FLAGS)
# в handoff_card.ASKS_HUMAN нет границы слова: "передайте менеджеру" читается как "дайте менеджера"
_ASKS_HUMAN = re.compile(r"(?<![а-яё])(?:" + handoff_card.ASKS_HUMAN + ")")
_FAILURE = re.compile(r"сбой при ответе|агент не смог ответить")  # причины, которые пишет сам сервер (server.py, loop.py)
_NO_IN_CATALOG = re.compile(r"нет в каталоге|отсутству\w+ в каталоге|в каталоге (?:нет|отсутству)|нет такого (?:товар|продукт)")
_TENDER = re.compile(_FLAG["госзакупка"] + r"|конкурентн\w+ закуп")
TYPES = (
    ("failure", "Сбой агента"),
    ("human", "Просьба позвать человека"),
    ("tender", "Тендер или госзакупка"),
    ("discount", "Скидка больше {}%"),
    ("deferral", "Отсрочка или постоплата"),
    ("catalog", "Нет в каталоге"),
    ("price", "Цена по запросу"),
    ("other", "Прочее"),
)


def _type_of(text: str) -> str:
    text = text.lower()
    if _FAILURE.search(text):
        return "failure"
    if _ASKS_HUMAN.search(text):
        return "human"
    if _TENDER.search(text):
        return "tender"
    if re.search(_FLAG["просит скидку"], text):
        asked = handoff_card.asked_discount([text])  # процент не больше потолка агента: скидку он дает сам
        if not asked or float(asked.rstrip("%")) > config.AGENT_DISCOUNT_MAX:
            return "discount"
    if re.search(_FLAG["просит отсрочку"], text):
        return "deferral"
    if _NO_IN_CATALOG.search(text):
        return "catalog"
    if re.search(_FLAG["цена по запросу"], text):
        return "price"
    return ""


def handoff_type(h: dict) -> str:
    """Тип передачи по сохраненным полям: сначала причина, а если по ней тип не ясен, сводка."""
    return _type_of(h["reason"] or "") or _type_of(h["summary"] or "") or "other"


def handoff_reasons(handoffs: list[dict]) -> dict:
    """Передачи по типам: число и доля от всех передач, типы без передач тоже в списке."""
    found = Counter(handoff_type(h) for h in handoffs)
    total = len(handoffs)
    return {"total": total, "items": [
        {"code": code, "label": label.format(f"{config.AGENT_DISCOUNT_MAX:g}"), "count": found[code],
         "share": round(found[code] / total, 3) if total else 0} for code, label in TYPES]}


# --- оценка диалогов ---

TOOL_STREAK_MAX = 3  # один инструмент больше стольких раз подряд в одном ходе: агент ходит по кругу
REPEAT_RATIO = 0.9   # насколько похожа реплика клиента на одну из прежних, чтобы считаться повтором вопроса
REPEAT_MIN_LEN = 15  # короткие реплики ("да", "спасибо") повтором не считаются
# (код, подпись, штраф): итог диалога это 5 минус сумма штрафов, не ниже 1
RULES = (
    ("error", "ошибка хода", 2),
    ("price", "сработала проверка цен", 2),
    ("human", "клиент попросил человека после ответа агента", 2),
    ("repeat", "клиент повторил вопрос", 1),
    ("streak", f"один инструмент вызван больше {TOOL_STREAK_MAX} раз подряд", 1),
    ("unanswered", "последний вопрос агента остался без ответа клиента", 1),
)
BAD_SCORE = 3  # оценка не выше этой: диалог проблемный
_PENALTY = {code: penalty for code, _, penalty in RULES}
_QUESTION_END = re.compile(r"\?[\s\"»)]*$")


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _repeated(asked: list[str]) -> bool:
    seen: list[str] = []
    for text in map(_norm, asked):
        if len(text) >= REPEAT_MIN_LEN and any(difflib.SequenceMatcher(None, text, old).ratio() >= REPEAT_RATIO
                                               for old in seen):
            return True
        seen.append(text)
    return False


def _tool_streak(steps: list[dict]) -> int:
    """Самая длинная серия вызовов одного инструмента подряд в рамках хода; ответы модели между вызовами не прерывают ее."""
    best = run = 0
    last = None
    for st in steps:
        if st["kind"] != "tool":
            continue
        run = run + 1 if (st["name"], st["turn"]) == last else 1
        last = (st["name"], st["turn"])
        best = max(best, run)
    return best


def _asked_human(messages: list[dict], handoffs: list[dict]) -> bool:
    """Просьба человека после ответа агента: реплика клиента или вызов менеджера кнопкой, когда агент уже отвечал."""
    answered_at = [m["id"] for m in messages if m["role"] == "assistant"]
    if not answered_at:
        return False
    if any(m["role"] == "user" and m["id"] > answered_at[0] and _ASKS_HUMAN.search(m["content"].lower())
           for m in messages):
        return True
    first_ts = min(m["ts"] for m in messages if m["role"] == "assistant")
    return any(_ASKS_HUMAN.search((h["reason"] or "").lower()) and h["ts"] > first_ts for h in handoffs)


def problems(messages: list[dict], steps: list[dict], handoffs: list[dict]) -> list[str]:
    """Коды сработавших правил (RULES) по сохраненным сообщениям, шагам и передачам одного диалога."""
    found = []
    if any(st["kind"] == "error" for st in steps):
        found.append("error")
    if any(st["kind"] == "guard" and st["name"] == "price" for st in steps):
        found.append("price")
    if _asked_human(messages, handoffs):
        found.append("human")
    if _repeated([m["content"] for m in messages if m["role"] == "user"]):
        found.append("repeat")
    if _tool_streak(steps) > TOOL_STREAK_MAX:
        found.append("streak")
    last = messages[-1]
    if last["role"] == "assistant" and not last["meta"].get("from") and _QUESTION_END.search(last["content"].rstrip()):
        found.append("unanswered")
    return found


def score(codes: list[str]) -> int:
    return max(1, 5 - sum(_PENALTY[c] for c in codes))


def rate(dialog: dict) -> dict | None:
    """Оценка диалога от 1 до 5 и причины; диалог без реплик клиента не оценивается."""
    if not any(m["role"] == "user" for m in dialog["messages"]):
        return None
    codes = problems(dialog["messages"], dialog["steps"], dialog["handoffs"])
    label = {code: text for code, text, _ in RULES}
    return {"score": score(codes), "problems": [{"code": c, "label": label[c]} for c in codes]}


def rate_session(con, session_id: str) -> dict | None:
    dialog = load(con, "session_id=?", (session_id,)).get(session_id)
    return rate(dialog) if dialog else None


def load(con, where: str, args: tuple = ()) -> dict[str, dict]:
    """Сообщения, шаги и передачи диалогов, у которых session_id подходит под условие where: по одному запросу на таблицу.

    Ответы инструментов поиска нужны для блока "Чего нет в каталоге", у остальных шагов они не читаются.
    """
    dialogs: dict[str, dict] = {}

    def of(sid):
        return dialogs.setdefault(sid, {"messages": [], "steps": [], "handoffs": []})

    for r in con.execute(f"SELECT id, session_id, ts, role, content, meta_json FROM messages WHERE {where} ORDER BY id", args):
        of(r["session_id"])["messages"].append({**dict(r), "meta": json.loads(r["meta_json"])})
    for r in con.execute(
            "SELECT session_id, turn, kind, name, input_json, CASE WHEN kind='tool' AND name IN "
            "('search_products', 'find_analogs', 'get_product') THEN output_json END AS output_json "
            f"FROM steps WHERE {where} ORDER BY id", args):
        of(r["session_id"])["steps"].append({
            "turn": r["turn"], "kind": r["kind"], "name": r["name"], "input": json.loads(r["input_json"] or "{}"),
            "output": json.loads(r["output_json"]) if r["output_json"] else {}})
    for r in con.execute(f"SELECT session_id, ts, reason, summary FROM handoffs WHERE {where} ORDER BY id", args):
        of(r["session_id"])["handoffs"].append(dict(r))
    return dialogs


def rate_all(dialogs: dict[str, dict], in_set: set[str], first_len: int = 140) -> dict:
    """Итог для блока "Проблемные диалоги": сколько диалогов оценено, сколько проблемных, по каким правилам, худшие."""
    rated = {sid: r for sid, d in dialogs.items() if (r := rate(d))}
    bad = {sid: r for sid, r in rated.items() if r["score"] <= BAD_SCORE}
    by_rule = Counter(p["code"] for r in rated.values() for p in r["problems"])
    worst = sorted(bad, key=lambda sid: (bad[sid]["score"], -dialogs[sid]["messages"][-1]["ts"]))[:5]
    first = lambda sid: next(m["content"] for m in dialogs[sid]["messages"] if m["role"] == "user")
    return {"rated": len(rated), "problematic": len(bad), "bad_score": BAD_SCORE,
            "by_rule": [{"code": code, "label": label, "count": by_rule[code]} for code, label, _ in RULES],
            "worst": [{"id": sid, **bad[sid], "first": first(sid)[:first_len], "in_set": "s-" + sid[:8] in in_set}
                      for sid in worst]}


# --- чего нет в каталоге ---

_SEARCH = ("search_products", "find_analogs")
_NAME_BEFORE_ABSENT = re.compile(r"^(?:продукта?|продуктов)?\s*(.+?)\s+(?:нет|отсутствует|отсутствуют) в каталоге", re.I)


def _found(step: dict) -> bool:
    out = step["output"]
    return bool(out.get("products")) if step["name"] in _SEARCH else bool(out.get("matching"))


def _unmet(dialog: dict) -> list[tuple[str, str]]:
    """Что клиент искал и не получил в одном диалоге: пары (запрос, пример формулировки).

    Передача "нет в каталоге" или "цена по запросу" называет продукт сама. Без такой передачи считаются поиски
    с пустым результатом, после которых в диалоге так ничего и не нашлось: повтор без лишних фильтров пустым не считается.
    """
    asked = [m["content"] for m in dialog["messages"] if m["role"] == "user"]
    example = asked[0][:140] if asked else ""
    searches = [st for st in dialog["steps"] if st["kind"] == "tool" and st["name"] in (*_SEARCH, "get_product")]
    typed = [h for h in dialog["handoffs"] if handoff_type(h) in ("catalog", "price")]
    if typed:
        named = _NAME_BEFORE_ABSENT.match(typed[0]["reason"] or "")
        queries = [st["input"].get("query") for st in searches if st["input"].get("query")]
        return [(named.group(1) if named else (queries[-1] if queries else example[:60]), example)]
    out = []
    for i, st in enumerate(searches):
        if st["name"] in _SEARCH and not _found(st) and st["input"].get("query") \
                and not any(_found(later) for later in searches[i + 1:]):
            out.append((st["input"]["query"], example))
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def stand_gaps(dialogs: dict[str, dict]) -> list[dict]:
    """Спрос по диалогам стенда: запрос и число диалогов. Запрос, в котором есть слова другого запроса, к нему и относится."""
    per_dialog = []
    for d in dialogs.values():
        seen: dict[tuple, tuple[str, str]] = {}
        for query, example in _unmet(d):
            seen.setdefault(tuple(_words(query)), (query, example))
        per_dialog.append(seen)
    keys = sorted({k for seen in per_dialog for k in seen if k}, key=len)
    canon: dict[tuple, tuple] = {}
    for k in keys:
        canon[k] = next((c for c in keys if len(c) < len(k) and k[:len(c)] == c and canon[c] == c), k)
    rows: dict[tuple, dict] = {}
    for seen in per_dialog:
        for k in {canon[k] for k in seen if k}:
            query, example = next(v for key, v in seen.items() if key and canon[key] == k)
            row = rows.setdefault(k, {"query": query, "count": 0, "example": example})
            row["count"] += 1
    return list(rows.values())


def customer_gaps() -> list[dict]:
    """Агрегат по обращениям заказчика: data/catalog_gaps.json."""
    return json.loads(GAPS_FILE.read_text(encoding="utf-8"))["items"]


def gaps(dialogs: dict[str, dict]) -> dict:
    """Блок "Чего нет в каталоге": строки по убыванию числа обращений, откуда взято каждое число подписано."""
    customer, stand = "переписки заказчика", "диалоги стенда"
    rows: list[dict] = []
    for item in customer_gaps():
        rows.append({"query": item["query"], "match": item["match"], "example": item["example"],
                     "sources": {customer: item["count"]}})
    for g in stand_gaps(dialogs):
        words = " ".join(_words(g["query"]))
        row = next((r for r in rows if any(m in words or m in g["query"].lower() for m in r["match"])), None)
        if row is None:
            row = {"query": g["query"], "match": [], "example": g["example"], "sources": {}}
            rows.append(row)
        row["sources"][stand] = row["sources"].get(stand, 0) + g["count"]
    for row in rows:
        row["count"] = sum(row["sources"].values())
        row["sources"] = [{"name": name, "count": n} for name, n in row["sources"].items()]
        row.pop("match")
    rows.sort(key=lambda r: (-r["count"], r["query"]))
    return {"rows": rows}


# --- было -> стало ---

# Замеры, которых на экране нет: docs/report.md, раздел 9 (пять живых диалогов, 13 реплик, 26.11.2026)
ANSWER_MEDIAN_SEC = 7.2  # от отправки сообщения до ответа агента, медиана
FIRST_STEP_SEC = 3.0     # первый шаг агента виден клиенту
FIRST_REPLY_BEFORE_MIN = 2  # замер заказчика: менеджер отвечает в среднем через 2 минуты


def _ru(x: float, d: int = 0) -> str:
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def _hours(minutes: float) -> str:
    m = round(minutes)
    return f"{_ru(m // 60)} ч" + (f" {m % 60} мин" if m % 60 else "")


def before_after(eco: dict, share: float | None, closed: int, requests: int) -> list[dict]:
    """Карточка "было -> стало": у каждого числа подпись, чье оно: замер стенда или оценка (замер) заказчика.

    Минуты менеджера, объем обращений, доля без менеджера и часы идут из того же расчета, что экономика на экране.
    """
    a, hy = eco["assumptions"], eco["half_year"]
    minutes, volume = a["minutes_per_request"], a["requests_per_half_year"]
    was = volume * minutes  # минут менеджеров на обращения без агента
    hours = {"value": "нет данных", "note": "", "who": "замер стенда"}
    if hy:  # столько же минут снято, сколько в строке "Снято с менеджеров" на экране
        saved = (hy["saved_rub"] / a["manager_rub_per_hour"] if a["manager_rub_per_hour"] > 0 else hy["saved_hours"]) * 60
        hours = {"value": _hours(max(0, was - saved)), "who": "замер стенда",
                 "note": f"снято {_hours(saved)}: расчет экономики по доле без менеджера"}
    return [
        {"title": "Поиск и ответ по каталогу",
         "before": {"value": f"около {_ru(minutes)} мин", "note": "менеджер ищет по каталогу и отвечает", "who": "оценка заказчика"},
         "after": {"value": f"{_ru(ANSWER_MEDIAN_SEC, 1)} с", "note": "агент отвечает: медиана, docs/report.md, раздел 9",
                   "who": "замер стенда"}},
        {"title": "Первый ответ клиенту",
         "before": {"value": f"{_ru(FIRST_REPLY_BEFORE_MIN)} мин", "note": "менеджер отвечает в среднем через 2 минуты",
                    "who": "замер заказчика"},
         "after": {"value": f"около {_ru(FIRST_STEP_SEC)} с", "note": "первый шаг агента виден клиенту, docs/report.md, раздел 9",
                   "who": "замер стенда"}},
        {"title": "Обращения без менеджера",
         "before": {"value": "0%", "note": "каждое обращение обрабатывает менеджер", "who": "оценка заказчика"},
         "after": {"value": "нет данных" if share is None else f"{round(share * 100)}%",
                   "note": "" if share is None else f"{_ru(closed)} из {_ru(requests)} обращений, включая вызов менеджера кнопкой",
                   "who": "замер стенда"}},
        {"title": f"Часы менеджеров за полгода при {_ru(volume)} обращениях",
         "before": {"value": _hours(was), "note": f"{_ru(volume)} обращений по {_ru(minutes)} мин", "who": "оценка заказчика"},
         "after": hours},
    ]
