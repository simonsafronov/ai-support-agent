"""Тесты каталога на реальной базе data/db/catalog.sqlite.

Запросы взяты из переписок поддержки. База собирается командой
`python -m app.catalog.ingest`; без нее тесты поиска пропускаются,
тесты разбора фида (чистые функции) идут всегда.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time

import pytest

from app.catalog import DEFAULT_DB, ingest, norm, tokens
from app.catalog.search import (browse_categories, find_analogs, get_offer, get_product,
                                search_products, stats)

needs_db = pytest.mark.skipif(not DEFAULT_DB.exists(), reason="нет базы каталога: python -m app.catalog.ingest")

# Запрос клиента -> подстроки, одна из которых должна быть в названии или вендоре
# продукта из первых пяти. Только то, что в фиде есть (проверено по именам offer).
CLIENT_QUERIES = {
    "ред ос": ["ред ос"],
    "red os": ["ред ос"],
    "kaspersky endpoint security": ["kaspersky endpoint security"],
    "касперский": ["kaspersky"],
    "redcheck": ["redcheck"],
    "power bi": ["power bi"],
    # версии 10 в фиде нет: есть "Альт Рабочая станция 11" и "Альт СП ... релиз 10"
    "альт рабочая станция 10": ["альт рабочая станция", "альт сп"],
    "photoshop": ["photoshop"],
    "фотошоп": ["photoshop"],
    "мой офис": ["мойофис"],
    "revit": ["revit"],
    # редакции Standard в фиде нет, сам MDaemon есть
    "MDaemon Standard": ["mdaemon"],
    "dr web": ["dr.web"],
    "доктор веб": ["доктор веб"],
    "астра линукс": ["astra linux"],
    "р7 офис": ["р7-офис"],
    "криптопро": ["криптопро"],
    "windows 11 pro": ["windows 11 pro"],
    "office 2021": ["office standard 2021", "office профессиональный 2021"],
    "autocad": ["autocad"],
    "компас 3d": ["компас-3d"],
    "битрикс24": ["битрикс24"],
    "яндекс 360": ["яндекс 360"],
    "ноутбук asus zenbook": ["zenbook"],
}
# Этих продуктов в фиде нет (есть только упоминания или другие продукты вендора):
# "maxpatrol vm", "websoft hcm", "1с бухгалтерия", "veeam", "zoom".


def _top(query: str, limit: int = 5, **filters) -> list[dict]:
    return search_products(query, limit=limit, **filters)["products"]


def _text(product: dict) -> str:
    return norm(product["title"] + " | " + product["vendor"])


# --- поиск продуктов ----------------------------------------------------------

@needs_db
@pytest.mark.parametrize("query, expected", CLIENT_QUERIES.items())
def test_client_query_finds_product_in_top5(query, expected):
    found = [_text(p) for p in _top(query)]
    assert any(e in text for e in expected for text in found), f"{query!r}: {found}"


@needs_db
@pytest.mark.parametrize("query, expected", [
    ("ред ос", "ред ос"),
    ("астра линукс", "astra linux"),
    ("windows 11 pro", "windows 11 pro"),
    ("redcheck", "redcheck"),
    ("ноутбук asus zenbook", "zenbook"),
])
def test_unambiguous_query_is_first(query, expected):
    assert expected in _text(_top(query, limit=1)[0])


@needs_db
@pytest.mark.parametrize("query, expected", [
    ("касперксий", "kaspersky"),   # перестановка букв
    ("kasperksy", "kaspersky"),
    ("фотошп", "photoshop"),       # пропущена буква, другой алфавит
    ("майкрософт", "microsoft"),   # транслит заимствованного слова
])
def test_typo_and_translit(query, expected):
    found = [_text(p) for p in _top(query)]
    assert any(expected in text for text in found), f"{query!r}: {found}"


@needs_db
def test_search_result_shape_and_size():
    result = search_products("касперский")
    assert set(result) == {"query", "total", "match", "products"}
    assert result["match"] == "all" and result["total"] >= len(result["products"]) == 8
    assert set(result["products"][0]) == {
        "product_id", "title", "vendor", "category_path", "n_offers", "n_available",
        "price_min", "price_max", "price_on_request", "in_ru_registry", "russian_vendor", "url"}
    assert len(json.dumps(result, ensure_ascii=False)) <= 4096


@needs_db
@pytest.mark.parametrize("query", ["альт рабочая станция 10", "касперский", "power bi"])
def test_top_results_do_not_depend_on_limit(query):
    ids = [p["product_id"] for p in _top(query, limit=20)]
    assert [p["product_id"] for p in _top(query, limit=3)] == ids[:3]


@needs_db
def test_search_says_when_match_is_only_partial():
    result = search_products("websoft hcm")  # WebSoft в фиде нет, есть другой HCM
    assert result["match"] == "partial"
    assert all("websoft" not in _text(p) for p in result["products"])


@needs_db
def test_search_says_when_nothing_found():
    result = search_products("qwertyzxcvb")
    assert result["match"] == "none" and result["total"] == 0 and result["products"] == []


@needs_db
def test_search_by_article_returns_offer():
    products = _top("KL4867RANFE")
    assert products[0]["offer_hit"]["vendor_code"] == "KL4867RANFE"
    assert "kaspersky endpoint security" in _text(products[0])


@needs_db
def test_search_filters():
    assert all(p["n_available"] > 0 for p in _top("office", limit=20, only_available=True))
    russian = _top("антивирус", limit=20, russian_only=True)
    assert russian and all(p["in_ru_registry"] or p["russian_vendor"] for p in russian)
    assert all("касперск" in norm(p["vendor"]) for p in _top("security", vendor="касперский"))
    hardware = _top("asus", product_type="электроника")
    assert hardware and all(p["vendor"] == "ASUS" for p in hardware)


@needs_db
def test_search_category_filter_covers_subtree():
    root = next(c for c in browse_categories("антивирус")["categories"] if c["has_children"])
    inside = search_products("security", category_id=root["id"], limit=20)["products"]
    assert inside and all(p["category_path"].startswith(root["path"]) for p in inside)
    listing = search_products("", category_id=root["id"])
    assert listing["match"] == "filters" and listing["total"] == root["n_products"]


@needs_db
def test_search_speed_on_warm_base():
    queries = list(CLIENT_QUERIES)[:20]
    for query in queries:
        search_products(query)
    timings = []
    for query in queries:
        started = time.perf_counter()
        search_products(query)
        timings.append((time.perf_counter() - started) * 1000)
    assert statistics.median(timings) <= 150 and max(timings) <= 500, timings


# --- варианты продукта ----------------------------------------------------------

def _product_with_facets(*dims: str) -> str:
    """Самый большой доступный продукт, у которого варианты различаются по этим измерениям."""
    conn = sqlite3.connect(f"{DEFAULT_DB.resolve().as_uri()}?mode=ro", uri=True)
    where = " AND ".join(f"""facets_json LIKE '%"{dim}"%'""" for dim in dims)
    row = conn.execute(f"SELECT product_id FROM products WHERE n_available > 0 AND {where}"
                       " ORDER BY n_offers DESC LIMIT 1").fetchone()
    conn.close()
    return row[0]


@needs_db
def test_get_product_users_and_term_narrow_and_clarify():
    product_id = _product_with_facets("users_max", "term_months", "license_type")
    everything = get_product(product_id)
    assert everything["matching"] == everything["total_offers"] > 10
    assert everything["clarify"] and set(everything["clarify"]) <= set(everything["facets"])
    assert everything["clarify"][0] == "users_max"  # сильнее всего дробит варианты

    narrowed = get_product(product_id, {"users": 20, "term_months": 12})
    assert 0 < narrowed["matching"] < everything["total_offers"]
    assert "users_max" not in narrowed["clarify"] and "term_months" not in narrowed["clarify"]
    assert narrowed["clarify"], "после количества и срока осталось что уточнять"
    offers = narrowed["offers"]
    assert offers and all(o["term_months"] == 12 for o in offers)
    assert all(o.get("users_max") is None or o["users_max"] >= 20 for o in offers)
    assert offers[0]["users_max"] == min(o["users_max"] for o in offers)  # ближайший ярус первым
    assert offers[0]["available"] and offers[0]["price"] is not None


@needs_db
def test_get_product_nodes_picks_tier_containing_number():
    product_id = search_products("kaspersky endpoint security для бизнеса расширенный",
                                 limit=1)["products"][0]["product_id"]
    result = get_product(product_id, {"nodes": 22, "term_months": 12})
    assert result["offers"]
    assert all(o["nodes_min"] <= 22 <= o["nodes"] for o in result["offers"])
    below_any_tier = get_product(product_id, {"nodes": 3})
    assert below_any_tier["tier_notes"][0]["nearest_tier_from"] > 3


@needs_db
def test_get_product_quantity_tier_and_clarify():
    product_id = _product_with_facets("qty")
    everything = get_product(product_id)
    assert "qty" in everything["clarify"]
    tier = everything["facets"]["qty"][0]["value"]  # например "1-5" или "500+"
    need = int(tier.rstrip("+").split("-")[0])
    for key in ("qty", "users"):
        result = get_product(product_id, {key: need})
        assert "qty" not in result["clarify"]
        tiers = [o for o in result["offers"] if "qty_min" in o]
        assert tiers and all(o["qty_min"] <= need <= o.get("qty_max", need) for o in tiers)


@needs_db
def test_get_product_text_and_availability_filters():
    product_id = _product_with_facets("license_type")
    result = get_product(product_id, {"license_type": "продлен", "only_available": True})
    assert result["offers"]
    assert all("продлен" in o["license_type"].lower() and o["available"] for o in result["offers"])
    assert get_product("no-such-product") == {"error": "product_not_found",
                                              "product_id": "no-such-product"}


@needs_db
def test_same_article_with_two_prices_is_flagged():
    products = _top("LBS-AC-12M-25-A3")
    hits = [p["offer_hit"] for p in products if "offer_hit" in p]
    assert len(hits) == 2 and hits[0]["price"] != hits[1]["price"]
    for hit in hits:
        assert hit["price_ambiguous"] is True
        assert sorted(h["price"] for h in hits) == hit["same_code_prices"]
    offer = get_offer(hits[0]["offer_id"])
    assert offer["price_ambiguous"] is True and offer["product_title"]
    assert offer["params"]["Срок действия"] == ["12 мес."]
    assert get_offer(-1)["error"] == "offer_not_found"


# --- категории, аналоги, счетчики -----------------------------------------------

@needs_db
def test_browse_categories():
    found = browse_categories("антивирус")["categories"]
    assert found and "антивирус" in norm(found[0]["name"])
    assert browse_categories("antivirus")["categories"][0]["id"] == found[0]["id"]  # транслит
    roots = browse_categories()["categories"]
    assert roots and all(" > " not in c["path"] for c in roots)
    counts = [c["n_available"] for c in roots]
    assert counts == sorted(counts, reverse=True)
    children = browse_categories(parent_id=found[0]["id"])["categories"]
    assert children and all(c["path"].startswith(found[0]["path"] + " > ") for c in children)


@needs_db
def test_find_analogs_for_foreign_antivirus_are_russian():
    foreign = next(p for p in _top("f-secure business suite") if p["vendor"] == "F-Secure")
    assert not foreign["in_ru_registry"] and not foreign["russian_vendor"]
    result = find_analogs(foreign["product_id"])
    assert "антивирус" in norm(result["basis"]["path"])
    assert result["products"]
    assert all(p["in_ru_registry"] or p["russian_vendor"] for p in result["products"])
    assert all(p["vendor"] != "F-Secure" for p in result["products"])
    counts = [p["n_available"] for p in result["products"]]
    assert counts == sorted(counts, reverse=True)
    any_vendor = find_analogs(foreign["product_id"], russian_only=False)
    assert foreign["product_id"] not in {p["product_id"] for p in any_vendor["products"]}


@needs_db
def test_stats():
    result = stats()
    assert result["offers"] == 136040
    assert result["available"] == 44601 and result["with_price"] == 136040 - 80269
    assert result["russian_vendor_products"] > result["in_ru_registry"] > 0
    assert result["feed_date"].startswith("2026-")


@needs_db
def test_results_are_json_serializable():
    product_id = _top("касперский", limit=1)[0]["product_id"]
    for result in (search_products("офис"), get_product(product_id), find_analogs(product_id),
                   browse_categories("офис"), stats()):
        json.dumps(result, ensure_ascii=False)


# --- разбор фида (без базы) -----------------------------------------------------

@pytest.mark.parametrize("values, months", [
    (["12 мес."], 12), (["бессрочная"], 0), (["бессрочная (ФСБ/ФСТЭК)"], 0),
    (["1 год"], 12), (["3 года"], 36), (["11.8275 мес."], 12), (["372 мес."], 372),
    (["мес."], None), (["5 дн."], None), (["0.0329 мес."], None), (["4106.7762 мес."], None),
    ([], None),
])
def test_parse_term(values, months):
    assert ingest.parse_term(values) == months


def test_parse_count_and_quantity_tiers():
    assert ingest.parse_count(["20", "21", "22", "23", "24"]) == (20, 24)
    assert ingest.parse_count(["250", "499"]) == (250, 499)
    assert ingest.parse_count(["000"]) == (None, None)
    assert ingest.parse_qty("Минимальная покупка: от 501 до 1000 шт.", "") == (501, 1000)
    assert ingest.parse_qty("", "Кол-во от 26 шт.") == (26, None)
    assert ingest.parse_qty("", "Кол-во от 10000 до 65535 шт.") == (10000, None)
    assert ingest.parse_qty("Минимальная покупка: от 1 шт.", "Кол-во от 1 до 1 шт.") == (None, None)
    assert ingest.qty_label(50, 99) == "50-99" and ingest.qty_label(500, None) == "500+"


def test_product_title_from_offer_names():
    names = ["Adobe Systems Adobe InCopy CC (лицензии для школ), лицензия, 12 мес.",
             "Adobe Systems Adobe InCopy CC (лицензии для школ), лицензия, 24 мес."]
    assert ingest.product_title(names, "Adobe Systems", None) == "Adobe InCopy CC (лицензии для школ)"
    leaf = (1, "ALD Pro", "ald pro")
    names = ["Группа Астра Программный комплекс ALD Pro, серверные лицензии",
             "Группа Астра Сертификаты на техническую поддержку для ALD Pro, тип Стандарт"]
    assert ingest.product_title(names, "Группа Астра", leaf) == "ALD Pro"
    names = ["Доктор Веб Комплексная защита, без компонентов на 1 год на 10 ПК",
             "Доктор Веб Комплексная защита + Центр управления на 1 год на 86 ПК"]
    assert ingest.product_title(names, "Доктор Веб", None) == "Доктор Веб Комплексная защита"
    assert ingest.cut_variant_tail("Tripwire, Inc. Device Profiler (лицензии), Ev Express",
                                   "Tripwire, Inc.") == "Tripwire, Inc. Device Profiler (лицензии)"


def test_text_normalization():
    assert norm("  Сontent  AI Ёж ") == "content ai еж"  # русская "С" в латинском слове
    assert tokens("Dr.Web Р7-Офис 1С:Бухгалтерия КОМПАС-3D") == [
        "dr", "web", "р7", "офис", "1с", "бухгалтерия", "компас", "3d"]


def test_article_is_found_inside_a_sentence():
    result = search_products("здравствуйте, можете оформить кп на артикул: kl4863rapfs 40 лицензий")
    assert result["products"], result
    hit = result["products"][0].get("offer_hit") or {}
    assert (hit.get("vendor_code") or "").lower() == "kl4863rapfs"


def test_long_polite_message_still_finds_the_product():
    result = search_products("добрый день интересует возможность приобретения лицензии kaspersky endpoint "
                             "security для бизнеса расширенный сроком на 3 года для 100 пользователей")
    assert "kaspersky endpoint security" in result["products"][0]["title"].lower()
