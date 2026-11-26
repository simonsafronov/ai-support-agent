"""Тесты черновика КП: суммы и НДС, проверки входа, PDF, нумерация.

Идут на синтетическом каталоге tests/data/feed_small.xml, на чистом клоне тоже. Тесты с пометкой needs_data
берут позиции из каталога заказчика data/db/catalog.sqlite (нет базы: пропускаются).
Хранилище приложения и каталог с PDF на время теста направлены во временную папку.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import threading
import types
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # чтобы тест находил пакет app и при запуске .venv/bin/pytest
    sys.path.insert(0, str(ROOT))

from app import config, store  # noqa: E402
from app.quote import kp  # noqa: E402

pytestmark = pytest.mark.small_catalog

CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def vat_of(amount: Decimal) -> Decimal:
    return (amount * 22 / 122).quantize(CENT, rounding=ROUND_HALF_UP)


def ru(value) -> str:
    """Число так, как оно стоит в PDF, без пробелов между разрядами: 1054.17 -> «1054,17»."""
    return f"{money(value):.2f}".replace(".", ",")


def pdf_pages(path) -> list[str]:
    return [page.extract_text() for page in PdfReader(path).pages]


def squeeze(text: str) -> str:
    """Текст без пробелов и переносов: сравнение не зависит от того, как PDF разбит на строки."""
    return re.sub(r"\s+", "", text)


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB", tmp_path / "app.sqlite")
    monkeypatch.setattr(config, "QUOTES_DIR", tmp_path / "quotes")
    monkeypatch.setattr(store, "_local", threading.local())  # свое соединение с базой на каждый тест
    yield tmp_path
    con = getattr(store._local, "con", None)
    if con is not None:
        con.close()


@pytest.fixture(scope="module")
def catalog(small_catalog_db):
    con = sqlite3.connect(small_catalog_db)
    con.row_factory = sqlite3.Row
    yield con
    con.close()


@pytest.fixture(scope="module")
def offers(catalog):
    """Три позиции каталога: из реестра отечественного ПО с ценой, обычная с ценой, с ценой по запросу.

    Все три без ступеней количества: цена ступени на чужое количество в КП не идет, это отдельный тест ниже.
    """
    def pick(*wheres):
        for where in wheres:
            row = catalog.execute("SELECT id, name, vendor_code, price, registry_no, available FROM offers "
                                  f"WHERE {where} AND qty_pricing IS NULL ORDER BY id LIMIT 1").fetchone()
            if row:
                return dict(row)
        pytest.skip("в каталоге нет подходящей позиции")

    foreign = "(registry_no IS NULL OR registry_no = '')"
    return {
        "registry": pick("price > 0 AND registry_no <> ''"),
        "plain": pick(f"price > 0 AND price <> CAST(price AS INTEGER) AND {foreign}", f"price > 0 AND {foreign}"),
        "request": pick("price IS NULL"),
    }


def fake_catalog(monkeypatch, fake_offers: dict):
    """Каталог из словаря: позиции с заданными ценами, артикулами и признаком реестра."""
    fake = types.ModuleType("app.catalog.search")
    fake.get_offer = lambda offer_id: fake_offers.get(offer_id) or {"error": "offer_not_found", "offer_id": offer_id}
    monkeypatch.setitem(sys.modules, "app.catalog.search", fake)


@pytest.fixture(scope="module")
def missing_id(catalog):
    return catalog.execute("SELECT MAX(id) FROM offers").fetchone()[0] + 1000


def quote_rows() -> list:
    return store.db().execute("SELECT * FROM quotes ORDER BY id").fetchall()


def test_sums_vat_and_pdf(offers, storage):
    reg, plain, req = offers["registry"], offers["plain"], offers["request"]
    res = kp.make_quote("s1", "ООО «Ромашка»",
                        [{"offer_id": reg["id"], "qty": 3}, {"offer_id": plain["id"], "qty": 7},
                         {"offer_id": req["id"], "qty": 2}],
                        inn="7701234567", contact="buh@romashka.example")
    assert res["ok"] is True, res
    reg_sum, plain_sum = money(reg["price"]) * 3, money(plain["price"]) * 7
    by_id = {item["offer_id"]: item for item in res["items"]}

    assert by_id[reg["id"]] == {"offer_id": reg["id"], "name": by_id[reg["id"]]["name"],
                                "vendor_code": reg["vendor_code"] or "", "qty": 3, "price": float(money(reg["price"])),
                                "sum": float(reg_sum), "vat": "Без НДС", "available": bool(reg["available"])}
    assert by_id[plain["id"]]["price"] == float(money(plain["price"]))
    assert by_id[plain["id"]]["sum"] == float(plain_sum)
    assert by_id[plain["id"]]["vat"] == float(vat_of(plain_sum))
    assert (by_id[req["id"]]["price"], by_id[req["id"]]["sum"], by_id[req["id"]]["vat"]) == (None, None, None)
    for src in (reg, plain, req):  # названия из каталога, у обрезанных в фиде снята только висящая запятая
        assert by_id[src["id"]]["name"] == " ".join(src["name"].split()).strip(" ,;")

    assert res["total"] == float(reg_sum + plain_sum)  # позиция по запросу в итог не входит
    assert res["vat_total"] == float(vat_of(plain_sum))
    assert res["has_price_on_request"] is True
    assert res["customer"] == "ООО «Ромашка»"
    assert res["note"].startswith("Черновик КП сформирован, его проверит менеджер.")
    assert res["customer_missing"] == ["КПП"]
    assert "уточнит менеджер" not in res["note"] and "ради реквизитов не заводите" in res["note"]

    assert re.fullmatch(r"КП-\d{8}-0001", res["number"])
    assert res["url"] == f"/quotes/{res['number']}.pdf"
    date = datetime.strptime(res["date"], "%d.%m.%Y")
    assert res["number"].split("-")[1] == date.strftime("%Y%m%d")
    assert datetime.strptime(res["valid_until"], "%d.%m.%Y") == date + timedelta(days=14)

    pdf = Path(res["file"])
    assert pdf == storage / "quotes" / f"{res['number']}.pdf" and pdf.stat().st_size > 10_000
    pages = pdf_pages(pdf)
    assert len(pages) == 1
    text = squeeze(pages[0])
    assert squeeze("Куда: ООО «Ромашка», ИНН 7701234567") in text
    assert squeeze("Контакт заказчика: buh@romashka.example") in text
    assert squeeze(f"Исх. № {res['number']} от {res['date']}.") in text
    assert "Итого" + ru(reg_sum + plain_sum) + ru(vat_of(plain_sum)) in text
    assert ru(plain_sum) in text and ru(reg_sum) in text
    assert text.count("позапросу") == 1 and "БезНДС" in text  # позиция по запросу стоит блоком под таблицей
    article = f", арт. {req['vendor_code']}" if req["vendor_code"] else ""
    assert text.index("Итого") < text.index(squeeze(kp.ON_REQUEST_HEAD)) < text.index(squeeze("Цена указана"))
    assert squeeze(f"{by_id[req['id']]['name']}{article}, 2 шт.") in text
    for phrase in ("Уважаемый заказчик!", "Наименование", "В т.ч. НДС", kp.ON_REQUEST_HEAD,
                   "НДС рассчитан автоматически, проверяет менеджер.",
                   "Условия оплаты: Предоплата.", f"Срок поставки: {kp.DEFAULT_DELIVERY}.",
                   f"Срок действия коммерческого предложения - до {res['valid_until']}.",
                   "Черновик коммерческого предложения.",
                   "Подготовлен ИИ-консультантом Softline Store, требует проверки менеджером.",
                   config.KP_SIGNER_NAME, config.KP_SIGNER_TITLE):
        assert squeeze(phrase) in text, phrase

    (row,) = quote_rows()
    assert (row["session_id"], row["number"], row["customer"], row["total"], row["file"]) == \
           ("s1", res["number"], "ООО «Ромашка»", res["total"], str(pdf))


def test_only_offer_id_and_qty_are_taken_from_the_model(offers):
    plain = offers["plain"]
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": str(plain["id"]), "qty": 2.0, "price": 1, "sum": 2,
                                           "name": "Подмена", "vendor_code": "FAKE", "vat": 0}],
                        payment="постоплата 30 дней.", delivery="5 рабочих дней")
    assert res["ok"] is True, res
    (item,) = res["items"]
    assert item["name"] != "Подмена" and item["vendor_code"] == (plain["vendor_code"] or "")
    assert (item["qty"], item["price"], item["sum"]) == (2, float(money(plain["price"])),
                                                         float(money(plain["price"]) * 2))
    assert res["total"] == item["sum"] and res["has_price_on_request"] is False
    text = squeeze(pdf_pages(res["file"])[0])
    assert squeeze("Куда: ООО Тест") in text and "ИНН" not in text and "Контактзаказчика" not in text
    assert "постоплат" not in text.lower()  # условие оплаты не берется из вызова: в документе условие магазина
    assert squeeze(f"Условия оплаты: {kp.DEFAULT_PAYMENT}.") in text
    assert squeeze("Срок поставки: 5 рабочих дней.") in text
    assert "Подмена" not in text and "FAKE" not in text and "рассчитаетменеджер" not in text


def test_errors_do_not_create_pdf(offers, missing_id, storage):
    good = {"offer_id": offers["plain"]["id"], "qty": 1}
    cases = [
        (dict(customer="ООО Тест", items=[]), "позици"),
        (dict(customer="ООО Тест", items=None), "позици"),
        (dict(customer="ООО Тест", items=[{**good, "qty": 0}]), "qty"),
        (dict(customer="ООО Тест", items=[{**good, "qty": -3}]), "qty"),
        (dict(customer="ООО Тест", items=[{**good, "qty": 2.5}]), "qty"),
        (dict(customer="ООО Тест", items=[{**good, "qty": "много"}]), "qty"),
        (dict(customer="ООО Тест", items=[{**good, "qty": True}]), "qty"),
        (dict(customer="ООО Тест", items=[{"offer_id": good["offer_id"]}]), "qty"),
        (dict(customer="ООО Тест", items=[{"qty": 1}]), "offer_id"),
        (dict(customer="ООО Тест", items=[good, {"offer_id": missing_id, "qty": 1}]), str(missing_id)),
        (dict(customer="ООО Тест", items=[good] * 31), "30"),
    ]
    for kwargs, expected in cases:
        res = kp.make_quote("s1", **kwargs)
        assert res["ok"] is False and set(res) == {"ok", "error"}, (kwargs, res)
        assert expected in res["error"] and re.search("[а-яё]", res["error"]), (kwargs, res)
    assert not list(storage.rglob("*.pdf"))
    assert quote_rows() == []


def test_missing_offer_is_named(offers, missing_id):
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": missing_id, "qty": 1}, {"offer_id": missing_id + 1, "qty": 2}])
    assert res["ok"] is False
    assert f"offer_id {missing_id} не найден" in res["error"] and f"offer_id {missing_id + 1} не найден" in res["error"]


def test_thirty_items_continue_on_next_page_with_table_header(catalog, storage):
    ids = [r[0] for r in catalog.execute("SELECT id FROM offers WHERE price > 0 AND vendor_code <> '' "
                                         "AND qty_pricing IS NULL ORDER BY LENGTH(name) DESC, id LIMIT 30")]
    assert len(ids) == 30
    res = kp.make_quote("s1", "ООО «Тридцать позиций»", [{"offer_id": i, "qty": n} for n, i in enumerate(ids, 1)])
    assert res["ok"] is True, res
    assert len(res["items"]) == 30
    expected = sum(money(catalog.execute("SELECT price FROM offers WHERE id=?", (i,)).fetchone()[0]) * n
                   for n, i in enumerate(ids, 1))
    assert res["total"] == float(expected)

    pages = [squeeze(p) for p in pdf_pages(res["file"])]
    assert len(pages) >= 2
    assert "Уважаемыйзаказчик!" in pages[0] and all("Уважаемыйзаказчик!" not in p for p in pages[1:])
    assert all(res["number"] in p for p in pages)
    (total_page,) = [n for n, p in enumerate(pages) if "Итого" in p]
    assert total_page >= 1
    for page in pages[:total_page + 1]:  # шапка таблицы повторяется на каждой странице с позициями
        assert "Артикул" in page and "Наименование" in page and squeeze("Сумма, руб.") in page
    for item in res["items"]:  # ни одна позиция не потерялась при переносе
        assert sum(p.count(squeeze(item["vendor_code"])) for p in pages) >= 1
    # итог не отрывается от последней позиции и сноски; условия, плашка и подпись идут одним блоком
    assert "Итого" + ru(expected) in pages[total_page] and ru(res["items"][-1]["sum"]) in pages[total_page]
    assert squeeze("НДС рассчитан автоматически") in pages[total_page]
    (terms_page,) = [p for p in pages if squeeze("Цена указана с учетом") in p]
    assert squeeze("Черновик коммерческого предложения.") in terms_page and squeeze(config.KP_SIGNER_SHORT) in terms_page


def test_numbers_grow_and_special_characters_are_safe(offers, storage):
    item = [{"offer_id": offers["registry"]["id"], "qty": 1}]
    first = kp.make_quote("s1", 'ЗАО "Альфа & Омега" <тест>', item, inn="7700000000")
    second = kp.make_quote("s2", "ИП Иванов И.И.", item)
    assert first["ok"] and second["ok"], (first, second)
    assert first["number"].endswith("-0001") and second["number"].endswith("-0002")
    assert Path(first["file"]).exists() and Path(second["file"]).exists()
    assert [r["number"] for r in quote_rows()] == [first["number"], second["number"]]
    text = squeeze(pdf_pages(first["file"])[0])
    assert squeeze('Куда: ЗАО "Альфа & Омега" <тест>, ИНН 7700000000') in text
    assert second["vat_total"] == 0.0 and second["has_price_on_request"] is False
    assert "Итого" + ru(second["total"]) + "БезНДС" in squeeze(pdf_pages(second["file"])[0])


def test_offer_is_read_from_table_when_catalog_search_is_missing(offers, missing_id, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.catalog.search", None)  # импорт модуля поиска падает
    plain = offers["plain"]
    found = kp._offer(plain["id"])
    assert (found["offer_id"], found["name"], found["vendor_code"], found["price"], found["registry_no"]) == \
           (plain["id"], plain["name"], plain["vendor_code"], plain["price"], plain["registry_no"])
    assert kp._offer(missing_id) is None
    assert kp.make_quote("s1", "ООО Тест", [{"offer_id": plain["id"], "qty": 4}])["total"] == \
           float(money(plain["price"]) * 4)


TIERS = {  # цена за штуку действует в своей ступени количества; у per_tier цена за весь комплект, ступени нет
    1: {"offer_id": 1, "name": "Узлы 10-14", "vendor_code": "T-1", "price": 7703.1, "registry_no": None,
        "qty_pricing": "per_unit", "qty_min": 10, "qty_max": 14},
    2: {"offer_id": 2, "name": "Объем 100-149", "vendor_code": "T-2", "price": 1054.17, "registry_no": None,
        "qty_pricing": "per_unit", "qty_min": 100, "qty_max": 149},
    3: {"offer_id": 3, "name": "От 500", "vendor_code": "T-3", "price": 10, "registry_no": None,
        "qty_pricing": "per_unit", "qty_min": 500, "qty_max": None},
    4: {"offer_id": 4, "name": "Комплект на 30", "vendor_code": "T-4", "price": 131356, "registry_no": None,
        "qty_pricing": "per_tier", "qty_min": 26, "qty_max": 30},
}


def test_quote_refuses_quantity_outside_price_tier(monkeypatch, storage):
    """Зачем: calc_total отказывал в цене ступени на чужое количество, а КП на то же количество формировалось."""
    fake_catalog(monkeypatch, TIERS)
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 1}, {"offer_id": 2, "qty": 5},
                                           {"offer_id": 3, "qty": 499}, {"offer_id": 2, "qty": 150}])
    assert res["ok"] is False
    for part in ("позиция 1: вариант действует при покупке от 10 до 14 лицензий, а запрошено 1",
                 "позиция 2: вариант действует при покупке от 100 до 149 лицензий, а запрошено 5",
                 "позиция 3: вариант действует при покупке от 500 и больше лицензий, а запрошено 499",
                 "позиция 4: вариант действует при покупке от 100 до 149 лицензий, а запрошено 150",
                 "get_product с quantity=150"):
        assert part in res["error"], res["error"]
    assert not list(storage.rglob("*.pdf")) and quote_rows() == []

    ok = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 10}, {"offer_id": 2, "qty": 149},
                                          {"offer_id": 3, "qty": 9000}, {"offer_id": 4, "qty": 1}])
    assert ok["ok"] is True, ok   # внутри ступени КП формируется; комплект (цена за ступень целиком) берут и по одному
    assert ok["total"] == float(money(7703.1) * 10 + money(1054.17) * 149 + money(10) * 9000 + money(131356))


def test_calc_total_and_quote_share_one_tier_check(monkeypatch):
    from app.agent import tools
    fake_catalog(monkeypatch, TIERS)
    monkeypatch.setattr(tools.catalog, "get_offer", lambda offer_id: TIERS[int(offer_id)])
    item = [{"offer_id": 1, "qty": 15}]
    calc = tools.call(tools.ToolContext("s1"), "calc_total", {"items": item})["error"]
    quote = kp.make_quote("s1", "ООО Тест", item)["error"]
    said = kp.tier_problem(TIERS[1], 15)
    assert said.startswith("вариант действует при покупке от 10 до 14 лицензий, а запрошено 15")
    assert said[1:] in calc and said in quote and kp.tier_problem(TIERS[1], 12) == "" == kp.tier_problem(TIERS[4], 1)


@pytest.mark.needs_data
@pytest.mark.parametrize("offer_id,qty,span", [(4845, 1, "от 10 до 14"), (3574, 5, "от 100 до 149")])
def test_real_tier_offers_are_refused_in_the_quote_with_and_without_catalog_search(monkeypatch, storage,
                                                                                   offer_id, qty, span):
    catalog = sqlite3.connect(config.CATALOG_DB)
    row = catalog.execute("SELECT qty_pricing, qty_min, qty_max FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not row or tuple(row) != ("per_unit", *(int(x) for x in re.findall(r"\d+", span))):
        pytest.skip("каталог обновился: у контрольного варианта другая ступень")
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": offer_id, "qty": qty}])
    assert res["ok"] is False and f"позиция 1: вариант действует при покупке {span}" in res["error"]
    monkeypatch.setitem(sys.modules, "app.catalog.search", None)  # позиция читается прямо из таблицы: проверка та же
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": offer_id, "qty": qty}])
    assert res["ok"] is False and f"позиция 1: вариант действует при покупке {span}" in res["error"]
    assert quote_rows() == []


def test_unavailable_catalog_and_oversized_text_are_reported(offers, storage, monkeypatch):
    item = [{"offer_id": offers["plain"]["id"], "qty": 1}]
    res = kp.make_quote("s1", "ООО «Длинное название» " * 300, item)
    assert res["ok"] is False and "слишком длинный текст" in res["error"]

    monkeypatch.setitem(sys.modules, "app.catalog.search", None)
    monkeypatch.setattr(config, "CATALOG_DB", storage / "no-catalog.sqlite")
    res = kp.make_quote("s1", "ООО Тест", item)
    assert res["ok"] is False and "Каталог сейчас недоступен" in res["error"]
    assert not (storage / "no-catalog.sqlite").exists()  # чтение каталога не создает пустую базу
    assert not list(storage.rglob("*.pdf")) and quote_rows() == []


def test_catalog_search_is_used_and_rounding_is_to_kopecks(monkeypatch):
    fake_offers = {
        1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": "A-1", "price": 100, "registry_no": None},
        2: {"offer_id": 2, "name": "Лицензия Б", "vendor_code": "B-2", "price": 0.99, "registry_no": ""},
        3: {"offer_id": 3, "name": "Отечественная ОС", "vendor_code": None, "price": 999.99, "registry_no": "1999"},
        4: {"offer_id": 4, "name": "Цена по запросу", "vendor_code": "D-4", "price": None, "registry_no": None},
    }
    fake_catalog(monkeypatch, fake_offers)

    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 1}, {"offer_id": 2, "qty": 3},
                                           {"offer_id": 3, "qty": 2}, {"offer_id": 4, "qty": 5}])
    assert res["ok"] is True, res
    assert [(i["name"], i["price"], i["sum"], i["vat"]) for i in res["items"]] == [
        ("Лицензия А", 100.0, 100.0, 18.03),      # 100 * 22 / 122 = 18.0327...
        ("Лицензия Б", 0.99, 2.97, 0.54),         # 2.97 * 22 / 122 = 0.5355...
        ("Отечественная ОС", 999.99, 1999.98, "Без НДС"),
        ("Цена по запросу", None, None, None),
    ]
    assert res["total"] == 2102.95 and res["vat_total"] == 18.57 and res["has_price_on_request"] is True
    assert kp.VAT_RATE == Decimal("22")


def test_quote_without_customer_name_is_still_a_draft(offers, storage):
    """Клиент не назвал организацию: черновик все равно собирается, менеджер уточнит название."""
    res = kp.make_quote("s1", "  ", [{"offer_id": offers["plain"]["id"], "qty": 1}])
    assert res["ok"] and res["customer"] == kp.NO_CUSTOMER and "название организации" in res["note"]


def test_second_quote_in_dialog_updates_the_same_draft(offers, storage):
    first = kp.make_quote("s-upd", customer="", items=[{"offer_id": offers["registry"]["id"], "qty": 2}])
    second = kp.make_quote("s-upd", customer="ООО Ромашка", items=[{"offer_id": offers["registry"]["id"], "qty": 3}])
    assert second["number"] == first["number"] and second["updated"] and not first["updated"]
    rows = store.db().execute("SELECT customer FROM quotes WHERE session_id='s-upd'").fetchall()
    assert [r[0] for r in rows] == ["ООО Ромашка"]
    third = kp.make_quote("s-upd", customer="", items=[{"offer_id": offers["registry"]["id"], "qty": 5}])
    assert third["customer"] == "ООО Ромашка"  # название, названное раньше, при правке количества не теряется


def test_customer_block_takes_name_inn_and_kpp(offers, storage):
    item = [{"offer_id": offers["plain"]["id"], "qty": 2}]
    res = kp.make_quote("s1", "ООО «Ромашка»", item, inn="7701 234 567", kpp="770101001")
    assert res["ok"] and res["customer_missing"] == []
    text = squeeze(pdf_pages(res["file"])[0])
    assert squeeze("Куда: ООО «Ромашка», ИНН 7701234567, КПП 770101001") in text


def test_missing_and_malformed_requisites_are_named(offers, storage):
    item = [{"offer_id": offers["plain"]["id"], "qty": 1}]
    res = kp.make_quote("s1", "", item, inn="12345", kpp="77")
    assert res["ok"] and res["customer_missing"] == ["название организации", "ИНН", "КПП"]
    assert "ИНН и КПП в черновик не попал" in res["note"] and "не хватает" in res["note"]
    text = squeeze(pdf_pages(res["file"])[0])
    assert "12345" not in text and "КПП" not in text
    sole = kp.make_quote("s2", "ИП Иванов И.И.", item, inn="770123456789", kpp="770101001")
    assert sole["customer_missing"] == [] and "КПП" not in squeeze(pdf_pages(sole["file"])[0])


def test_discount_is_shown_as_separate_rows_before_the_total(monkeypatch):
    """Скидка видна в документе: позиции по ценам каталога, ниже сумма без скидки, скидка и итог."""
    fake_catalog(monkeypatch, {
        1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": "A-1", "price": 1000, "registry_no": None},
        2: {"offer_id": 2, "name": "Лицензия Б", "vendor_code": "B-2", "price": 0.99, "registry_no": ""}})
    items = [{"offer_id": 1, "qty": 2}, {"offer_id": 2, "qty": 3}]

    res = kp.make_quote("s1", "ООО Тест", items, discount_percent=5)
    assert [(i["price"], i["sum"], i["vat"]) for i in res["items"]] == [(950.0, 1900.0, 342.62), (0.94, 2.82, 0.51)]
    assert (res["total"], res["vat_total"]) == (1902.82, 343.13)
    assert (res["total_without_discount"], res["discount_sum"]) == (2002.97, 100.15)
    text = squeeze(pdf_pages(res["file"])[0])
    assert "A-1ЛицензияА21000,002000,00360,66" in text and "B-2ЛицензияБ30,992,970,54" in text
    assert "Суммабезскидки2002,97361,20" in text
    assert "Скидка5%−100,15−18,07" in text
    assert "Итого1902,82343,13" in text and "Итогосо" not in text

    plain = kp.make_quote("s2", "ООО Тест", items)
    assert "total_without_discount" not in plain and "discount_sum" not in plain
    text = squeeze(pdf_pages(plain["file"])[0])
    assert "Суммабезскидки" not in text and "Скидка" not in text and "Итого2002,97361,20" in text


def test_discount_on_registry_software_has_no_vat_in_any_row(monkeypatch):
    fake_catalog(monkeypatch, {3: {"offer_id": 3, "name": "Отечественная ОС", "vendor_code": None, "price": 999.99,
                                   "registry_no": "1999"}})
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 3, "qty": 2}], discount_percent=2.5)
    assert (res["total"], res["total_without_discount"], res["discount_sum"]) == (1949.98, 1999.98, 50.0)
    text = squeeze(pdf_pages(res["file"])[0])
    assert "Суммабезскидки1999,98БезНДС" in text and "Скидка2,5%−50,00БезНДС" in text
    assert "Итого1949,98БезНДС" in text


def test_article_column_is_dropped_when_no_item_has_an_article(monkeypatch):
    """В фиде у части позиций артикула нет: пустой графы в документе быть не должно."""
    fake_catalog(monkeypatch, {
        1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": None, "price": 100, "registry_no": None},
        2: {"offer_id": 2, "name": "Лицензия Б", "vendor_code": "B-2", "price": 50, "registry_no": None}})
    bare = squeeze(pdf_pages(kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 1}])["file"])[0])
    assert "Артикул" not in bare
    assert squeeze("Наименование Кол-во Цена, руб. Сумма, руб. В т.ч. НДС, руб. Лицензия А 1 100,00") in bare

    mixed = squeeze(pdf_pages(kp.make_quote("s2", "ООО Тест", [{"offer_id": 1, "qty": 1},
                                                              {"offer_id": 2, "qty": 1}])["file"])[0])
    assert squeeze("Артикул Наименование Кол-во") in mixed
    assert "—ЛицензияА1100,00" in mixed and "B-2ЛицензияБ150,00" in mixed


def test_table_header_fits_its_columns_in_one_line():
    for with_article in (True, False):
        widths = kp.column_widths(with_article)
        assert ("code" in widths) is with_article and sum(widths.values()) == pytest.approx(kp.WIDTH)
        for key, title in kp.HEAD:
            if key in widths:
                need = pdfmetrics.stringWidth(title, kp.S_HEAD.fontName, kp.S_HEAD.fontSize)
                assert need <= widths[key] - 2 * kp.PAD, (title, need, widths[key])


def test_stand_quote_is_rebuilt_with_the_discount_given_in_the_dialog(monkeypatch):
    """Перенос диалога на стенд собирает КП заново: скидка берется из памяти диалога, номер остается прежним."""
    from eval import to_stand
    fake_catalog(monkeypatch, {1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": "A-1", "price": 1000,
                                   "registry_no": None}})
    sid = store.get_session(None, "eval-c55")["id"]
    store.update_session(sid, facts={"компания": "ООО Ромашка", "ИНН": "7701234567", "скидка": "5%"})
    store.add_step(sid, 1, "tool", "make_quote", {"items": [{"offer_id": 1, "qty": 2}], "customer": "[компания]"}, {})

    first = to_stand.requote(sid)
    assert (first["total"], first["discount_percent"], first["customer"]) == (1900.0, 5.0, "ООО Ромашка")
    again = to_stand.requote(sid)
    assert again["number"] == first["number"] and len(quote_rows()) == 1
    assert "Скидка5%−100,00" in squeeze(pdf_pages(again["file"])[0])
    assert to_stand.requote(store.get_session(None, "eval-c01")["id"]) is None  # в диалоге КП не было



def number(text: str) -> Decimal:
    """Число из ячейки PDF: «−1 054,17» -> Decimal("-1054.17")."""
    return Decimal(text.replace(kp.MINUS, "-").replace(kp.GROUP, "").replace(",", "."))


@pytest.fixture
def printed(monkeypatch):
    """Ячейки таблицы последнего собранного КП: ровно тот текст, который уходит в PDF."""
    seen, render = {}, kp._render
    monkeypatch.setattr(kp, "_render", lambda q: seen.update(cells=kp.table_cells(q)) or render(q))
    return seen


@pytest.mark.parametrize("discount", [0, 5, 2.5])
def test_printed_table_adds_up(monkeypatch, printed, discount):
    """Арифметика напечатанного: цена на количество дает сумму строки, столбцы сходятся с итогом."""
    fake_catalog(monkeypatch, {
        1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": "A-1", "price": 6150, "registry_no": "1999"},
        2: {"offer_id": 2, "name": "Лицензия Б", "vendor_code": "B-2", "price": 13566, "registry_no": None},
        3: {"offer_id": 3, "name": "Лицензия В", "vendor_code": "C-3", "price": 7703.1, "registry_no": None},
        4: {"offer_id": 4, "name": "Цена по запросу", "vendor_code": "D-4", "price": None, "registry_no": None}})
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 25}, {"offer_id": 2, "qty": 3},
                                           {"offer_id": 3, "qty": 17}, {"offer_id": 4, "qty": 10}],
                        discount_percent=discount)
    cells = printed["cells"]
    items = [c for c in cells if c["kind"] == "item"]
    assert [c["name"] for c in items] == ["Лицензия А", "Лицензия Б", "Лицензия В"]  # позиция по запросу вне таблицы
    for c in items:
        assert number(c["price"]) * int(c["qty"]) == number(c["sum"]), c
    rows_sum = sum(number(c["sum"]) for c in items)
    rows_vat = sum(number(c["vat"]) for c in items if c["vat"] != kp.NO_VAT)
    total = cells[-1]
    assert (total["kind"], number(total["sum"]), number(total["vat"])) == \
           ("total", Decimal(str(res["total"])), Decimal(str(res["vat_total"])))
    if discount:
        before, off = [c for c in cells if c["kind"] == "subtotal"]
        assert (number(before["sum"]), number(before["vat"])) == (rows_sum, rows_vat)
        assert number(off["sum"]) < 0 and number(off["vat"]) < 0
        assert number(before["sum"]) + number(off["sum"]) == number(total["sum"])
        assert number(before["vat"]) + number(off["vat"]) == number(total["vat"])
    else:
        assert (rows_sum, rows_vat) == (number(total["sum"]), number(total["vat"]))
        assert all(c["kind"] != "subtotal" for c in cells)


def test_long_sums_are_printed_in_full_in_one_line(monkeypatch, printed):
    """Суммы от миллиона: число целиком, с первой цифрой, в одну строку в своей колонке."""
    fake_catalog(monkeypatch, {1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": "A-1", "price": 1234567.89,
                                   "registry_no": None}})
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 9}], discount_percent=5)
    full = money("1234567.89") * 9
    text = squeeze(pdf_pages(res["file"])[0])
    assert "A-1ЛицензияА9" + ru("1234567.89") + ru(full) + ru(vat_of(full)) in text
    assert "Суммабезскидки" + ru(full) + ru(vat_of(full)) in text
    assert "Итого" + ru(res["total"]) + ru(res["vat_total"]) in text and res["total"] > 10_000_000

    widths = kp.column_widths(True)
    for cell in printed["cells"]:
        for key in ("qty", "price", "sum", "vat"):
            if cell.get(key):
                room = widths[key] - 2 * kp.PAD
                line = kp._fit(cell[key], room, kp.S_NUM_BOLD)
                assert pdfmetrics.stringWidth(line.text, line.style.fontName, line.style.fontSize) <= room, cell
                assert line.wrap(room, 1000) == (room, line.style.leading)  # одна строка во всю ширину ячейки



def test_copied_table_row_keeps_quantity_and_price_apart(monkeypatch):
    """Текст PDF, каким его отдает копирование: разряды числа держит узкий пробел, колонки обычный.

    Иначе строка «25 6 150,00 153 750,00» читается как цена 25 150,00 без количества.
    """
    fake_catalog(monkeypatch, {1: {"offer_id": 1, "name": "Лицензия А", "vendor_code": "A-1", "price": 6150,
                                   "registry_no": None}})
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1, "qty": 25}], discount_percent=5)
    page = PdfReader(res["file"]).pages[0]
    tokens = re.split(r"[ \n]+", page.extract_text())  # делим только по обычному пробелу и переводу строки
    for token in ("25", "6\u202f150,00", "153\u202f750,00", "27\u202f725,41", "\u22127\u202f687,50", "146\u202f062,50"):
        assert token in tokens, (token, tokens)
    (row,) = [ln for ln in page.extract_text(extraction_mode="layout").splitlines() if "Лицензия А" in ln]
    assert re.split(r" {2,}", row.strip()) == ["A-1", "Лицензия А", "25", "6\u202f150,00", "153\u202f750,00",
                                               "27\u202f725,41"]


def test_article_with_several_prices_is_marked_for_the_manager(offers, storage):
    """Зачем: у артикула CB-WS-1Y в каталоге две цены без различия в параметрах, в КП молча шла одна из них."""
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": 1082, "qty": 2}, {"offer_id": offers["plain"]["id"], "qty": 1}])
    assert res["ok"] is True, res
    flagged, plain = res["items"]
    assert flagged["price_to_confirm"] is True and "price_to_confirm" not in plain
    assert flagged["sum"] == 9900.0 and res["total"] == 9900.0 + plain["sum"]  # сумма считается по цене offer_id
    assert res["price_to_confirm"] == [flagged["name"]] and "подтвердит менеджер" in res["note"]
    text = squeeze(pdf_pages(res["file"])[0])
    assert "CB-WS-1Y" in text and "4950,00*" in text and squeeze("* " + kp.PRICE_CHECK_LINE) in text
    assert text.count("*") == 2  # знак у цены и сноска под таблицей


def test_ordinary_item_has_no_price_footnote(offers, storage):
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": offers["plain"]["id"], "qty": 1}])
    assert res["price_to_confirm"] == [] and "подтвердит менеджер" not in res["note"]
    text = squeeze(pdf_pages(res["file"])[0])
    assert "*" not in text and squeeze(kp.PRICE_CHECK_LINE) not in text


@pytest.mark.needs_data
def test_real_article_with_several_prices_gets_the_footnote(monkeypatch, storage):
    """Кейс c03 набора: у LBS-AC-12M-25-A3 в каталоге две цены; кейсы c10 и c52: цена зависит от ступени количества."""
    catalog = sqlite3.connect(config.CATALOG_DB)
    ambiguous = catalog.execute("SELECT id FROM offers WHERE price_ambiguous = 1 AND price > 0 AND qty_pricing IS NULL "
                                "ORDER BY id LIMIT 1").fetchone()
    c03 = catalog.execute("SELECT id FROM offers WHERE vendor_code = 'LBS-AC-12M-25-A3' AND price > 0 "
                          "ORDER BY id LIMIT 1").fetchone()
    c52 = catalog.execute("SELECT id, qty_min FROM offers WHERE vendor_code = 'SNLSP1.x-BL1-SB-SP1Y' "
                          "AND qty_pricing = 'per_unit' ORDER BY qty_min LIMIT 1").fetchone()
    catalog.close()
    if not (ambiguous and c03 and c52):
        pytest.skip("каталог обновился: контрольных артикулов нет")
    for offer_id in (ambiguous[0], c03[0]):
        res = kp.make_quote("s1", "ООО Тест", [{"offer_id": offer_id, "qty": 1}])
        assert res["ok"] and res["items"][0]["price_to_confirm"] is True, res
        assert squeeze("* " + kp.PRICE_CHECK_LINE) in squeeze(pdf_pages(res["file"])[0])
    # ступень количества объясняет разницу цен артикула: цена определена, сноски нет
    res = kp.make_quote("s2", "ООО Тест", [{"offer_id": c52[0], "qty": c52[1] or 1}])
    assert res["ok"] and res["price_to_confirm"] == [], res


@pytest.mark.parametrize("bad", ["abc", "nan", float("nan"), -5, 150, "1e999", True])
def test_wrong_discount_counts_as_zero_before_anything_is_written(offers, storage, bad):
    """Зачем: discount_percent="abc" ронял make_quote уже после записи PDF и строки в базе."""
    plain = offers["plain"]
    res = kp.make_quote("s1", "ООО Тест", [{"offer_id": plain["id"], "qty": 2}], discount_percent=bad)
    assert res["ok"] is True and res["discount_percent"] == 0.0, res
    assert res["total"] == float(money(plain["price"]) * 2) and "не распознана" in res["note"]
    assert [p.name for p in storage.rglob("*.pdf")] == [f"{res['number']}.pdf"]
    assert [(r["number"], r["total"]) for r in quote_rows()] == [(res["number"], res["total"])]
    ok = kp.make_quote("s2", "ООО Тест", [{"offer_id": plain["id"], "qty": 2}], discount_percent="3,5%")
    assert ok["discount_percent"] == 3.5 and "не распознана" not in ok["note"]


def test_signer_comes_from_settings(offers, storage, monkeypatch):
    monkeypatch.setattr(config, "KP_SIGNER_NAME", "Иванов Иван")
    monkeypatch.setattr(config, "KP_SIGNER_TITLE", "Руководитель отдела")
    monkeypatch.setattr(config, "KP_SIGNER_SHORT", "Иванов И.И.")
    text = squeeze(pdf_pages(kp.make_quote("s1", "ООО Тест", [{"offer_id": offers["plain"]["id"], "qty": 1}])["file"])[0])
    assert squeeze("Иванов Иван Руководитель отдела") in text and squeeze("Иванов И.И.") in text


# --- вызов make_quote и calc_total моделью: условие оплаты, реквизиты, сноска о цене ---------------------------------

def _dialog(*client_messages: str, **facts) -> "tools.ToolContext":
    """Диалог, в котором клиент уже написал эти сообщения, а код запомнил эти реквизиты."""
    from app.agent import tools
    sid = store.get_session(None)["id"]
    for text in client_messages:
        store.add_message(sid, "user", text)
    if facts:
        store.update_session(sid, facts=facts)
    return tools.ToolContext(sid, facts=dict(facts))


def test_model_payment_terms_do_not_reach_the_pdf(offers):
    """Зачем: модель передавала "Постоплата через 30 дней", и это попадало в PDF, хотя отсрочку магазин не дает."""
    from app.agent import tools
    spec = next(t for t in tools.specs() if t["name"] == "make_quote")
    assert "payment" not in spec["parameters"]["properties"]
    ctx = _dialog("Можно постоплату через 30 дней?")
    res = tools.call(ctx, "make_quote", {"items": [{"offer_id": offers["plain"]["id"], "qty": 1}],
                                         "payment": "Постоплата через 30 дней"})  # старый формат вызова: без ошибки
    assert res["ok"] is True, res
    text = squeeze(pdf_pages(res["file"])[0])
    assert "Постоплата" not in text and "постоплата" not in text.lower()
    assert squeeze(f"Условия оплаты: {kp.DEFAULT_PAYMENT}.") in text


def test_model_requisites_not_said_by_client_do_not_replace_saved(offers):
    from app.agent import tools
    ctx = _dialog("Счет на ООО Ромашка, ИНН 7707083893, КПП 770701001",
                  **{"компания": "ООО Ромашка", "ИНН": "7707083893", "КПП": "770701001"})
    res = tools.call(ctx, "make_quote", {"items": [{"offer_id": offers["plain"]["id"], "qty": 1}],
                                         "customer": "ООО Вектор", "inn": "7701234567", "kpp": "770101001"})
    assert res["ok"] is True and res["customer"] == "ООО Ромашка", res
    assert store.get_session(ctx.session_id)["facts"] == {"компания": "ООО Ромашка", "ИНН": "7707083893",
                                                          "КПП": "770701001"}
    text = squeeze(pdf_pages(res["file"])[0])
    assert "7707083893" in text and "770701001" in text
    assert "7701234567" not in text and "770101001" not in text and "Вектор" not in text


def test_requisites_corrected_by_client_replace_saved(offers):
    from app.agent import tools
    ctx = _dialog("ИНН 7707083893", "Ошибся, правильный ИНН 77 0123 4567, компания «Ромашка-Плюс»",
                  **{"компания": "ООО Ромашка", "ИНН": "7707083893"})
    res = tools.call(ctx, "make_quote", {"items": [{"offer_id": offers["plain"]["id"], "qty": 1}],
                                         "customer": "Ромашка-Плюс", "inn": "7701234567"})
    assert res["ok"] is True and res["customer"] == "Ромашка-Плюс", res
    facts = store.get_session(ctx.session_id)["facts"]
    assert (facts["ИНН"], facts["компания"]) == ("7701234567", "Ромашка-Плюс")
    text = squeeze(pdf_pages(res["file"])[0])
    assert "7701234567" in text and "7707083893" not in text


def test_quote_without_inn_is_still_built_with_a_note(offers):
    from app.agent import tools
    ctx = _dialog("Нужно КП на ООО Ромашка")
    res = tools.call(ctx, "make_quote", {"items": [{"offer_id": offers["plain"]["id"], "qty": 1}],
                                         "customer": "ООО Ромашка", "inn": "7701234567"})  # ИНН клиент не называл
    assert res["ok"] is True and res["customer"] == "ООО Ромашка", res
    assert res["customer_missing"] == ["ИНН", "КПП"] and "не хватает: ИНН, КПП" in res["note"]
    assert "ИНН" not in store.get_session(ctx.session_id)["facts"]
    assert "7701234567" not in squeeze(pdf_pages(res["file"])[0])


def test_calc_total_warns_about_article_with_several_prices(offers):
    """Зачем: в КП у такой позиции сноска, а расчет в чате называл одну из цен артикула без оговорки."""
    from app.agent import tools
    ctx = _dialog()
    items = [{"offer_id": 1082, "qty": 2}, {"offer_id": offers["plain"]["id"], "qty": 1}]
    res = tools.call(ctx, "calc_total", {"items": items})
    flagged, plain = res["lines"]
    assert "подтвердит менеджер" in res["note"] and flagged["name"] in res["note"] and plain["name"] not in res["note"]
    assert flagged["sum"] == 9900.0 and res["total"] == 9900.0 + plain["sum"]  # суммы те же, что без оговорки
    with_discount = tools.call(ctx, "calc_total", {"items": items, "discount_percent": 5})
    assert "подтвердит менеджер" in with_discount["note"]
    single = tools.call(ctx, "calc_total", {"items": items[1:]})
    assert "подтвердит менеджер" not in single["note"]
