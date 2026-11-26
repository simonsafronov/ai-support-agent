"""Сборка базы каталога из YML-фида.

    python -m app.catalog.ingest [--feed PATH] [--db PATH]

Фид читается потоково (iterparse с очисткой), база строится во временный
файл рядом с целевым и атомарно заменяет его.
"""
from __future__ import annotations

import argparse
import html
import itertools
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from . import DEFAULT_DB, DEFAULT_FEED, DIMENSIONS, MULTI_SEP, RU_VENDORS, facet_counts, norm

# Фид отдается в двух разметках: выгрузка-файл и живой адрес магазина называют часть параметров по-разному
# ("К-во пользователей до" = 30 против "К-во пользователей" = "от 30 до 30"). Читаются обе: первое имя, которое есть.
PARAM_NAMES = {
    "users": ("К-во пользователей до", "К-во пользователей"),
    "user_type": ("Пользователь", "Конечный пользователь"),
    "platform": ("Совместимая архитектура/платформа", "Операционная система"),
}


def param(params: dict[str, list[str]], field: str) -> list[str]:
    return next((params[name] for name in PARAM_NAMES[field] if params.get(name)), [])


# Значения-пустышки в param: выбрасываются.
PLACEHOLDERS = {"", "другое или отсутствует", "не определено", "другое", "n/a"}

# Синонимы значений "Тип лицензии" в фиде (одно и то же записано по-разному).
LICENSE_ALIASES = {
    "full": "Полная версия",
    "upg": "Обновление",
    "модули": "Модуль",
    "maintenance": "Техподдержка",
    "support": "Техподдержка",
}
LANG_ALIASES = {"русская версия": "Русский"}

# Колонки offers в порядке вставки. Вторая группа заполняется после чтения фида.
OFFER_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY"), ("product_id", "TEXT"), ("name", "TEXT"), ("vendor", "TEXT"),
    ("vendor_code", "TEXT"), ("url", "TEXT"), ("price", "REAL"), ("oldprice", "REAL"),
    ("available", "INTEGER"), ("category_id", "INTEGER"), ("description", "TEXT"),
    ("product_type", "TEXT"), ("sales_notes", "TEXT"), ("params_json", "TEXT"),
    ("license_type", "TEXT"), ("term_months", "INTEGER"), ("term_raw", "TEXT"),
    ("users_max", "INTEGER"), ("nodes", "INTEGER"), ("nodes_min", "INTEGER"),
    ("qty_min", "INTEGER"), ("qty_max", "INTEGER"), ("qty", "TEXT"),
    ("org_type", "TEXT"), ("user_type", "TEXT"), ("version", "TEXT"), ("platform", "TEXT"),
    ("registry_no", "TEXT"), ("lang", "TEXT"), ("russian_vendor", "INTEGER"),
)
OFFER_DERIVED_COLUMNS = (
    ("qty_pricing", "TEXT"), ("same_code_prices", "TEXT"), ("price_ambiguous", "INTEGER DEFAULT 0"),
)

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE categories(
    id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT, path TEXT, depth INTEGER,
    n_products INTEGER DEFAULT 0, n_offers INTEGER DEFAULT 0, n_available INTEGER DEFAULT 0);
CREATE TABLE offers(%s);
CREATE TABLE products(
    product_id TEXT PRIMARY KEY, title TEXT, vendor TEXT, vendor_url TEXT,
    category_id INTEGER, category_path TEXT, product_type TEXT,
    n_offers INTEGER, n_available INTEGER, price_min REAL, price_max REAL,
    price_on_request INTEGER, registry_no TEXT, russian_vendor INTEGER, is_russian INTEGER,
    sample_url TEXT, facets_json TEXT);
CREATE VIRTUAL TABLE products_fts USING fts5(
    title, vendor, category_path, product_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE products_tri USING fts5(
    text, product_id UNINDEXED, tokenize='trigram');
CREATE VIRTUAL TABLE categories_fts USING fts5(
    name, path, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE offers_fts USING fts5(
    name, vendor_code, content='', tokenize='unicode61 remove_diacritics 2');
""" % ", ".join(f"{name} {kind}" for name, kind in OFFER_COLUMNS + OFFER_DERIVED_COLUMNS)

# offers(product_id) создается раньше: по нему собираются продукты.
INDEXES = """
CREATE INDEX offers_category ON offers(category_id);
CREATE INDEX offers_vendor_code ON offers(vendor_code COLLATE NOCASE)
    WHERE vendor_code IS NOT NULL;
CREATE INDEX products_category ON products(category_id);
CREATE INDEX categories_parent ON categories(parent_id);
"""

_SPACE_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"\d+")
_TERM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мес|год|лет|дн|ден|сут|нед)")
_TERM_UNIT_MONTHS = {
    "мес": 1.0, "год": 12.0, "лет": 12.0,
    "дн": 1 / 30.4375, "ден": 1 / 30.4375, "сут": 1 / 30.4375, "нед": 7 / 30.4375,
}
# Срок длиннее этого в фиде означает мусор в данных (например "4106.7762 мес.").
_TERM_MAX_MONTHS = 600
_PLATFORM_RE = re.compile(r"Платформа:\s*([^•]+)")
# Ступень количества: "Минимальная покупка: от 501 до 1000 шт." в описании
# и "Кол-во от 501 до 1000 шт." в sales_notes (там верхняя граница обрезана до 65535).
_QTY_RES = (re.compile(r"Минимальная покупка:\s*от\s+(\d+)(?:\s+до\s+(\d+))?\s*шт"),
            re.compile(r"Кол-во от\s+(\d+)(?:\s+до\s+(\d+))?\s*шт"))
_QTY_OVERFLOW = 65535


def clean(text: str | None) -> str:
    """Схлопывает пробелы и раскрывает HTML-сущности (в фиде встречается "&amp;amp;")."""
    if not text:
        return ""
    if "&" in text:
        text = html.unescape(text)
    return _SPACE_RE.sub(" ", text).strip()


def parse_term(values: list[str]) -> int | None:
    """"Срок действия" -> месяцы. Бессрочная -> 0, меньше полумесяца или мусор -> None."""
    for value in values:
        low = value.lower()
        if "бессроч" in low:
            return 0
        m = _TERM_RE.search(low)
        if not m:
            continue
        months = float(m.group(1).replace(",", ".")) * _TERM_UNIT_MONTHS[m.group(2)]
        if months < 0.5 or months > _TERM_MAX_MONTHS:
            continue
        return max(1, round(months))
    return None


def parse_count(values: list[str]) -> tuple[int | None, int | None]:
    """Число пользователей или узлов -> (нижняя, верхняя) граница яруса.

    В фиде ярус записан повтором param: либо все числа подряд (20, 21, ... 24),
    либо две границы (250, 499). Мусор вроде "000" дает (None, None).
    """
    numbers = [int(m) for value in values for m in _NUM_RE.findall(value.replace(" ", ""))]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def parse_qty(description: str, sales_notes: str) -> tuple[int | None, int | None]:
    """Ступень количества (от, до) для цены. "от 1 шт." и "от 1 до 1" ступенью не считаются."""
    for regex, text in zip(_QTY_RES, (description, sales_notes)):
        m = regex.search(text)
        if not m:
            continue
        low = int(m.group(1))
        high = int(m.group(2)) if m.group(2) else None
        if high is not None and (high >= _QTY_OVERFLOW or high < low):
            high = None
        if low <= 1 and high in (None, 1):
            return None, None
        return low, high
    return None, None


def qty_label(low: int | None, high: int | None) -> str | None:
    if low is None:
        return None
    return f"{low}+" if high is None else f"{low}-{high}"


def join_values(values: list[str], aliases: dict[str, str] | None = None) -> str | None:
    out: list[str] = []
    for value in values:
        parts = value.split(";") if aliases is LICENSE_ALIASES else [value]
        for part in parts:
            part = part.strip()
            if aliases:
                part = aliases.get(part.lower(), part)
            if part and part not in out:
                out.append(part)
    return MULTI_SEP.join(out) or None


def load_ru_vendors(path: Path = RU_VENDORS) -> set[str]:
    """Справочник российских вендоров: по одному на строку, "#" это комментарий."""
    if not path.exists():
        return set()
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def _read_offer(el: ET.Element) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Дочерние теги offer -> (простые поля, param без пустышек с единицами измерения)."""
    fields: dict[str, str] = {}
    params: dict[str, list[str]] = {}
    for ch in el:
        if ch.tag != "param":
            fields[ch.tag] = ch.text or ""
            continue
        value = clean(ch.text)
        if value.lower() in PLACEHOLDERS:
            continue
        if ch.get("unit"):
            value = f"{value} {ch.get('unit')}"
        values = params.setdefault(ch.get("name") or "", [])
        if value not in values:
            values.append(value)
    return fields, params


def _money(text: str | None) -> float | None:
    """Цена из фида; 0.00 означает "цена по запросу" и дает None."""
    try:
        value = float(text or 0)
    except ValueError:
        return None
    return value if value > 0 else None


def _quantity_columns(params: dict[str, list[str]], description: str, sales_notes: str) -> dict:
    """Ярусы варианта: пользователи, узлы, ступень количества для цены."""
    nodes_min, nodes_max = parse_count(params.get("К-во узлов", []))
    users_max = parse_count(param(params, "users"))[1]
    qty_min, qty_max = parse_qty(description, sales_notes)
    # ступень, повторяющая ярус узлов или пользователей, отдельным измерением не считается
    repeats_tier = (qty_min, qty_max) == (nodes_min, nodes_max) or (
        qty_max is not None and qty_max == users_max)
    return {
        "users_max": users_max,
        "nodes": nodes_max,
        "nodes_min": nodes_min if nodes_min != nodes_max else None,
        "qty_min": qty_min,
        "qty_max": qty_max,
        "qty": None if repeats_tier else qty_label(qty_min, qty_max),
    }


def _license_columns(params: dict[str, list[str]], description: str) -> dict:
    """Нормализованные параметры лицензии."""
    get = lambda name: params.get(name, [])  # noqa: E731
    # ОС указана только в описании ("Платформа: Windows"), в param лежит архитектура
    platform_match = _PLATFORM_RE.search(description)
    platform = [platform_match.group(1).strip()] if platform_match else []
    return {
        "license_type": join_values(get("Тип лицензии"), LICENSE_ALIASES),
        "term_months": parse_term(get("Срок действия")),
        "term_raw": join_values(get("Срок действия")),
        "org_type": join_values(get("Тип организации")),
        "user_type": join_values(param(params, "user_type")),
        "version": join_values(get("Версия")),
        "platform": join_values(platform + [v for v in param(params, "platform") if v not in platform]),
        "registry_no": join_values(get("Номер записи в реестре отечественного ПО")),
        "lang": join_values(get("Язык интерфейса"), LANG_ALIASES),
    }


def offer_row(el: ET.Element, ru_vendors: set[str]) -> tuple[dict, str, tuple[str, str]]:
    """offer из фида -> (значения колонок offers, vendorUrl, название и адрес линейки, если фид их дает)."""
    offer_id = int(el.get("id"))
    fields, params = _read_offer(el)
    text = {tag: clean(fields.get(tag)) for tag in (
        "name", "vendor", "vendorCode", "url", "description", "sales_notes", "group_id",
        "categoryId", "vendorUrl", "group_name", "group_url")}
    group = el.get("group_id") or text["group_id"]
    row = {
        "id": offer_id,
        "product_id": group or f"o{offer_id}",
        "name": text["name"],
        "vendor": text["vendor"],
        "vendor_code": text["vendorCode"] or next(iter(params.get("Артикул", [])), None),  # живой фид: артикул в param
        "url": text["url"],
        "price": _money(fields.get("price")),
        "oldprice": _money(fields.get("oldprice")),
        "available": int(el.get("available") == "true"),
        "category_id": int(text["categoryId"]) if text["categoryId"] else None,
        "description": text["description"] or None,
        # живой фид типа не дает; в выгрузке-файле "электроника" это ровно позиции без линейки (group_id)
        "product_type": join_values(params.get("Тип продукта", [])) or ("софт" if group else "электроника"),
        "sales_notes": text["sales_notes"] or None,
        "params_json": json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        "russian_vendor": int(text["vendor"] in ru_vendors),
    }
    row.update(_quantity_columns(params, text["description"], text["sales_notes"]))
    row.update(_license_columns(params, text["description"]))
    return row, text["vendorUrl"], (text["group_name"], text["group_url"])


def iter_feed(feed: Path):
    """Потоковое чтение фида: сначала ("date", дата), затем ("category" | "offer", элемент).

    Разобранный offer выбрасывается из дерева, как только вызывающий взял следующий.
    """
    context = ET.iterparse(str(feed), events=("start", "end"))
    _, root = next(context)
    yield "date", root.get("date") or ""
    for event, el in context:
        if event == "end" and el.tag in ("offer", "category"):
            yield el.tag, el
            if el.tag == "offer":
                root.clear()


def load_feed(conn: sqlite3.Connection, feed: Path, ru_vendors: set[str]) -> dict:
    """Читает фид: категории в память (их мало), offers пачками в базу."""
    categories: dict[int, tuple[int | None, str]] = {}
    vendor_urls: dict[str, str] = {}
    batch: list[tuple] = []
    groups: dict[str, tuple[str, str]] = {}  # product_id -> (название линейки, адрес) из group_name и group_url
    info = {"feed_date": "", "categories": categories, "vendor_urls": vendor_urls, "groups": groups,
            "offers_read": 0}
    names = [name for name, _ in OFFER_COLUMNS]
    insert = (f"INSERT OR REPLACE INTO offers({','.join(names)})"
              f" VALUES ({','.join('?' * len(names))})")
    for kind, el in iter_feed(feed):
        if kind == "date":
            info["feed_date"] = el
        elif kind == "category":
            parent = el.get("parentId")
            categories[int(el.get("id"))] = (int(parent) if parent else None, clean(el.text))
        else:
            row, vendor_url, group = offer_row(el, ru_vendors)
            if vendor_url:
                vendor_urls.setdefault(row["vendor"], vendor_url)
            if group[0]:
                groups.setdefault(row["product_id"], group)
            batch.append(tuple(row[name] for name in names))
            info["offers_read"] += 1
            if len(batch) >= 5000:
                conn.executemany(insert, batch)
                batch.clear()
    conn.executemany(insert, batch)
    return info


def category_paths(categories: dict[int, tuple[int | None, str]]) -> dict[int, tuple[str, int]]:
    cache: dict[int, tuple[str, int]] = {}

    def resolve(cid: int) -> tuple[str, int]:
        if cid in cache:
            return cache[cid]
        chain: list[str] = []
        seen: set[int] = set()
        cur: int | None = cid
        while cur is not None and cur in categories and cur not in seen:
            seen.add(cur)
            parent, name = categories[cur]
            chain.append(name)
            cur = parent
        cache[cid] = (" > ".join(reversed(chain)), len(chain) - 1)
        return cache[cid]

    return {cid: resolve(cid) for cid in categories}


# --- заголовок продукта и листовая категория --------------------------------
#
# В фиде offer привязан к категории второго уровня, а третий уровень (листья)
# это названия линеек продуктов без прямой привязки. Лист находим по вхождению
# его названия в имена вариантов; он же подсказывает, где в имени начинается
# название продукта (после юрлица вендора).

_TAIL_JUNK = " ,;:-–—/(+"


def majority_prefix(names: list[str], share: float = 0.7) -> str:
    """Самый длинный префикс по словам (без учета регистра), общий для доли имен."""
    if len(names) == 1:
        return names[0]
    words = [name.split(" ") for name in names]
    keys = [[w.lower().rstrip(",;:") for w in ws] for ws in words]
    need = share * len(names)
    alive = list(range(len(names)))
    pos = 0
    while True:
        counter = Counter(keys[i][pos] for i in alive if len(keys[i]) > pos)
        if not counter:
            break
        word, count = counter.most_common(1)[0]
        if count < need:
            break
        alive = [i for i in alive if len(keys[i]) > pos and keys[i][pos] == word]
        pos += 1
    return " ".join(words[alive[0]][:pos])


def _paren_depths(text: str) -> list[int]:
    """Глубина вложенности скобок перед каждым символом; последним элементом итоговая."""
    depths = []
    depth = 0
    for ch in text:
        if ch == ")":
            depth = max(0, depth - 1)
        depths.append(depth)
        if ch == "(":
            depth += 1
    return depths + [depth]


def cut_variant_tail(title: str, vendor: str = "", segments: int = 1) -> str:
    """Обрезает описание варианта: все после запятой вне скобок и незакрытую скобку.

    segments: сколько частей между запятыми оставить. Запятая внутри названия
    вендора в начале ("Tripwire, Inc. ...") не считается.
    """
    skip = len(vendor) if vendor and title.lower().startswith(vendor.lower()) else 0
    depths = _paren_depths(title[skip:])
    top_level = [skip + k for k, depth in enumerate(depths[:-1]) if depth == 0]
    commas = [i for i in top_level if title[i] == "," and i >= 3]
    if len(commas) >= segments:
        title = title[:commas[segments - 1]]
    elif depths[-1] > 0:
        title = title[:max(i for i in top_level if title[i] == "(")]
    return title.rstrip(_TAIL_JUNK)


def find_word(text: str, needle: str) -> int:
    """Позиция needle в text по границам слов, иначе -1."""
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return -1
        j = i + len(needle)
        if (i == 0 or not text[i - 1].isalnum()) and (j == len(text) or not text[j].isalnum()):
            return i
        start = i + 1


def match_leaf(names_norm: list[str], leaves: list[tuple[int, str, str]]) -> tuple | None:
    """Лист (id, название, норм. название), чье название есть в >= половине имен; самый длинный."""
    if not leaves:
        return None
    step = max(1, len(names_norm) // 8)
    sample = names_norm[::step][:8]
    best = None
    for leaf in leaves:
        needle = leaf[2]
        if len(needle) < 3 or not any(needle in name for name in sample):
            continue
        if best is not None and len(needle) <= len(best[2]):
            continue
        hits = sum(1 for name in names_norm if find_word(name, needle) >= 0)
        if hits * 2 >= len(names_norm):
            best = leaf
    return best


# Признаки того, что вендор записан юрлицом, а не брендом ("Tripwire, Inc.", "ООО «...»").
_LEGAL_RE = re.compile(
    r"(?<![^\W_])(inc|llc|ltd|gmbh|corp|corporation|co|ооо|ао|зао|оао|пао|фгуп|нпп|нтп|ип"
    r"|software|development|solutions|systems|technologies|technology|group)(?![^\W_])|[«»]",
    re.IGNORECASE)
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_OPENING = "«\"'“("


def strip_vendor(title: str, vendor: str) -> str:
    """Убирает вендора из начала заголовка, если без него название остается понятным.

    Убирается задвоенный вендор ("Adobe Systems Adobe InCopy" -> "Adobe InCopy") и
    вендор-юрлицо, если в остатке есть имя собственное (латинское слово или слово
    с заглавной буквы не в начале). Бренд из одного слова ("NAVITEL Колумбия") остается.
    """
    if not vendor or not title.lower().startswith(vendor.lower() + " "):
        return title
    rest = title[len(vendor) + 1:].lstrip(" .,:-")
    if len(rest) < 4:
        return title
    first = vendor.split(" ")[0].lower().strip(",.")
    if rest.lower().startswith(first):
        return rest
    if not _LEGAL_RE.search(vendor):
        return title
    words = rest.split(" ")
    has_name = bool(_LATIN_WORD_RE.search(rest)) or any(w[:1].isupper() for w in words[1:])
    return rest if has_name else title


def _title_from_leaf(prefix: str, leaf: tuple, segments: int, vendor: str = "") -> str:
    """Название начинается там, где в общем начале имен стоит название листовой категории.

    Имя вендора в начале пропускается: в "MDaemon Technologies, Ltd. MDaemon (лицензия ..."
    продукт это второе вхождение "MDaemon", а не начало названия компании.
    """
    prefix_norm = norm(prefix)
    pos = -1
    if len(prefix_norm) == len(prefix):
        skip = len(vendor) if vendor and prefix_norm.startswith(norm(vendor) + " ") else 0
        pos = find_word(prefix_norm[skip:], leaf[2]) if skip else -1
        pos = pos + skip if pos >= 0 else find_word(prefix_norm, leaf[2])
    if pos < 0:
        return leaf[1]
    if pos > 0 and prefix[pos - 1] in _OPENING:
        pos -= 1  # не отрываем открывающую кавычку от названия
    return cut_variant_tail(prefix[pos:], segments=segments)


def _title_from_names(names: list[str], prefix: str, vendor: str, segments: int) -> str:
    """Название из общего начала имен; если имена расходятся сразу, из самого частого начала."""
    title = strip_vendor(cut_variant_tail(prefix, vendor, segments), vendor)
    if len(title) >= 3 and norm(title) != norm(vendor):
        return title
    heads = Counter(cut_variant_tail(name.split(" (")[0], vendor) for name in names)
    return strip_vendor(heads.most_common(1)[0][0], vendor)


def product_title(names: list[str], vendor: str, leaf: tuple | None, segments: int = 1) -> str:
    prefix = majority_prefix(names)
    if leaf:
        title = _title_from_leaf(prefix, leaf, segments, vendor)
    else:
        title = _title_from_names(names, prefix, vendor, segments)
    if len(title) < 3 or norm(title) == norm(vendor):
        title = leaf[1] if leaf else cut_variant_tail(names[0], vendor) or names[0]
    return title


def widen_duplicate_titles(conn: sqlite3.Connection, products: list[dict]) -> int:
    """Одинаковые заголовки у разных продуктов вендора: берем еще одну часть имени."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in products:
        if not p["product_id"].startswith("o"):
            groups.setdefault((p["vendor"], norm(p["title"])), []).append(p)
    changed = 0
    for same in groups.values():
        if len(same) < 2:
            continue
        for p in same:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM offers WHERE product_id=? ORDER BY id", (p["product_id"],))]
            wider = product_title(names, p["vendor"], p["leaf"], segments=2)
            added = wider[len(p["title"]):].strip(" ,")
            # одно добавленное слово ("..., Тариф") названия не различает
            if wider.startswith(p["title"]) and " " in added and len(wider) <= 140:
                p["title"] = wider
                changed += 1
    return changed


# Варианты одной "лесенки" цен различаются только ступенью количества.
_LADDER_KEY = ("name", "term_months", "license_type", "org_type", "user_type", "version")


def _ladder_mode(prices: list[float]) -> str | None:
    """Цена по ступеням только падает: за единицу. Только растет: за всю ступень."""
    pairs = list(zip(prices, prices[1:]))
    if not pairs or prices[0] == prices[-1]:
        return None
    if all(a >= b for a, b in pairs):
        return "per_unit"
    if all(a <= b for a, b in pairs):
        return "per_tier"
    return None


def qty_pricing(offers: list[sqlite3.Row]) -> dict[int, str]:
    """Как понимать цену ступени количества, по ходу цен в лесенке: id offer -> режим.

    Цена падает с ростом количества: это цена за единицу ("per_unit").
    Цена растет: это цена за всю ступень ("per_tier"). Иначе режим не определен.
    """
    ladders: dict[tuple, list[sqlite3.Row]] = {}
    for o in offers:
        if o["qty_min"] is not None and o["price"] is not None:
            ladders.setdefault(tuple(o[k] for k in _LADDER_KEY), []).append(o)
    modes: dict[int, str] = {}
    for steps in ladders.values():
        steps.sort(key=lambda o: (o["qty_min"], o["qty_max"] or float("inf")))
        mode = _ladder_mode([o["price"] for o in steps])
        if mode and len({(o["qty_min"], o["qty_max"]) for o in steps}) > 1:
            modes.update((o["id"], mode) for o in steps)
    return modes


def _most_common(values) -> object | None:
    counted = Counter(v for v in values if v is not None).most_common(1)
    return counted[0][0] if counted else None


def _sales_summary(offers: list[sqlite3.Row]) -> dict:
    """Цены и доступность продукта по его вариантам."""
    prices = [o["price"] for o in offers if o["price"]]
    available = [o for o in offers if o["available"]]
    return {
        "n_offers": len(offers),
        "n_available": len(available),
        "price_min": min(prices, default=None),
        "price_max": max(prices, default=None),
        "price_on_request": int(len(prices) < len(offers)),
        "sample_url": (available or offers)[0]["url"],
    }


def product_record(product_id: str, offers: list[sqlite3.Row], leaves: dict, paths: dict,
                   group: tuple[str, str] | None = None) -> dict:
    """Продукт из его вариантов: заголовок, листовая категория, цены, счетчики значений.

    group это название и адрес линейки из фида: если они есть, заголовок берется оттуда, а не выводится из названий.
    """
    names = [o["name"] for o in offers]
    vendor = _most_common(o["vendor"] for o in offers)
    category_id = _most_common(o["category_id"] for o in offers)
    leaf = None
    title = names[0]  # товар без вариантов: имя уже и есть название
    if not product_id.startswith("o"):
        leaf = match_leaf([norm(n) for n in names], leaves.get(category_id, []))
        title = group[0] if group and group[0] else product_title(names, vendor, leaf)
        category_id = leaf[0] if leaf else category_id
    registry_no = _most_common(o["registry_no"] for o in offers)
    russian_vendor = offers[0]["russian_vendor"]
    return {
        "product_id": product_id, "title": title, "vendor": vendor, "leaf": leaf,
        "category_id": category_id, "path": paths.get(category_id, ("", 0))[0],
        "product_type": offers[0]["product_type"],
        "registry_no": registry_no, "russian_vendor": russian_vendor,
        "is_russian": int(bool(registry_no or russian_vendor)),
        "facets": json.dumps(facet_counts(offers), ensure_ascii=False, separators=(",", ":")),
        **_sales_summary(offers), **({"sample_url": group[1]} if group and group[1] else {}),
    }


def build_products(conn: sqlite3.Connection, paths: dict[int, tuple[str, int]],
                   categories: dict[int, tuple[int | None, str]],
                   vendor_urls: dict[str, str], groups: dict[str, tuple[str, str]] | None = None) -> int:
    leaves: dict[int, list[tuple[int, str, str]]] = {}
    for cid, (parent, name) in categories.items():
        if parent is not None:
            leaves.setdefault(parent, []).append((cid, name, norm(name)))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, product_id, name, vendor, url, price, available, category_id, product_type,"
        f" registry_no, russian_vendor, qty_min, qty_max, {', '.join(DIMENSIONS)}"
        " FROM offers ORDER BY product_id, id")
    products: list[dict] = []
    pricing: dict[int, str] = {}
    for product_id, group in itertools.groupby(cur, key=lambda r: r["product_id"]):
        offers = list(group)
        products.append(product_record(product_id, offers, leaves, paths, (groups or {}).get(product_id)))
        pricing.update(qty_pricing(offers))
    conn.row_factory = None
    widen_duplicate_titles(conn, products)
    conn.executemany("UPDATE offers SET qty_pricing = ? WHERE id = ?",
                     [(mode, offer_id) for offer_id, mode in pricing.items()])
    conn.executemany(
        "INSERT INTO products VALUES (" + ",".join("?" * 17) + ")",
        [(p["product_id"], p["title"], p["vendor"], vendor_urls.get(p["vendor"]),
          p["category_id"], p["path"], p["product_type"], p["n_offers"], p["n_available"],
          p["price_min"], p["price_max"], p["price_on_request"], p["registry_no"],
          p["russian_vendor"], p["is_russian"], p["sample_url"], p["facets"])
         for p in products])
    conn.executemany(
        "INSERT INTO products_fts(title, vendor, category_path, product_id) VALUES (?,?,?,?)",
        [(norm(p["title"]), norm(p["vendor"]), norm(p["path"]), p["product_id"])
         for p in products])
    conn.executemany(
        "INSERT INTO products_tri(text, product_id) VALUES (?,?)",
        [(norm(f"{p['title']} {p['vendor']}"), p["product_id"]) for p in products])
    return len(products)


# Чем варианты с одним артикулом могут отличаться по существу: количество, срок, вид
# лицензии, тип организации, редакция. Продукта здесь нет намеренно: один артикул
# в двух карточках каталога для клиента та же неоднозначность.
_CODE_SIGNATURE = ("qty", "users_max", "nodes", "nodes_min", "term_months", "license_type",
                   "org_type", "version")


def _same_item(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """Два варианта одного артикула неразличимы: вендор тот же, параметры не расходятся.

    Пустое значение параметра не считается отличием: в одной карточке он просто не заполнен.
    """
    if a["vendor"] != b["vendor"]:
        return False
    return all(a[k] is None or b[k] is None or a[k] == b[k] for k in _CODE_SIGNATURE)


def mark_same_code_prices(conn: sqlite3.Connection) -> dict:
    """Артикулы с несколькими ценами: всем их вариантам пишет список цен артикула.

    price_ambiguous ставится варианту, если у неотличимого от него варианта с тем же
    артикулом цена другая, то есть причину разницы из фида извлечь нельзя.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT id, vendor_code, vendor, price, {', '.join(_CODE_SIGNATURE)} FROM offers"
        " WHERE vendor_code IS NOT NULL AND price IS NOT NULL").fetchall()
    conn.row_factory = None
    rows.sort(key=lambda r: r["vendor_code"].lower())
    updates: list[tuple] = []
    counts = {"multi_price_codes": 0, "ambiguous_codes": 0, "ambiguous_offers": 0}
    for _, group in itertools.groupby(rows, key=lambda r: r["vendor_code"].lower()):
        offers = list(group)
        prices = sorted({o["price"] for o in offers})
        if len(prices) < 2:
            continue
        flags = [any(o["price"] != other["price"] and _same_item(o, other) for other in offers)
                 for o in offers]
        counts["multi_price_codes"] += 1
        counts["ambiguous_codes"] += any(flags)
        counts["ambiguous_offers"] += sum(flags)
        prices_json = json.dumps([int(p) if p.is_integer() else p for p in prices])
        updates += [(prices_json, int(flag), o["id"]) for o, flag in zip(offers, flags)]
    conn.executemany(
        "UPDATE offers SET same_code_prices = ?, price_ambiguous = ? WHERE id = ?", updates)
    return counts


def build_categories(conn: sqlite3.Connection, categories: dict[int, tuple[int | None, str]],
                     paths: dict[int, tuple[str, int]]) -> None:
    """Категории со счетчиками по поддереву (свои товары плюс все вложенные)."""
    own = {cid: [0, 0, 0] for cid in categories}
    for cid, n_products, n_offers, n_available in conn.execute(
            "SELECT category_id, COUNT(*), SUM(n_offers), SUM(n_available)"
            " FROM products GROUP BY category_id"):
        if cid in own:
            own[cid] = [n_products, n_offers, n_available]
    total = {cid: list(v) for cid, v in own.items()}
    # от глубоких к корню: к моменту обработки узла его поддерево уже просуммировано
    for cid in sorted(categories, key=lambda c: -paths[c][1]):
        parent = categories[cid][0]
        if parent in total and parent != cid:
            for i in range(3):
                total[parent][i] += total[cid][i]
    conn.executemany(
        "INSERT INTO categories VALUES (?,?,?,?,?,?,?,?)",
        [(cid, parent, name, paths[cid][0], paths[cid][1], *total[cid])
         for cid, (parent, name) in categories.items()])
    conn.executemany("INSERT INTO categories_fts(rowid, name, path) VALUES (?,?,?)",
                     [(cid, norm(name), norm(paths[cid][0]))
                      for cid, (_, name) in categories.items()])


_COUNTERS = {
    "offers": "SELECT COUNT(*) FROM offers",
    "products": "SELECT COUNT(*) FROM products",
    "categories": "SELECT COUNT(*) FROM categories",
    "available": "SELECT COUNT(*) FROM offers WHERE available = 1",
    "with_price": "SELECT COUNT(*) FROM offers WHERE price IS NOT NULL",
    "in_ru_registry": "SELECT COUNT(*) FROM products WHERE registry_no IS NOT NULL",
    "russian_vendor_products": "SELECT COUNT(*) FROM products WHERE russian_vendor = 1",
}


def fill(conn: sqlite3.Connection, feed: Path, log) -> dict:
    """Наполняет пустую базу из фида. Возвращает счетчики, они же ложатся в таблицу meta."""
    conn.executescript(SCHEMA)
    conn.execute("BEGIN")
    ru_vendors = load_ru_vendors()
    feed_info = load_feed(conn, feed, ru_vendors)
    categories = feed_info["categories"]
    log(f"фид прочитан: offers {feed_info['offers_read']}, categories {len(categories)},"
        f" российских вендоров в справочнике {len(ru_vendors)}")
    conn.execute("COMMIT")
    conn.executescript("CREATE INDEX offers_product ON offers(product_id);")
    conn.execute("BEGIN")
    paths = category_paths(categories)
    n_products = build_products(conn, paths, categories, feed_info["vendor_urls"], feed_info["groups"])
    log(f"продукты собраны: {n_products}")
    code_counts = mark_same_code_prices(conn)
    build_categories(conn, categories, paths)
    conn.create_function("norm", 1, norm, deterministic=True)
    # артикул ищется и по тегу vendorCode, и по param "Артикул": в живом фиде они бывают разными
    conn.execute("INSERT INTO offers_fts(rowid, name, vendor_code)"
                 " SELECT id, norm(name), norm(COALESCE(vendor_code, '') || ' ' ||"
                 " COALESCE(json_extract(params_json, '$.\"Артикул\"[0]'), '')) FROM offers")
    log("полнотекстовые индексы построены")
    counts = {key: conn.execute(sql).fetchone()[0] for key, sql in _COUNTERS.items()}
    counts.update(ru_vendors_in_list=len(ru_vendors), **code_counts,
                  feed_date=feed_info["feed_date"])
    meta = dict(counts, built_at=time.strftime("%Y-%m-%d %H:%M:%S"), feed_file=feed.name)
    conn.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    conn.execute("COMMIT")
    conn.executescript(INDEXES)
    conn.executescript("INSERT INTO products_fts(products_fts) VALUES('optimize');"
                       "INSERT INTO offers_fts(offers_fts) VALUES('optimize');"
                       "ANALYZE;")
    log("индексы построены")
    counts["duplicate_offer_ids"] = feed_info["offers_read"] - counts["offers"]
    return counts


def build(feed: Path = DEFAULT_FEED, db: Path = DEFAULT_DB, verbose: bool = True) -> dict:
    """Строит базу с нуля во временном файле и атомарно заменяет им целевой."""
    started = time.time()
    feed, db = Path(feed), Path(db)
    if not feed.exists():
        raise FileNotFoundError(f"нет фида: {feed}")
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(db.name + ".tmp")
    tmp.unlink(missing_ok=True)

    def log(message: str) -> None:
        if verbose:
            print(f"[{time.time() - started:6.1f}s] {message}", flush=True)

    conn = sqlite3.connect(tmp, isolation_level=None)
    try:
        conn.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-200000;"
            " PRAGMA temp_store=MEMORY;")
        counts = fill(conn, feed, log)
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, db)
    counts["db_bytes"] = db.stat().st_size
    counts["seconds"] = round(time.time() - started, 1)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка базы каталога из YML-фида")
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    counts = build(args.feed, args.db)
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
