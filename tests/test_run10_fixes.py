"""Правки по итогам десятого проверочного прогона: число в названии продукта не сумма, новые лицензии раньше
продлений в выдаче вариантов, недостающие реквизиты КП не повод для обращения менеджеру."""
import pytest

from app.agent import prices
from app.catalog.search import _offer_order, get_product, search_products

pytestmark = pytest.mark.small_catalog


def test_number_inside_product_title_is_not_a_sum():
    """Зачем: ответ "Стоимость Яндекс 360 для бизнеса: цена по запросу" проверка цен вернула модели, и название
    продукта из ответа пропало."""
    named: set = set()
    prices.collect_named({"products": [{"title": "Яндекс 360 для бизнеса"}],
                          "offers": [{"name": "Kaspersky Security на 500 узлов"}]}, named)
    assert named == {"яндекс 360"}  # "на 500 узлов" это количество, а не название
    text = "Стоимость Яндекс 360 для бизнеса: цена по запросу."
    assert prices.unknown(text, set()) == ["360"]  # без названий из инструментов число после слова о цене ловится
    assert prices.unknown(text, set(), named=named) == []
    assert prices.mask_unknown(text, set(), named=named) == text
    assert prices.unknown("Итого выйдет 360 за все.", set(), named=named) == ["360"]  # то же число вне названия
    assert prices.unknown("Яндекс 360 стоит 360 руб.", set(), named=named) == ["360 руб."]  # с валютой это сумма


def _row(i, price, license_type):
    return {"id": i, "price": price, "available": 1, "license_type": license_type}


def test_new_licenses_go_before_renewals_when_type_is_not_asked():
    rows = [_row(1, 100, "Дистрибутив"), _row(2, 150, "Продление, Подписка"), _row(3, 300, "Полная версия"),
            _row(4, 500, None), _row(5, 200, "Подписка")]
    assert [r["id"] for r in sorted(rows, key=_offer_order({}, new_first=True))] == [5, 3, 4, 1, 2]
    assert [r["id"] for r in sorted(rows, key=_offer_order({}))] == [1, 2, 5, 3, 4]  # тип назван: только цена


def test_get_product_puts_renewal_after_new_license():
    """У "Кибер Бэкап Стандартный" продление для сервера дешевле новой лицензии для сервера."""
    pid = search_products("Кибер Бэкап Стандартный", limit=1)["products"][0]["product_id"]
    kinds = [o.get("license_type") for o in get_product(pid, {})["offers"]]
    assert kinds.index("Продление") == len(kinds) - 1
    only = get_product(pid, {"license_type": "Продление"})["offers"]
    assert [o.get("license_type") for o in only] == ["Продление"]


def test_new_license_filter_means_any_purchase_type():
    """Зачем: фильтр "новая" искал только значение "Полная версия" и давал ноль у продуктов, где новая лицензия
    записана в фиде как "Базовая" или "Подписка"."""
    pid = search_products("Кибер Бэкап Стандартный", limit=1)["products"][0]["product_id"]
    new = get_product(pid, {"license_type": "новая лицензия"})
    assert new["matching"] == 4 and "Продление" not in [o.get("license_type") for o in new["offers"]]
    assert "dropped_filter" not in new
