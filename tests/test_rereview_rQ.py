"""Повторное ревью: в КП идут ИНН и КПП, которые клиент назвал последним; сверка состава КП без эталонной цены.

Диалоги идут через цикл агента на модели-сценарии и синтетическом каталоге tests/data/feed_small.xml.
"""
import re
import sqlite3

import pytest
from pypdf import PdfReader

from app import config, store
from app.agent import loop, tools
from app.agent.llm import Scripted
from eval import quote_check

pytestmark = pytest.mark.small_catalog

OLD_INN, NEW_INN = "7707083893", "7728168971"  # оба с верной контрольной цифрой
OLD_KPP, NEW_KPP = "770701001", "772801001"


@pytest.fixture(scope="module")
def item(small_catalog_db):
    """Позиция с ценой и без ступеней количества: КП на нее собирается при любом количестве."""
    con = sqlite3.connect(small_catalog_db)
    row = con.execute("SELECT id FROM offers WHERE price > 0 AND qty_pricing IS NULL ORDER BY id LIMIT 1").fetchone()
    con.close()
    return {"offer_id": row[0], "qty": 2}


def pdf_text(quote: dict) -> str:
    """Текст PDF без пробелов и переносов: сравнение не зависит от того, как документ разбит на строки."""
    path = config.QUOTES_DIR / f"{quote['number']}.pdf"
    return re.sub(r"\s+", "", "".join(page.extract_text() for page in PdfReader(path).pages))


def quote_sid(quote: dict) -> str:
    return store.db().execute("SELECT session_id FROM quotes WHERE number=?", (quote["number"],)).fetchone()[0]


def dialog(item: dict, *turns: str, **quote_args) -> tuple[dict, str]:
    """Клиент пишет реплики по очереди, на последней модель вызывает make_quote с этими аргументами.

    Возвращает ответ make_quote и текст PDF.
    """
    sid = None
    for text in turns[:-1]:
        sid = loop.run_turn(sid, text, llm=Scripted(["Записал."]))["session_id"]
    res = loop.run_turn(sid, turns[-1], llm=Scripted([
        {"name": "make_quote", "args": {"items": [item], "customer": "ООО Ромашка", **quote_args}},
        "Черновик КП под этим сообщением."]))
    (quote,) = [t["output"] for t in res["trace"] if t["kind"] == "tool" and t["name"] == "make_quote"]
    assert quote["ok"], quote
    return quote, pdf_text(quote)


# --- A. ИНН и КПП в КП: последнее, что назвал клиент ---

def test_corrected_inn_goes_to_quote_whatever_the_model_passes(item):
    """Зачем: модель возвращала старый ИНН, клиент его когда-то писал, и номер подменял исправленный."""
    quote, text = dialog(item, f"Нужно КП на ООО Ромашка, ИНН {OLD_INN}", f"Ошибся, ИНН {NEW_INN}", inn=OLD_INN)
    assert quote["customer_missing"] == ["КПП"]
    assert f"ИНН{NEW_INN}" in text and OLD_INN not in text
    assert store.get_session(quote_sid(quote))["facts"]["ИНН"] == NEW_INN


def test_corrected_kpp_goes_to_quote_whatever_the_model_passes(item):
    quote, text = dialog(item, f"ООО Ромашка, ИНН {OLD_INN}, КПП {OLD_KPP}", f"КПП другой: {NEW_KPP}",
                         inn=OLD_INN, kpp=OLD_KPP)
    assert f"ИНН{OLD_INN},КПП{NEW_KPP}" in text and OLD_KPP not in text


def test_old_inn_the_model_saw_does_not_replace_the_corrected(item):
    """Первый ИНН клиент написал группами цифр (модель видела его как есть), исправленный кодом скрыт меткой."""
    quote, text = dialog(item, "ИНН 77 0708 3893, КП на ООО Ромашка", f"Нет, правильный ИНН {NEW_INN}", inn=OLD_INN)
    assert NEW_INN in text and OLD_INN not in text


def test_later_inn_the_code_did_not_recognize_is_taken_from_the_model(item):
    """Исправленный ИНН группами цифр код не распознал: номер от модели принят, клиент назвал его последним."""
    quote, text = dialog(item, f"ИНН {OLD_INN}, КП на ООО Ромашка", "Ошибся, ИНН 77 2816 8971", inn=NEW_INN)
    assert NEW_INN in text and OLD_INN not in text


def test_single_inn_goes_to_quote_as_before(item):
    for passed in (OLD_INN, "[ИНН]", ""):
        quote, text = dialog(item, f"Нужно КП на ООО Ромашка, ИНН {OLD_INN}, КПП {OLD_KPP}", inn=passed)
        assert quote["customer_missing"] == [] and f"ИНН{OLD_INN},КПП{OLD_KPP}" in text


def test_inn_the_client_never_named_is_not_accepted(item):
    quote, text = dialog(item, "Нужно КП на ООО Ромашка", inn=NEW_INN, kpp=NEW_KPP)
    assert quote["customer_missing"] == ["ИНН", "КПП"] and NEW_INN not in text and NEW_KPP not in text
    quote, text = dialog(item, f"Нужно КП на ООО Ромашка, ИНН {OLD_INN}", inn=NEW_INN)
    assert OLD_INN in text and NEW_INN not in text


@pytest.mark.parametrize("message", [
    f"ИНН контрагента {OLD_INN}, наш ИНН {NEW_INN}",
    f"ИНН {OLD_INN}, а для договора ИНН/КПП {NEW_INN}/{NEW_KPP}",
    f"ИНН {OLD_INN} неверный, вот верный: ИНН {NEW_INN}",
])
def test_two_inn_in_one_message_the_last_is_kept(message):
    masked, found = loop.mask_requisites(message)
    assert found["inn"] == NEW_INN and OLD_INN not in masked and NEW_INN not in masked


def test_two_kpp_in_one_message_the_last_is_kept():
    found = loop.mask_requisites(f"реквизиты {NEW_INN} / {OLD_KPP}, КПП {NEW_KPP}")[1]
    assert found == {"inn": NEW_INN, "kpp": NEW_KPP}


def test_two_inn_in_one_message_model_cannot_pick_the_first(item):
    quote, text = dialog(item, f"КП на ООО Ромашка. ИНН контрагента {OLD_INN}, наш ИНН {NEW_INN}", inn=OLD_INN)
    assert NEW_INN in text and OLD_INN not in text


def test_quote_already_made_is_updated_with_the_new_inn(item):
    first, text = dialog(item, f"Нужно КП на ООО Ромашка, ИНН {OLD_INN}", inn=OLD_INN)
    assert OLD_INN in text
    res = loop.run_turn(quote_sid(first), f"Ошибся, ИНН {NEW_INN}", llm=Scripted([
        {"name": "make_quote", "args": {"items": [item], "inn": OLD_INN}}, "Черновик обновлен."]))
    (second,) = [t["output"] for t in res["trace"] if t["kind"] == "tool" and t["name"] == "make_quote"]
    assert second["updated"] and second["number"] == first["number"]
    text = pdf_text(second)
    assert NEW_INN in text and OLD_INN not in text


def test_said_at_finds_the_last_place_the_client_wrote_the_value():
    said = [f"ИНН {OLD_INN}", f"ИНН 77 2816 8971, а старый {OLD_INN}"]
    assert tools._said_at(said, "inn", OLD_INN) == (1, 27)
    assert tools._said_at(said, "inn", NEW_INN) == (1, 4)
    assert tools._said_at(said, "inn", "7701234567") is None
    assert tools._said_at(["Счет на ООО «Ромашка»"], "company", "ООО Ромашка") == (0, 0)


# --- B. Сверка состава КП: эталонной цены нет ---

WANT = [{"code": "KL4863RAPFS", "qty": 40}]


def kp_of(price) -> dict:
    """КП на одну позицию; price None: цена по запросу, строка без суммы."""
    line = {"offer_id": 4985, "name": "Kaspersky Endpoint Security для бизнеса Стандартный",
            "vendor_code": "KL4863RAPFS", "qty": 40, "price": price, "sum": None if price is None else price * 40}
    return {"ok": True, "items": [line], "total": line["sum"] or 0, "discount_percent": 0}


def test_missing_reference_price_fails_the_check():
    """Зачем: каталог не вернул цену (предложения нет, цена пустая), и сверка цены пропускалась без ошибки."""
    assert quote_check.mismatches(WANT, kp_of(2813.4), lambda _id: None) == [
        "эталонная цена не найдена: KL4863RAPFS (offer_id 4985), в каталоге нет такого предложения или у него нет цены"]


def test_price_on_request_in_quote_and_catalog_is_a_match():
    assert quote_check.mismatches(WANT, kp_of(None), lambda _id: None) == []


def test_reference_price_present_works_as_before():
    assert quote_check.mismatches(WANT, kp_of(2813.4), lambda _id: 2813.4) == []
    assert quote_check.mismatches(WANT, kp_of(2813.4), lambda _id: 3000.0) == [
        "KL4863RAPFS: цена 2813.4, в каталоге 3000.0"]
    assert quote_check.mismatches(WANT, kp_of(None), lambda _id: 2813.4) == [
        "KL4863RAPFS: цена 0.0, в каталоге 2813.4"]
    assert quote_check.mismatches(WANT, kp_of(2813.4)) == []  # без каталога цена не сверяется
