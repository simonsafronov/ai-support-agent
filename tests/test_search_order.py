"""Порядок фильтров подбора на синтетическом каталоге: требования клиента раньше выбора ближайшего пакета.

У продукта "Стриж Резервное копирование" (tests/data/feed_small.xml) два пакета: до 25 пользователей
под Windows и до 50 пользователей под Linux.
"""
import pytest

from app.catalog.search import get_product, search_products

pytestmark = pytest.mark.small_catalog


def _strizh() -> str:
    return search_products("Стриж Резервное копирование", limit=1)["products"][0]["product_id"]


def test_platform_applies_before_nearest_package():
    """Зачем: пакет на 25 под Windows вытеснял подходящий пакет на 50 под Linux, и клиент слышал
    предложение снять требование к ОС."""
    result = get_product(_strizh(), {"users": 20, "platform": "Linux"})
    assert [o["vendor_code"] for o in result["offers"]] == ["STRIZH-L-50"]
    assert result["matching"] == 1
    assert "dropped_filter" not in result and "note" not in result


def test_nearest_package_still_wins_without_other_requirements():
    result = get_product(_strizh(), {"users": 20})
    assert [o["vendor_code"] for o in result["offers"]] == ["STRIZH-W-25"]  # на 20 пользователей не пакет на 50


def test_relaxed_hint_uses_the_same_order():
    """Срока 24 месяца нет ни у одного пакета: подсказка снимает срок, а не ОС, и показывает Linux на 50."""
    result = get_product(_strizh(), {"users": 20, "platform": "Linux", "term_months": 24})
    assert result["matching"] == 0
    assert result["dropped_filter"] == "term_months"
    assert [o["vendor_code"] for o in result["offers"]] == ["STRIZH-L-50"]


def _row(i, users_max=None):
    return {"id": i, "users_max": users_max, "nodes": None, "nodes_min": None, "qty_min": None, "qty_max": None}


def test_oversized_pack_is_not_offered_when_single_licenses_exist():
    """На 9 пользователей пакет на 500 не предлагается, если лицензии продаются и без пакета."""
    from app.catalog import search
    rows, _, _ = search._filter_quantity([_row(1), _row(2, 500)], "users", 9)
    assert [r["id"] for r in rows] == [1]
    rows, _, _ = search._filter_quantity([_row(1), _row(2, 25)], "users", 20)  # близкий пакет остается
    assert [r["id"] for r in rows] == [1, 2]
    rows, _, _ = search._filter_quantity([_row(2, 500)], "users", 9)  # другого варианта нет: пакет остается
    assert [r["id"] for r in rows] == [2]
    rows, _, _ = search._filter_quantity([_row(1), _row(2, 10)], "users", 1)  # на одного пакет на 10 еще уместен
    assert [r["id"] for r in rows] == [1, 2]
