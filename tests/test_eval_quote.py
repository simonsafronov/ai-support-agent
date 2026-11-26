"""Проверка состава КП в наборе: документ на одну лицензию вместо сорока не должен считаться верным."""
from eval import quote_check


def _quote(qty=40, price=2813.4, code="KL4863RAPFS", total=None):
    items = [{"offer_id": 1, "name": "Kaspersky Endpoint Security для бизнеса Стандартный", "vendor_code": code,
              "qty": qty, "price": price, "sum": round(qty * price, 2)}]
    return {"ok": True, "items": items, "total": round(qty * price, 2) if total is None else total,
            "discount_percent": 0}


WANT = [{"code": "KL4863RAPFS", "qty": 40}]


def test_right_quote_passes():
    assert quote_check.mismatches(WANT, _quote(), lambda _id: 2813.4) == []


def test_one_license_instead_of_forty_fails():
    bad = quote_check.mismatches(WANT, _quote(qty=1))
    assert bad == ["KL4863RAPFS: количество 1, заказано 40"]


def test_other_article_and_extra_line_fail():
    assert "нет позиции: KL4863RAPFS" in quote_check.mismatches(WANT, _quote(code="KL4867RAQFS"))
    q = _quote()
    q["items"].append({"offer_id": 2, "name": "лишняя", "vendor_code": "X", "qty": 1, "price": 10.0, "sum": 10.0})
    q["total"] += 10.0
    assert "позиций 2, заказано 1" in quote_check.mismatches(WANT, q)


def test_price_not_from_catalog_and_broken_total_fail():
    assert any("в каталоге" in b for b in quote_check.mismatches(WANT, _quote(), lambda _id: 3000.0))
    assert "итог не равен сумме строк" in quote_check.mismatches(WANT, _quote(total=1.0))


def test_discount_is_applied_to_catalog_price():
    q = _quote(price=2672.73)
    q["discount_percent"] = 5
    assert quote_check.mismatches(WANT, q, lambda _id: 2813.4) == []


def test_words_from_name_and_no_quote():
    want = [{"has": ["kaspersky endpoint security", "стандартный"], "qty": 40}]
    assert quote_check.mismatches(want, _quote()) == []
    assert quote_check.mismatches(want, None) == ["КП не сформировано"]


def test_last_quote_takes_the_latest_successful():
    run = {"turns": [{"tools": [{"name": "make_quote", "output": {"ok": True, "total": 1}},
                                {"name": "calc_total", "output": {"ok": True}}]},
                     {"tools": [{"name": "make_quote", "output": {"ok": False}},
                                {"name": "make_quote", "output": {"ok": True, "total": 2}}]}]}
    assert quote_check.last_quote(run)["total"] == 2
    assert quote_check.last_quote({"turns": [{"tools": []}]}) is None
