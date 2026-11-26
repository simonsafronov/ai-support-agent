"""Стоимость реплики и диалога по токенам прогона: прайс Anthropic и рублевый посредник, без кэша и с кэшем промпта.

    python -m eval.cost                      # диалоги прогона, загруженные на стенд (data/db/app.sqlite)
    python -m eval.cost eval/runs/.../app.sqlite

Без кэша это замер: токены каждого вызова модели умножаются на цену. Строки с кэшем это расчет по тем же
токенам: при каждом вызове модель заново получает инструкцию и описания инструментов (PREFIX токенов), а внутри
одного хода еще и всю переписку. Чтение из кэша стоит 5% цены входа, запись 125%. В коде кэш не включен.
"""
from __future__ import annotations

import json
import sqlite3
import sys

from app import config

USD = 82.3130   # курс ЦБ на 26.11.2026
PREFIX = 4221   # токенов в неизменной части запроса: инструкция и описания инструментов
PRICES = {      # руб. за 1 млн токенов: вход, запись в кэш, чтение из кэша, выход
    "Прайс Anthropic": (2 * USD, 2.5 * USD, 0.1 * USD, 10 * USD),
    "Российский посредник": (400.0, 500.0, 20.0, 2000.0),  # цена записи в кэш принята как 125% входа
}
RUN = "SELECT id FROM sessions WHERE client_id LIKE 'eval-%'"


def load(db: str) -> tuple[int, dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    only_run = con.execute(f"SELECT COUNT(*) FROM ({RUN})").fetchone()[0] > 0  # на стенде есть и живые диалоги
    where = f"AND session_id IN ({RUN})" if only_run else ""
    turns: dict = {}
    for sid, turn, p, c in con.execute(f"SELECT session_id, turn, prompt_tokens, completion_tokens FROM steps "
                                       f"WHERE kind='llm' {where} ORDER BY id"):
        turns.setdefault((sid, turn), []).append((p, c))
    return len({sid for sid, _ in turns}), turns


def cost(turns: dict, price: tuple, cache: bool) -> float:
    """Рублей за все ходы. С кэшем: первый вызов диалога пишет префикс, дальше читается все отправленное раньше."""
    p_in, p_write, p_read, p_out = price
    total, seen = 0.0, set()
    for (sid, _), calls in turns.items():
        prev = 0
        for k, (p, c) in enumerate(calls):
            total += c * p_out
            if not cache:
                total += p * p_in
            elif k == 0:
                pre = min(PREFIX, p)
                total += pre * (p_read if sid in seen else p_write) + (p - pre) * p_write
            else:
                read = min(prev, p)
                total += read * p_read + (p - read) * p_write
            prev = p
        seen.add(sid)
    return total / 1e6


def report(db: str) -> dict:
    dialogs, turns = load(db)
    calls = [c for t in turns.values() for c in t]
    out = {"dialogs": dialogs, "replies": len(turns), "llm_calls": len(calls),
           "in_per_reply": round(sum(p for p, _ in calls) / len(turns)),
           "out_per_reply": round(sum(c for _, c in calls) / len(turns)),
           "replies_per_dialog": round(len(turns) / dialogs, 2), "calls_per_reply": round(len(calls) / len(turns), 2),
           "prefix_share": round(PREFIX * len(calls) / sum(p for p, _ in calls), 2), "rub": {}}
    for name, price in PRICES.items():
        for cache in (False, True):
            t = cost(turns, price, cache)
            out["rub"][name + (", кэш промпта (расчет)" if cache else "")] = {
                "reply": round(t / len(turns), 2), "dialog": round(t / dialogs, 2), "per_500": round(t / dialogs * 500),
                "per_1000_month": round(t / dialogs * 1000)}
    return out


if __name__ == "__main__":
    print(json.dumps(report(sys.argv[1] if len(sys.argv) > 1 else str(config.APP_DB)), ensure_ascii=False, indent=1))
