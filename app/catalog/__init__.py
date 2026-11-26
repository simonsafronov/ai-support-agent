"""Каталог товаров: сборка SQLite из YML-фида (ingest) и поиск (search).

Здесь лежит только общее для сборки и поиска: пути, список измерений
вариантов и нормализация текста. Функции поиска подгружаются лениво:
    from app.catalog import search_products, get_product
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FEED = Path(os.environ.get("CATALOG_FEED", ROOT / "data" / "raw" / "feed.xml"))
DEFAULT_DB = Path(os.environ.get("CATALOG_DB", ROOT / "data" / "db" / "catalog.sqlite"))
# Справочник российских вендоров (составлен вручную, см. шапку файла).
RU_VENDORS = ROOT / "data" / "ru_vendors.txt"

# Измерения, по которым различаются варианты (offer) одного продукта.
# Порядок = приоритет уточнения у клиента при прочих равных.
DIMENSIONS = (
    "users_max", "nodes", "qty", "term_months", "license_type", "org_type",
    "user_type", "version", "platform", "lang",
)
NUMERIC_DIMS = ("users_max", "nodes", "term_months")
# qty это ступень количества для цены, записана текстом: "50-99" или "500+".
MULTI_SEP = "; "

_TOKEN_RE = re.compile(r"[^\W_]+")
_SPACE_RE = re.compile(r"\s+")

# Буквы-двойники кириллицы и латиницы (после приведения к нижнему регистру:
# "С"/"C", "Р"/"P", "Х"/"X" и т.д.). В фиде встречаются слова со смешанным
# алфавитом ("Сontent" с русской "С"), их приводим к одному алфавиту.
_LOOKALIKE_LAT = "abcehkmoptxy"
_LOOKALIKE_CYR = "авсенкмортху"
TO_CYR = str.maketrans(_LOOKALIKE_LAT, _LOOKALIKE_CYR)
TO_LAT = str.maketrans(_LOOKALIKE_CYR, _LOOKALIKE_LAT)
_MIXED_WORD_RE = re.compile(
    r"[a-zа-я]*(?:[a-z][a-zа-я]*[а-я]|[а-я][a-zа-я]*[a-z])[a-zа-я]*")


def _fix_mixed(match: re.Match) -> str:
    word = match.group(0)
    lat = [ch for ch in word if "a" <= ch <= "z"]
    cyr = [ch for ch in word if "а" <= ch <= "я"]
    minority, table, alphabet = (
        (cyr, TO_LAT, _LOOKALIKE_CYR) if len(cyr) <= len(lat) else (lat, TO_CYR, _LOOKALIKE_LAT))
    if all(ch in alphabet for ch in minority):
        return word.translate(table)
    return word


def norm(text: str | None) -> str:
    """Нижний регистр, ё -> е, схлопнутые пробелы, слова со смешанным алфавитом к одному.

    Одинаково для индекса и запроса.
    """
    if not text:
        return ""
    text = _SPACE_RE.sub(" ", text.lower().replace("ё", "е")).strip()
    return _MIXED_WORD_RE.sub(_fix_mixed, text)


def tokens(text: str | None) -> list[str]:
    """Токены так же, как их режет FTS5 unicode61: буквы и цифры, остальное разделитель."""
    return _TOKEN_RE.findall(norm(text))


def split_multi(value) -> list:
    """Значение измерения -> список отдельных значений ("A; B" -> ["A", "B"])."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v for v in value.split(MULTI_SEP) if v]
    return [value]


def qty_bounds(label: str) -> tuple[int, float]:
    """"50-99" -> (50, 99), "500+" -> (500, inf)."""
    low, _, high = label.rstrip("+").partition("-")
    return int(low), float(high) if high else float("inf")


def facet_counts(rows, dims=DIMENSIONS) -> dict[str, list[dict]]:
    """Счетчики различных значений по измерениям; только измерения с >= 2 значениями.

    rows: итерируемое из dict-подобных строк offers. Числовые измерения
    сортируются по значению, текстовые по убыванию счетчика.
    """
    counters: dict[str, dict] = {d: {} for d in dims}
    for row in rows:
        for d in dims:
            for v in split_multi(row[d]):
                c = counters[d]
                c[v] = c.get(v, 0) + 1
    out: dict[str, list[dict]] = {}
    for d in dims:
        c = counters[d]
        if len(c) < 2:
            continue
        if d in NUMERIC_DIMS:
            items = sorted(c.items())
        elif d == "qty":
            items = sorted(c.items(), key=lambda kv: qty_bounds(kv[0]))
        else:
            items = sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))
        out[d] = [{"value": v, "count": n} for v, n in items]
    return out


_SEARCH_API = (
    "search_products", "get_product", "get_offer", "browse_categories",
    "find_analogs", "stats",
)


def __getattr__(name: str):
    if name in _SEARCH_API:
        from . import search
        return getattr(search, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
