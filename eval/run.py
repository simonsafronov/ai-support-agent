"""Прогон проверочных диалогов через агента и отчет с метриками.

Запуск:  python -m eval.run [--only c01,c19] [--limit 10] [--out eval/runs]
Сводка:  python -m eval.run --merge eval/runs/a eval/runs/b   (общий отчет по нескольким прогонам,
         проверки пересчитываются по сохраненной переписке)

Каждый кейс из cases.jsonl проигрывается как отдельный диалог: реплики клиента идут по порядку,
ответы агента и след шагов сохраняются. Проверки ниже выполняются кодом, без участия модели:
они ловят то, что можно установить однозначно. Смысловые требования (ответил ли на все вопросы,
не обещал ли наличие) в отчете помечены как проверяемые человеком по сохраненной переписке.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from pathlib import Path

from app import config, store
from app.agent import loop

ROOT = Path(__file__).parent
ASK = {  # о чем агент должен спросить -> по каким словам это видно в его вопросе
    "users": r"пользовател|рабочих мест|сотрудник|устройств|узл|компьютер|количеств|сколько",
    "term": r"срок|на год|года|лет|месяц|бессрочн",
    "license_type": r"продлен|нов[аоу][яйю]|подписк|первичн|тип лицензи",
    "org_type": r"организаци|юрлиц|юридическ|физлиц|физическ|коммерческ|государствен|образовательн|частн",
    "version": r"верси|редакци|выпуск",
    "platform": r"платформ|операционн|windows|linux|macos|\bос\b",
    "task": r"задач|для чего|что именно|какие функции|что нужно|что требуется",
    "budget": r"бюджет|ориентир по цене|сумм",
    "infrastructure": r"инфраструктур|сервер|облак|операционн|почтов|windows|linux|где развернут",
    "contact": r"почт|телефон|контакт|e-mail|email|связаться",
}
DISCOUNT = re.compile(r"(дадим|дам|сделаем|сделаю|предоставим|предоставлю|получите|гарантиру\w+)\s+(вам\s+)?скидк", re.I)


def play(case: dict) -> dict:
    """Проигрывает один кейс и возвращает переписку со следом шагов."""
    sid, turns, started = None, [], time.time()
    for text in case["turns"]:
        res = loop.run_turn(sid, text, client_id="eval-" + case["id"])
        sid = res["session_id"]
        turns.append({"user": text, "reply": res["reply"], "cards": res["cards"], "choices": res["choices"],
                      "events": res["events"], "usage": res["usage"],
                      "tools": [{"name": t["name"], "input": t.get("input"), "output": t.get("output")}
                                for t in res["trace"] if t["kind"] == "tool"],
                      "guards": [t for t in res["trace"] if t["kind"] in ("guard", "error")]})
    seconds = store.db().execute("SELECT SUM(seconds) FROM steps WHERE session_id=? AND kind='llm'",
                                 (sid,)).fetchone()[0] or 0.0
    return {"id": case["id"], "session_id": sid, "turns": turns, "model_seconds": round(seconds, 1),
            "wall_seconds": round(time.time() - started, 1)}


def check(case: dict, run: dict) -> dict:
    """Проверки кодом. Возвращает {название: True/False/None}; None = к этому кейсу не относится."""
    exp, turns = case["expect"], run["turns"]
    replies = [t["reply"] for t in turns]
    low = "\n".join(replies).lower()
    tools = [x["name"] for t in turns for x in t["tools"]]
    events = [e["type"] for t in turns for e in t["events"]]
    out: dict[str, bool | None] = {}

    out["ответил_сам"] = all(r != loop.FALLBACK for r in replies)
    out["нужный_инструмент"] = any(t in tools for t in exp["tools_any"]) if exp["tools_any"] else None
    out["упомянул_ожидаемое"] = any(m in low for m in exp["mention_any"]) if exp["mention_any"] else None
    out["передача_менеджеру"] = ("handoff" in events) if exp["handoff"] else ("handoff" not in events)
    out["черновик_кп"] = ("quote" in events) if exp["quote"] else None

    # цены: любая сумма в ответах должна быть в данных инструментов этого диалога или в словах клиента
    known: set = set()
    for t in turns:
        known.update(round(v) for v in map(loop._num, loop._NUMBER.findall(t["user"])) if v)
        for x in t["tools"]:
            loop._collect_prices(x["output"], known)
    bad = [p for r in replies for p in loop.unknown_prices(r, known)]
    out["цены_из_каталога"] = not bad

    ask = [a for a in exp["ask_about"] if a in ASK]
    if ask:  # в первом ответе должен быть вопрос хотя бы об одном недостающем параметре
        first = replies[0].lower()
        out["уточнил_недостающее"] = "?" in first and any(re.search(ASK[a], first) for a in ask)
    else:
        out["уточнил_недостающее"] = None
    if "more_than_one_question_per_message" in exp["must_not"]:
        out["один_вопрос_за_сообщение"] = all(r.count("?") <= 1 for r in replies)
    if "promise_discount" in exp["must_not"]:
        out["не_обещал_скидку"] = not DISCOUNT.search(low)
    out["_плохие_цены"] = bad  # type: ignore[assignment]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="id кейсов через запятую")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "runs"))
    ap.add_argument("--merge", nargs="+", default=[], help="каталоги прогонов: собрать общий отчет")
    args = ap.parse_args()

    cases = [json.loads(line) for line in (ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if args.merge:
        return merge(cases, args.merge, Path(args.out))
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only.split(","))]
    if args.limit:
        cases = cases[:args.limit]

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    config.APP_DB = out_dir / "app.sqlite"   # прогон не смешивается с диалогами стенда
    config.QUOTES_DIR = out_dir / "quotes"
    store._local.con = None

    rows = []
    try:
        with (out_dir / "transcripts.jsonl").open("w", encoding="utf-8") as f:
            for i, case in enumerate(cases, 1):
                row = judge(case, play(case))
                rows.append(row)
                f.write(line(row) + "\n")
                f.flush()
                print(f"[{i}/{len(cases)}] {case['id']} {case['scenario']}: {'ok' if row['passed'] else 'FAIL'} "
                      f"{[k for k, v in row['checks'].items() if v is False]}", flush=True)
    finally:
        if config.LLM_PROVIDER == "bridge":  # исполнителю на другом конце моста: запросов больше не будет
            (Path(config.BRIDGE_DIR) / "DONE").touch()

    report(rows, out_dir)
    print("Отчет:", out_dir / "report.md")


def judge(case: dict, run: dict) -> dict:
    res = check(case, run)
    bad = res.pop("_плохие_цены")
    return {"case": case, "run": run, "checks": res, "passed": all(v for v in res.values() if v is not None),
            "bad_prices": bad}


def line(row: dict) -> str:
    case = row["case"]
    return json.dumps({"scenario": case["scenario"], "passed": row["passed"], "checks": row["checks"],
                       "bad_prices": row["bad_prices"], **row["run"], "note": case["note"]}, ensure_ascii=False)


def merge(cases: list[dict], dirs: list[str], out: Path):
    """Общий отчет по нескольким прогонам; у повторно прогнанного кейса берется последний результат."""
    by_id, runs = {c["id"]: c for c in cases}, {}
    for d in dirs:
        for f in sorted(Path(d).rglob("transcripts.jsonl")):
            for raw in f.read_text(encoding="utf-8").splitlines():
                run = json.loads(raw)
                runs[run["id"]] = {k: run[k] for k in ("id", "session_id", "turns", "model_seconds", "wall_seconds")}
    rows = [judge(by_id[i], runs[i]) for i in sorted(runs)]
    out_dir = out / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcripts.jsonl").write_text("\n".join(line(r) for r in rows) + "\n", encoding="utf-8")
    report(rows, out_dir)
    print("Отчет:", out_dir / "report.md")


def report(rows: list[dict], out_dir: Path):
    n = len(rows)
    turns = [t for r in rows for t in r["run"]["turns"]]
    tok_in = sum(t["usage"]["prompt_tokens"] for t in turns)
    tok_out = sum(t["usage"]["completion_tokens"] for t in turns)
    cost = loop.cost_rub(tok_in, tok_out)
    per_turn_tokens = [t["usage"]["prompt_tokens"] + t["usage"]["completion_tokens"] for t in turns]
    secs = [r["run"]["model_seconds"] / max(1, len(r["run"]["turns"])) for r in rows]
    passed = sum(r["passed"] for r in rows)
    no_human = sum(1 for r in rows if r["checks"]["ответил_сам"]
                   and not any(e["type"] == "handoff" for t in r["run"]["turns"] for e in t["events"]))

    lines = [f"# Прогон проверочных диалогов, {time.strftime('%d.%m.%Y %H:%M')}", "",
             f"Модель: {config.LLM_MODEL} ({config.LLM_PROVIDER}). Диалогов: {n}, реплик клиента: {len(turns)}.", "",
             "## Итог", "",
             "| Показатель | Значение |", "|---|---|",
             f"| Диалогов прошло все проверки | {passed} из {n} ({passed / n:.0%}) |",
             f"| Закрыто без менеджера | {no_human} из {n} ({no_human / n:.0%}) |",
             f"| Вызовов модели на реплику клиента | {sum(t['usage']['llm_calls'] for t in turns) / len(turns):.1f} |",
             f"| Вызовов инструментов на реплику | {sum(t['usage']['tool_calls'] for t in turns) / len(turns):.1f} |",
             f"| Токенов на реплику, среднее / медиана | {statistics.mean(per_turn_tokens):.0f} / "
             f"{statistics.median(per_turn_tokens):.0f} |",
             f"| Стоимость реплики (запроса) | {cost / len(turns):.2f} руб. |",
             f"| Стоимость диалога | {cost / n:.2f} руб. |",
             "| Время модели на реплику, медиана | " + ("не измерялось |" if config.LLM_PROVIDER == "bridge"
                                                       else f"{statistics.median(secs):.1f} с |"),
             f"| Срабатываний проверки цен | {sum(1 for t in turns for g in t['guards'] if g['kind'] == 'guard')} |",
             "", "## По проверкам", "", "| Проверка | Прошло | Не прошло | Не относится |", "|---|---|---|---|"]
    names = list(dict.fromkeys(k for r in rows for k in r["checks"]))
    for k in names:
        vals = [r["checks"].get(k) for r in rows]
        lines.append(f"| {k.replace('_', ' ')} | {vals.count(True)} | {vals.count(False)} | {vals.count(None)} |")
    lines += ["", "## По сценариям", "", "| Сценарий | Диалогов | Прошло |", "|---|---|---|"]
    for sc in dict.fromkeys(r["case"]["scenario"] for r in rows):
        part = [r for r in rows if r["case"]["scenario"] == sc]
        lines.append(f"| {sc} | {len(part)} | {sum(r['passed'] for r in part)} |")
    failed = [r for r in rows if not r["passed"]]
    if failed:
        lines += ["", "## Что не прошло", ""]
        for r in failed:
            why = ", ".join(k.replace("_", " ") for k, v in r["checks"].items() if v is False)
            extra = f" Суммы не из каталога: {', '.join(r['bad_prices'])}." if r["bad_prices"] else ""
            lines.append(f"- {r['case']['id']} ({r['case']['scenario']}): {why}.{extra}")
    lines += ["", "Проверки выполняются кодом (eval/run.py). Требования `invent_availability` и `ignore_question` "
              "кодом однозначно не проверяются: их смотрит человек по transcripts.jsonl.", ""]
    if config.LLM_PROVIDER == "bridge":
        lines += ["Прогон шел через мост разработки (LLM_PROVIDER=bridge): токены и стоимость посчитаны оценкой, "
                  "3 знака на токен, по цене из PRICE_IN_PER_1K и PRICE_OUT_PER_1K; время модели не измерялось.", ""]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
