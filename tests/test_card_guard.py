"""Карточка варианта под ответом строится только по вариантам, которые инструменты показали в диалоге."""
from app.agent import loop
from app.agent.llm import Scripted
from app.catalog import search as catalog


def _two_offers():
    rows = catalog.search_products("Kaspersky Endpoint Security", limit=3)["products"]
    product = catalog.get_product(str(rows[0]["product_id"]), limit=2)
    return product["product"], product["offers"]


def test_render_skips_offer_mark_not_seen_in_dialog():
    _, offers = _two_offers()
    shown, other = str(offers[0]["offer_id"]), str(offers[1]["offer_id"])
    text, cards, _ = loop.render(f"Есть вариант [[offer:{shown}]] и еще [[offer:{other}]].", {shown})
    assert [str(c["id"]) for c in cards] == [shown]
    assert "[[" not in text  # метка без карточки из текста все равно убрана


def test_product_id_in_offer_mark_gives_no_foreign_card():
    product, offers = _two_offers()
    reply = f"Подойдет {product['title']} [[offer:{product['product_id']}]]."
    llm = Scripted([{"name": "get_product", "args": {"product_id": str(product["product_id"])}}, reply])
    res = loop.run_turn(None, "Нужен Kaspersky Endpoint Security", client_id="t-card-guard", llm=llm)
    ids = {str(o["offer_id"]) for o in offers}
    assert all(str(c["id"]) in ids or c["type"] != "offer" for c in res["cards"])
