"""Проверка цен и скидок на настоящих ответах агента: сколько раз она срабатывает на диалогах стенда и прогона.

Правка в app/agent/prices.py может начать резать честные ответы. Тесты на придуманных фразах этого не покажут,
поэтому после каждой правки проверка прогоняется по накопленным диалогам. База открывается только на чтение.
Для каждого текста восстанавливается то, что знал цикл агента в момент проверки: суммы, проценты и позиции из ответов
инструментов до этого места, суммы из слов клиента, цены показанных карточек.
Отдельно считается привязка цены к позиции: сколько пар "позиция, сумма" удалось проверить и сколько сумм
названо не у своей позиции.

Проверяются три набора текстов:
  ответ    сообщение, которое ушло клиенту (после проверки и вырезки): срабатываний быть не должно;
  модель   текст модели в том виде, в каком его видит проверка в цикле: со служебными метками, до вырезки;
  черновик текст модели, который проверка в свое время вернула на переписывание (шаги guard).

Запуск: python -m eval.guard_corpus data/db/app.sqlite eval/runs/full8 [--json файл]
Каталог обходится целиком: берется каждый app.sqlite внутри.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from app.agent import prices
from app.agent.loop import PERCENT_SOURCES


def databases(paths: list[str]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        out += sorted(p.rglob("app.sqlite")) if p.is_dir() else [p]
    return out


def read_only(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def texts(con: sqlite3.Connection, sid: str):
    """Тексты диалога по порядку: (ход, вид, текст, known, said, percents, positions) на момент проверки текста."""
    known, percents, said, positions = set(), set(), set(), {}
    rows = [("m", r["id"], r["ts"], dict(r)) for r in con.execute(
        "SELECT id, ts, role, content, meta_json FROM messages WHERE session_id=? ORDER BY id", (sid,))]
    rows += [("s", r["id"], r["ts"], dict(r)) for r in con.execute(
        "SELECT id, ts, turn, kind, name, input_json, output_json FROM steps WHERE session_id=? ORDER BY id", (sid,))]
    rows.sort(key=lambda r: (r[2], r[0] == "m" and r[3]["role"] == "assistant", r[1]))  # ответ хода после его шагов
    turn, cards = 0, set()
    for table, _, _, row in rows:
        if table == "m" and row["role"] == "user":
            turn += 1
            known |= cards  # цены карточек прошлого ответа: цикл помнит их как известные позиции
            said |= prices.client_amounts(row["content"])
        elif table == "m" and row["role"] == "assistant":
            yield turn, "ответ", row["content"], set(known), set(said), set(percents), positions
            meta = json.loads(row["meta_json"] or "{}")
            cards = {float(c["price"]) for c in meta.get("cards", []) if c.get("type") == "offer" and c.get("price")}
        elif table == "s" and row["kind"] == "tool":
            out = json.loads(row["output_json"] or "null")
            prices.collect(out, known)
            prices.collect_positions(out, positions)
            if row["name"] in PERCENT_SOURCES:
                prices.collect_percents(out, percents)
        elif table == "s" and row["kind"] == "llm":
            out = json.loads(row["output_json"] or "{}") or {}
            if out.get("text") and not out.get("tool_call"):
                yield turn, "модель", out["text"], set(known), set(said), set(percents), positions
        elif table == "s" and row["kind"] == "guard":
            draft = (json.loads(row["input_json"] or "{}") or {}).get("text")
            if draft:
                yield turn, "черновик", draft, set(known), set(said), set(percents), positions


def scan(paths: list[str], where: str = "1=1", guard=prices) -> dict:
    """Срабатывания проверки по всем диалогам баз. guard: модуль проверки (для сравнения версий)."""
    total = {"баз": 0, "диалогов": 0, "ответ": 0, "модель": 0, "черновик": 0}
    hits, pairs = [], {kind: {prices.OWN: 0, prices.FOREIGN: 0, prices.OTHER: 0} for kind in ("ответ", "модель")}
    for path in databases(paths):
        con = read_only(path)
        total["баз"] += 1
        for s in con.execute(f"SELECT id, client_id FROM sessions WHERE {where} ORDER BY created_at"):
            total["диалогов"] += 1
            seen = set()
            for turn, kind, text, known, said, percents, positions in texts(con, s["id"]):
                if (kind, text) in seen:  # один черновик записан по разу на каждую проверку, которая его вернула
                    continue
                seen.add((kind, text))
                total[kind] += 1
                where_ = {"база": str(path), "клиент": s["client_id"] or s["id"], "ход": turn, "вид": kind}
                for a, b in guard._bad(text, known, said):
                    hits.append({**where_, "проверка": "цена", "найдено": text[a:b].strip(),
                                 "фраза": guard._sentence_at(text, a).strip()})
                for a, b, _, verdict in guard.price_pairs(text, positions) if hasattr(guard, "price_pairs") else []:
                    if kind in pairs:
                        pairs[kind][verdict] += 1
                    if verdict == guard.FOREIGN:
                        hits.append({**where_, "проверка": "привязка", "найдено": text[a:b].strip(),
                                     "фраза": guard._sentence_at(text, a).strip()})
                for phrase in guard.discount_over_limit([text], percents):
                    hits.append({**where_, "проверка": "скидка", "найдено": phrase, "фраза": phrase})
        con.close()
    return {"всего": total, "пары_позиция_сумма": pairs, "срабатывания": hits}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="app.sqlite или каталог прогона")
    ap.add_argument("--where", default="1=1", help="условие на sessions, например \"client_id LIKE 'eval-c%%'\"")
    ap.add_argument("--json", default="", help="куда записать срабатывания списком")
    args = ap.parse_args()
    res = scan(args.paths, args.where)
    t, hits = res["всего"], res["срабатывания"]
    print(f"Баз {t['баз']}, диалогов {t['диалогов']}, ответов клиенту {t['ответ']}, текстов модели {t['модель']}, "
          f"из них возвращено проверкой {t['черновик']}.")
    for kind in ("ответ", "модель", "черновик"):
        for check in ("цена", "скидка", "привязка"):
            part = [h for h in hits if h["вид"] == kind and h["проверка"] == check]
            print(f"  {kind}, {check}: {len(part)}")
    for kind, got in res["пары_позиция_сумма"].items():
        print(f"  привязка, {kind}: пар \"позиция, сумма\" {sum(got.values())}, из них своя {got[prices.OWN]}, "
              f"чужая {got[prices.FOREIGN]}, не определено {got[prices.OTHER]}")
    for h in hits:
        print(f"- {h['клиент']} ход {h['ход']} {h['вид']} [{h['проверка']}] {h['найдено']!r}\n    {h['фраза'][:400]}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
