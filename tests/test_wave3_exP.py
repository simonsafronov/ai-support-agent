"""Отличия вариантов с одинаковым названием и срок поставки."""
import pytest

from app.agent import loop
from app.catalog import search as catalog

ASTRA = "121231"
ASTRA_WORKSTATION = {"version": "Орел", "org_type": "коммерческая", "term_months": 12, "license_type": "новая",
                     "text": "рабочей станции"}


@pytest.mark.parametrize("raw, term", [
    ("Поставка в электронном виде. Срок доставки: от 1 рабочего дня.", "в электронном виде, от 1 раб.дн."),
    ("Срок доставки: 1-3 раб.дн. Softline.", "1-3 раб.дн."),
    ("Срок доставки: 5‒5 раб.дн. с момента поступления оплаты.", "5 раб.дн. после оплаты"),
    ("Срок доставки по Москве: от 5 рабочих дней после подтверждения оплаты; по России: от 10 рабочих дней после "
     "подтверждения оплаты. По вопросам приобретения предыдущих коробочных версий обращайтесь к менеджерам Softline.",
     "по Москве от 5 раб.дн., по России от 10 раб.дн. после оплаты"),
])
def test_delivery_term_is_short(raw, term):
    assert catalog.delivery_term({"Особенности доставки": [raw]}) == term
    assert catalog.delivery_term({}) is None


def test_only_parameters_that_differ_and_are_not_visible_yet():
    params = [{"Артикул": ["A1"], "Тип техподдержки": ["Базовая"], "Срок действия": ["12 мес."], "Язык": ["RU"]},
              {"Артикул": ["A2"], "Тип техподдержки": ["Стандартная"], "Срок действия": ["24 мес."], "Язык": ["RU"]}]
    assert catalog.distinctions(params) == [{"Тип техподдержки": "Базовая", "Срок действия": "12 мес."},
                                            {"Тип техподдержки": "Стандартная", "Срок действия": "24 мес."}]
    # срок модель уже видит полем term_months, он делит варианты так же: второй раз не нужен
    shown = [{"term_months": 12}, {"term_months": 24}]
    assert catalog.distinctions(params, shown) == [{"Тип техподдержки": "Базовая"}, {"Тип техподдержки": "Стандартная"}]


def test_platform_listed_once():
    assert catalog._platform("Windows 10/Windows 11; Windows 10, Windows 11; Linux") == "Windows 10/Windows 11; Linux"


@pytest.mark.needs_data
def test_same_name_variants_differ_by_details_and_delivery_is_given_once():
    res = catalog.get_product(ASTRA, ASTRA_WORKSTATION)
    offers = {o["offer_id"]: o for o in res["offers"]}
    assert offers[517989]["name"] == offers[489925]["name"] and offers[517989]["price"] != offers[489925]["price"]
    assert offers[517989]["details"] == {"Размер организации": "Малый и средний бизнес", "Тип техподдержки": "Базовая"}
    assert offers[489925]["details"]["Тип техподдержки"] == "Стандартная"
    assert res["delivery"] == "от 10 раб.дн. после оплаты"
    assert not any("delivery" in o or "russian_vendor" in o for o in res["offers"])


@pytest.mark.needs_data
def test_offer_cards_with_the_same_title_carry_their_difference():
    _, cards, _ = loop.render("Базовая [[offer:517989]], стандартная [[offer:489925]], другая [[offer:490281]].")
    by_id = {c["id"]: c for c in cards}
    assert by_id[517989]["details"] == {"Размер организации": "Малый и средний бизнес", "Тип техподдержки": "Базовая"}
    assert by_id[489925]["details"]["Размер организации"] == "Крупный бизнес"
    assert "details" not in by_id[490281]  # название у карточки свое, отличать ее не от чего
