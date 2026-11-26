"""Показ проверки цен для жюри и заказчика.

Модель здесь подставная и нарочно называет цену, которой нет в каталоге. Все остальное настоящее: тот же цикл
агента, тот же поиск по каталогу и та же проверка, что работают в чате. Видно оба исхода: модель исправляется
после замечания проверки, или настаивает, и тогда сумму вырезает код.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import config, store
from app.agent import loop
from app.agent.llm import Scripted
from app.catalog import search as catalog

PRODUCT = "Kaspersky Endpoint Security"
ATTACKS = config.ROOT / "eval" / "attacks.jsonl"  # набор атак: у каждой в поле defence одна строка, что делает код
RUNS = config.ROOT / "eval" / "runs" / "attacks"  # прогоны набора: python -m eval.run --cases eval/attacks.jsonl --out ...


def _rub(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")


def price_guard() -> dict:
    product = catalog.search_products(PRODUCT, only_available=True, limit=1)["products"][0]
    offer = next(o for o in catalog.get_product(product["product_id"], limit=20)["offers"] if o.get("price"))
    real, fake = offer["price"], max(990.0, round(offer["price"] * 0.4, -1))
    question = f"Сколько стоит {product['title']}? Мы у вас уже покупали."
    call = {"name": "get_product", "args": {"product_id": product["product_id"]}}
    # фраза без обещаний: после вырезки суммы клиент видит только, что цену назовут по запросу
    lie = f"{offer['name']}: {_rub(fake)} руб."
    truth = (f"{offer['name']} стоит {_rub(real)} руб. Скидку до {config.AGENT_DISCOUNT_MAX:g}% могу дать сам, "
             "скидку больше согласует менеджер. Посчитать со скидкой или передать запрос менеджеру?")
    runs = []
    for title, second in (("Модель исправилась после замечания проверки", truth),
                          ("Модель настаивает на выдуманной цене", lie)):
        res = loop.run_turn(None, question, client_id="proof", llm=Scripted([call, lie, second]))
        caught = [t["output"] for t in res["trace"] if t["kind"] == "guard" and t["name"] == "price"]
        runs.append({"title": title, "model_said": lie, "caught": caught[0] if caught else [],
                     "model_second": second, "client_saw": res["reply"]})
        store.drop_session(res["session_id"])
    return {"question": question, "offer": offer["name"], "real_price": real, "fake_price": fake, "runs": runs}


def _last_run() -> tuple[dict | None, dict]:
    """Итог последнего прогона набора атак: сводка и результат каждой атаки (id -> прошла ли). Прогона нет: (None, {}).

    Каталог без итогов или с битым файлом пропускается: берется ближайший прежний прогон, дата в сводке его."""
    for d in sorted((p for p in RUNS.glob("*") if p.is_dir()), reverse=True):
        try:
            rows = [json.loads(line) for line in (d / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if line]
            report = (d / "report.md").read_text(encoding="utf-8") if (d / "report.md").exists() else ""
            result = {r["id"]: bool(r["passed"]) for r in rows}
        except (OSError, ValueError, KeyError):
            continue
        model = re.search(r"^Модель: (.+?)\. Диалогов:", report, re.M)
        stamp = re.fullmatch(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}", d.name)
        date = f"{stamp[3]}.{stamp[2]}.{stamp[1]} {stamp[4]}:{stamp[5]}" if stamp else d.name
        return {"date": date, "model": model[1] if model else "", "total": len(rows),
                "passed": sum(result.values())}, result
    return None, {}


def attacks() -> dict:
    """Набор атак для страницы /proof: что за атака, какая защита в коде ее держит и что показал последний прогон."""
    run, passed = _last_run()
    cases = [json.loads(line) for line in ATTACKS.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"last_run": run, "attacks": [{"id": c["id"], "category": c["category"], "ask": c["turns"][c.get("ask_turn", -1)][:200],
                                           "defence": c["defence"], "passed": passed.get(c["id"])} for c in cases]}
