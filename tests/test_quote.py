"""Тесты черновика КП: суммы и НДС, проверки входа, PDF, нумерация.

Идут на реальном каталоге data/db/catalog.sqlite (нет базы: тесты пропускаются).
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

pytestmark = pytest.mark.skipif(not config.CATALOG_DB.exists(), reason=f"нет базы каталога {config.CATALOG_DB}")

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
def catalog():
    con = sqlite3.connect(config.CATALOG_DB)
    con.row_factory = sqlite3.Row
    yield con
    con.close()


@pytest.fixture(scope="module")
def offers(catalog):
    """Три реальные позиции: из реестра отечественного ПО с ценой, обычная с ценой, с ценой по запросу."""
    def pick(*wheres):
        for where in wheres:
            row = catalog.execute("SELECT id, name, vendor_code, price, registry_no, available FROM offers "
                                  f"WHERE {where} ORDER BY id LIMIT 1").fetchone()
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
    assert text.count("позапросу") == 3 and "БезНДС" in text  # цена, сумма и строка под таблицей
    for phrase in ("Уважаемый заказчик!", "Артикул", "Наименование", "В т.ч. НДС",
                   "Позиции с ценой по запросу рассчитает менеджер.",
                   "НДС рассчитан автоматически, проверяет менеджер.",
                   "Условия оплаты: Предоплата.", f"Срок поставки: {kp.DEFAULT_DELIVERY}.",
                   f"Срок действия коммерческого предложения - до {res['valid_until']}.",
                   "Черновик коммерческого предложения.",
                   "Подготовлен ИИ-консультантом Softline Store, требует проверки менеджером.",
                   "Бондаренко Александр", "Директор департамента"):
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
    assert squeeze("Условия оплаты: постоплата 30 дней.") in text
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
                                         "ORDER BY LENGTH(name) DESC, id LIMIT 30")]
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
    assert squeeze("Черновик коммерческого предложения.") in terms_page and squeeze("Бондаренко А.С.") in terms_page


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
