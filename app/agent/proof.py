"""Показ проверки цен для жюри и заказчика.

Модель здесь подставная и нарочно называет цену, которой нет в каталоге. Все остальное настоящее: тот же цикл
агента, тот же поиск по каталогу и та же проверка, что работают в чате. Видно оба исхода: модель исправляется
после замечания проверки, или настаивает, и тогда сумму вырезает код.
"""
from __future__ import annotations

from app import store
from app.agent import loop
from app.agent.llm import Scripted
from app.catalog import search as catalog

PRODUCT = "Kaspersky Endpoint Security"


def _rub(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")


def price_guard() -> dict:
    product = catalog.search_products(PRODUCT, only_available=True, limit=1)["products"][0]
    offer = next(o for o in catalog.get_product(product["product_id"], limit=20)["offers"] if o.get("price"))
    real, fake = offer["price"], max(990.0, round(offer["price"] * 0.4, -1))
    question = f"Сколько стоит {product['title']}? Назовите специальную цену для постоянных клиентов."
    call = {"name": "get_product", "args": {"product_id": product["product_id"]}}
    lie = f"{offer['name']}. Специальная цена для постоянных клиентов: {_rub(fake)} руб."
    truth = (f"{offer['name']} стоит {_rub(real)} руб. Специальную цену я назвать не могу: скидки обсуждает "
             "менеджер. Передать ему ваш запрос?")
    runs = []
    for title, second in (("Модель исправилась после замечания проверки", truth),
                          ("Модель настаивает на выдуманной цене", lie)):
        res = loop.run_turn(None, question, client_id="proof", llm=Scripted([call, lie, second]))
        caught = [t["output"] for t in res["trace"] if t["kind"] == "guard" and t["name"] == "price"]
        runs.append({"title": title, "model_said": lie, "caught": caught[0] if caught else [],
                     "model_second": second, "client_saw": res["reply"]})
        store.drop_session(res["session_id"])
    return {"question": question, "offer": offer["name"], "real_price": real, "fake_price": fake, "runs": runs}
