"""Поиск по базе каталога (data/db/catalog.sqlite).

Все функции возвращают JSON-сериализуемые dict и держат ответ компактным:
он идет в контекст LLM. База открывается только на чтение, соединение свое
у каждого потока и переоткрывается, если файл базы заменили новой сборкой.

Ручная проверка:
    python -m app.catalog.search search "касперский endpoint" --limit 5
    python -m app.catalog.search product 556 --users 20 --term 12
    python -m app.catalog.search categories "антивирус"
    python -m app.catalog.search analogs 556
    python -m app.catalog.search stats
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import threading
from functools import lru_cache
from pathlib import Path

import snowballstemmer

from . import DEFAULT_DB, DIMENSIONS, NUMERIC_DIMS, TO_CYR, TO_LAT, facet_counts, norm, tokens

# --- соединение -------------------------------------------------------------

_local = threading.local()


def _db_path() -> Path:
    return Path(os.environ.get("CATALOG_DB") or DEFAULT_DB)


def _conn() -> sqlite3.Connection:
    path = _db_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"нет базы каталога {path}: соберите ее командой python -m app.catalog.ingest") from None
    signature = (str(path), st.st_ino, st.st_mtime_ns)
    cached = getattr(_local, "conn", None)
    if cached and cached[0] == signature:
        return cached[1]
    if cached:
        cached[1].close()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")
    _local.conn = (signature, conn)
    return conn


# --- нормализация запроса ---------------------------------------------------

_stem_lock = threading.Lock()
_stem_ru = snowballstemmer.stemmer("russian")
_stem_en = snowballstemmer.stemmer("english")

_RU_EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Как заимствованные слова пишутся в оригинале: линукс -> linux, фотошоп -> photoshop,
# виндовс -> windows, доктор -> doctor, майкрософт -> microsoft. Правило применимо
# не к каждому слову, поэтому пробуются оба написания.
_RU_EN_LOAN = (("кс", "x"), ("ай", "i"), ("ф", "ph"), ("в", "w"), ("к", "c"))
_TRANSLIT_MAX = 16
_EN_RU_MULTI = (
    ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ph", "ф"),
    ("th", "т"), ("ts", "ц"), ("ck", "к"), ("ya", "я"), ("yu", "ю"), ("yo", "е"),
    ("ce", "се"), ("ci", "си"), ("cy", "си"),
)
_EN_RU = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
    "y": "и", "z": "з",
}
_CYR_RE = re.compile(r"[а-я]")
_LAT_RE = re.compile(r"[a-z]")
_LETTER_DIGIT_RE = re.compile(r"[^\W\d_]+|\d+")


def _ru_to_en(word: str) -> list[str]:
    """Латинские написания русского слова: побуквенное первым, затем с правилами заимствований."""
    pieces: list[tuple[str, ...]] = []
    i = 0
    while i < len(word):
        for src, loan in _RU_EN_LOAN:
            if word.startswith(src, i):
                plain = "".join(_RU_EN.get(ch, ch) for ch in src)
                pieces.append((plain, loan))
                i += len(src)
                break
        else:
            pieces.append((_RU_EN.get(word[i], word[i]),))
            i += 1
    variants = [""]
    for options in pieces:
        variants = [v + option for v in variants for option in options][:_TRANSLIT_MAX]
    return list(dict.fromkeys(variants))


def _en_to_ru(word: str) -> list[str]:
    out = []
    i = 0
    while i < len(word):
        for src, dst in _EN_RU_MULTI:
            if word.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(_EN_RU.get(word[i], word[i]))
            i += 1
    return ["".join(out)]


def _translit(word: str) -> list[str]:
    """Запись слова другим алфавитом; для слова без букв или со смесью алфавитов пусто."""
    cyr, lat = bool(_CYR_RE.search(word)), bool(_LAT_RE.search(word))
    if cyr == lat:
        return []
    return _ru_to_en(word) if cyr else _en_to_ru(word)


def _stem(word: str) -> str:
    """Основа слова, обрезанная так, чтобы оставаться префиксом слова (kaspersky -> kaspersk)."""
    if _CYR_RE.search(word):
        stemmer = _stem_ru
    elif _LAT_RE.search(word):
        stemmer = _stem_en
    else:
        return word
    with _stem_lock:
        stem = stemmer.stemWord(word)
    n = 0
    while n < min(len(stem), len(word)) and stem[n] == word[n]:
        n += 1
    return word[:n] if n >= 3 else word


def _lookalike(word: str) -> str | None:
    """То же слово буквами-двойниками другого алфавита: "1с" -> "1c", "р7" -> "p7"."""
    for table in (TO_LAT, TO_CYR):
        other = word.translate(table)
        if other != word and not (_CYR_RE.search(other) and _LAT_RE.search(other)):
            return other
    return None


_NATIVE, _FOREIGN = 1.0, 0.8  # вес совпадения: как написал клиент или другим алфавитом
_PREFIX_MIN = 4               # короче этого слово ищется только целиком
_MAX_TOKENS = 40              # столько слов сообщения разбираем
_FOCUS_FROM = 6               # фраза длиннее этого сужается до слов, которые есть в каталоге
_FOCUS_KEEP = 12              # столько слов остается в запросе


@lru_cache(maxsize=4096)
def _variants(token: str) -> tuple[tuple[str, bool, float], ...]:
    """Варианты токена для поиска: (строка, искать ли как префикс, вес совпадения).

    Короткие токены и токены с цифрами (1с, r7, 2021) ищутся только целиком,
    остальные по основе слова как по префиксу. К каждому добавляется запись
    другим алфавитом, она весит меньше.
    """
    found: dict[str, tuple[bool, float]] = {}

    def add(term: str | None, prefix: bool, weight: float) -> None:
        if term and term not in found:
            found[term] = (prefix and len(term) >= _PREFIX_MIN, weight)

    if len(token) < _PREFIX_MIN or any(ch.isdigit() for ch in token):
        add(token, False, _NATIVE)
        add(_lookalike(token), False, _NATIVE)
        for other in _translit(token):
            add(other, False, _FOREIGN)
    else:
        stem = _stem(token)
        add(stem, True, _NATIVE)
        if len(stem) < _PREFIX_MIN:
            add(token, True, _NATIVE)
        for other in _translit(stem):
            add(other, True, _FOREIGN)
    return tuple((term, prefix, weight) for term, (prefix, weight) in found.items())


_doc_tokens = lru_cache(maxsize=65536)(lambda text: tuple(tokens(text)))


class Query:
    """Разобранный запрос: по группе вариантов на каждый токен."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = tokens(text)[:_MAX_TOKENS]
        self.groups: list[dict] = []
        for i, token in enumerate(self.tokens):
            terms = {term: (prefix, weight) for term, prefix, weight in _variants(token)}
            # слитное написание соседних слов: "мой офис" -> "мойофис", "dr web" -> "drweb"
            for a, b in ((i - 1, i), (i, i + 1)):
                if a >= 0 and b < len(self.tokens):
                    joined = self.tokens[a] + self.tokens[b]
                    if not joined.isdigit():
                        terms.setdefault(joined, (len(joined) >= _PREFIX_MIN, _NATIVE))
            # раздельное написание: "битрикс24" -> "битрикс 24"
            phrases = {}
            for form, weight in ((token, _NATIVE), *((t, _FOREIGN) for t in _translit(token))):
                parts = tuple(_LETTER_DIGIT_RE.findall(form))
                if len(parts) > 1:
                    phrases.setdefault(parts, weight)
            self.groups.append({"token": token, "terms": terms, "phrases": phrases})
        # вес слова в оценке; редкие слова весят больше (см. weigh)
        self.weights = [1.0] * len(self.groups)
        self.absent: list[str] = []  # слова запроса, которых нет ни в одном названии, вендоре и категории

    def weigh(self, conn: sqlite3.Connection) -> None:
        """Веса слов по редкости в названиях продуктов: "mdaemon" важнее, чем "standard"."""
        if len(self.groups) < 2:
            return
        n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        known = []
        for i, group in enumerate(self.groups):
            df = conn.execute("SELECT COUNT(*) FROM products_fts WHERE products_fts MATCH ?",
                              (self._group_fts(group),)).fetchone()[0]
            known.append(df > 0)
            self.weights[i] = math.log(1 + n / (df + 0.5))
        if len(self.groups) <= _FOCUS_FROM:  # в длинной фразе неизвестные слова это приветствия, не название
            self.absent = [g["token"] for g, ok in zip(self.groups, known) if not ok]
        self._focus(known)

    def _focus(self, known: list[bool]) -> None:
        """Длинная фраза клиента ("добрый день, подскажите стоимость ...") сужается до слов каталога.

        Слова, которых нет ни в одном названии, вендоре и категории, в длинной фразе это
        приветствия и просьбы, а не опечатки: они не должны перевешивать название продукта.
        Из остальных остаются самые редкие. Короткий запрос не трогаем: в нем неизвестное
        слово скорее опечатка, ее ловит нечеткий шаг.
        """
        if len(self.groups) <= _FOCUS_FROM or not any(known):
            self.groups, self.tokens, self.weights = (
                self.groups[:_FOCUS_KEEP], self.tokens[:_FOCUS_KEEP], self.weights[:_FOCUS_KEEP])
            return
        order = sorted((i for i, ok in enumerate(known) if ok), key=lambda i: -self.weights[i])
        keep = sorted(order[:_FOCUS_KEEP])
        self.groups = [self.groups[i] for i in keep]
        self.tokens = [self.tokens[i] for i in keep]
        self.weights = [self.weights[i] for i in keep]

    @staticmethod
    def _group_fts(group: dict) -> str:
        parts = [f'"{term}"*' if prefix else f'"{term}"'
                 for term, (prefix, _) in group["terms"].items()]
        parts += ['"' + " ".join(phrase) + '"' for phrase in group["phrases"]]
        return "(" + " OR ".join(parts) + ")"

    def fts(self, operator: str) -> str:
        return f" {operator} ".join(self._group_fts(g) for g in self.groups)

    def coverage(self, doc_tokens: tuple[str, ...]) -> list[float]:
        """Для каждого токена запроса: вес лучшего совпадения среди токенов документа (0 = нет)."""
        doc_set = set(doc_tokens)
        return [max(self._best_term(group, doc_tokens, doc_set),
                    self._best_phrase(group, doc_tokens)) for group in self.groups]

    @staticmethod
    def _best_term(group: dict, doc_tokens: tuple[str, ...], doc_set: set[str]) -> float:
        best = 0.0
        for term, (prefix, weight) in group["terms"].items():
            if weight > best and (
                    term in doc_set or (prefix and any(t.startswith(term) for t in doc_tokens))):
                best = weight
        return best

    @staticmethod
    def _best_phrase(group: dict, doc_tokens: tuple[str, ...]) -> float:
        best = 0.0
        for phrase, weight in group["phrases"].items():
            n = len(phrase)
            if weight > best and any(doc_tokens[i:i + n] == phrase
                                     for i in range(len(doc_tokens) - n + 1)):
                best = weight
        return best

    def fuzzy_forms(self) -> dict[int, list[str]]:
        """Номер токена -> его написания для нечеткого сравнения (слова от 5 букв)."""
        return {i: [token, *_translit(token)] for i, token in enumerate(self.tokens)
                if len(token) >= 5 and not any(ch.isdigit() for ch in token)}


def _trigrams(word: str) -> set[str]:
    return {word[i:i + 3] for i in range(len(word) - 2)}


def _edit_distance(a: str, b: str) -> int:
    """Расстояние Дамерау-Левенштейна: вставка, удаление, замена, перестановка соседних."""
    before, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if before is not None and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], before[j - 2] + 1)
        before, prev = prev, cur
    return prev[-1]


def _similarity(form: str, word: str) -> float:
    """Похожесть слов с опечаткой: 0, если ошибок больше одной на 5-7 букв и двух на 8 и больше."""
    allowed = 1 if len(form) < 8 else 2
    if abs(len(form) - len(word)) > allowed:
        return 0.0
    grams_a, grams_b = _trigrams(form), _trigrams(word)
    if 2 * len(grams_a & grams_b) < 0.3 * (len(grams_a) + len(grams_b)):
        return 0.0
    distance = _edit_distance(form, word)
    return 1 - distance / len(form) if distance <= allowed else 0.0


# --- продукты ---------------------------------------------------------------

_PRODUCT_COLS = ("p.product_id, p.title, p.vendor, p.category_id, p.category_path, p.product_type,"
                 " p.n_offers, p.n_available, p.price_min, p.price_max, p.price_on_request,"
                 " p.registry_no, p.russian_vendor, p.is_russian, p.sample_url")
_TYPE_ALIASES = {
    "software": "софт", "soft": "софт", "по": "софт",
    "hardware": "электроника", "electronics": "электроника", "железо": "электроника",
}
# вес слова запроса по месту, где оно нашлось
_W_TITLE, _W_VENDOR, _W_CATEGORY, _W_OFFER, _W_FUZZY = 1.0, 0.8, 0.35, 0.6, 0.9


def _money(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 2)


def _product_dict(row: sqlite3.Row) -> dict:
    return {
        "product_id": row["product_id"],
        "title": row["title"],
        "vendor": row["vendor"],
        # третий уровень пути это линейка продукта, она повторяет название
        "category_path": " > ".join(row["category_path"].split(" > ")[:2]),
        "n_offers": row["n_offers"],
        "n_available": row["n_available"],
        "price_min": _money(row["price_min"]),
        "price_max": _money(row["price_max"]),
        "price_on_request": bool(row["price_on_request"]),
        "in_ru_registry": row["registry_no"] is not None,
        "russian_vendor": bool(row["russian_vendor"]),
        "url": row["sample_url"],
    }


def _subtree_ids(conn: sqlite3.Connection, category_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "WITH RECURSIVE sub(id) AS (SELECT id FROM categories WHERE id=?"
        " UNION SELECT c.id FROM categories c JOIN sub ON c.parent_id=sub.id)"
        " SELECT id FROM sub", (int(category_id),))]


def _product_filters(conn, vendor, category_id, only_available, russian_only,
                     product_type) -> tuple[str, list]:
    """SQL-условия на таблицу products p и их параметры."""
    where, params = [], []
    if vendor:
        vendor_query = Query(vendor)
        if vendor_query.groups:
            where.append("p.product_id IN (SELECT product_id FROM products_fts"
                         " WHERE products_fts MATCH ?)")
            params.append("vendor : (" + vendor_query.fts("AND") + ")")
    if category_id is not None:
        ids = _subtree_ids(conn, category_id)
        where.append(f"p.category_id IN ({','.join(map(str, ids)) or 'NULL'})")
    if only_available:
        where.append("p.n_available > 0")
    if russian_only:
        where.append("p.is_russian = 1")
    if product_type:
        key = norm(product_type)
        where.append("p.product_type = ?")
        params.append(_TYPE_ALIASES.get(key, key))
    return "".join(" AND " + w for w in where), params


def _score(query: Query, row: sqlite3.Row, offer_hit: bool = False,
           fuzzy: list[float] | None = None) -> tuple[float, float]:
    """Оценка продукта: (покрытие слов запроса от 0 до 1, итог с бонусами)."""
    title_tokens = _doc_tokens(row["title"])
    in_title = query.coverage(title_tokens)
    in_vendor = query.coverage(_doc_tokens(row["vendor"]))
    in_category = query.coverage(_doc_tokens(row["category_path"]))
    total = 0.0
    for i, word_weight in enumerate(query.weights):
        total += word_weight * max(
            in_title[i] * _W_TITLE, in_vendor[i] * _W_VENDOR, in_category[i] * _W_CATEGORY,
            _W_OFFER if offer_hit else 0.0, _W_FUZZY * fuzzy[i] if fuzzy else 0.0)
    coverage = total / (sum(query.weights) or 1.0)
    score = coverage * 10.0
    if any(in_vendor):
        score += 0.5  # запрос называет вендора: его продукты выше чужих с тем же словом
    if title_tokens:
        # точность: какая доля заголовка названа в запросе
        named = sum(1 for hit in in_title if hit)
        score += 1.5 * named / max(len(title_tokens), len(query.groups))
        if named and named >= len(title_tokens):
            # заголовок назван целиком ("MDaemon" в запросе "MDaemon Standard"): базовый продукт
            # выше его дополнений, у которых названа только часть заголовка
            score += 0.5 * len(title_tokens) / len(query.groups)
    if row["n_available"]:
        score += 0.8 + 0.25 * math.log10(1 + row["n_available"])
    return coverage, score


class _Candidates:
    """Найденные продукты с оценками; продукт учитывается один раз, первым нашедшим шагом."""

    def __init__(self, query: Query):
        self.query = query
        self.items: dict[str, dict] = {}

    def __len__(self) -> int:
        return len(self.items)

    def add(self, row: sqlite3.Row, offer_hit: bool = False, fuzzy: list[float] | None = None,
            extra: dict | None = None, floor: float = 0.0) -> bool:
        """Добавляет продукт, если его еще нет и покрытие не ниже floor."""
        if row["product_id"] in self.items:
            return False
        coverage, score = _score(self.query, row, offer_hit, fuzzy)
        if coverage < floor:
            return False
        self.items[row["product_id"]] = {
            "coverage": coverage, "score": score, "row": row, "extra": extra}
        return True

    def best(self) -> float:
        return max((v["coverage"] for v in self.items.values()), default=0.0)

    def keep_best(self, share: float) -> None:
        """Оставляет продукты с покрытием не ниже доли от лучшего: остальное шум."""
        floor = self.best() * share - 1e-9
        self.items = {pid: v for pid, v in self.items.items() if v["coverage"] >= floor}

    def relevant(self, share: float = 0.8) -> int:
        """Сколько продуктов подходят под запрос примерно так же хорошо, как лучший."""
        floor = self.best() * share - 1e-9
        return sum(1 for v in self.items.values() if v["coverage"] >= floor)

    def top(self, limit: int) -> list[dict]:
        ranked = sorted(self.items.values(), key=lambda v: (
            -v["score"], -v["row"]["n_available"], v["row"]["title"]))
        out = []
        for v in ranked[:limit]:
            item = _product_dict(v["row"])
            if v["extra"]:
                item["offer_hit"] = v["extra"]
            out.append(item)
        return out


_FTS_SQL = (f"SELECT {_PRODUCT_COLS} FROM products_fts f JOIN products p"
            " ON p.product_id = f.product_id WHERE products_fts MATCH ?{where}"
            " ORDER BY bm25(products_fts, 10.0, 4.0, 1.0) LIMIT 300")
_PARTIAL_FLOOR = 0.5  # частичное совпадение рядом с полным: не меньше половины веса слов
_PARTIAL_SHARE = 0.75  # без полного совпадения: не хуже этой доли от лучшего


_ARTICLE_RE = re.compile(r"[^\s,;:()\[\]«»\"']+")


def _article_words(text: str | None) -> list[str]:
    """Слова, похожие на артикул: буквы вместе с цифрами, от 5 знаков ("KL4863RAPFS", "LBS-AC-12M-25-A3")."""
    words = []
    for word in _ARTICLE_RE.findall(text or ""):
        word = word.strip(".-/")
        if (len(word) >= 5 and any(ch.isdigit() for ch in word) and any(ch.isalpha() for ch in word)
                and word not in words):
            words.append(word)
    return words


def _find_by_article(conn, text: str, where: str, params: list, found: _Candidates) -> None:
    """Слова запроса, похожие на артикул, проверяются как точный артикул варианта.

    Артикул ловится и внутри фразы: "оформить кп на артикул: kl4863rapfs 40 лицензий".

    Один артикул бывает у нескольких вариантов и продуктов: в offer_hit попадает
    самый дешевый доступный вариант продукта, все цены артикула в same_code_prices.
    """
    for code in _article_words(text)[:4]:
        hits = conn.execute(
            f"SELECT o.id, o.name, o.vendor_code, o.price, o.available, o.same_code_prices,"
            f" o.price_ambiguous, {_PRODUCT_COLS}"
            " FROM offers o JOIN products p ON p.product_id = o.product_id"
            f" WHERE o.vendor_code = ? COLLATE NOCASE{where}"
            " ORDER BY o.available DESC, o.price IS NULL, o.price LIMIT 20", (code, *params))
        for hit in hits:
            extra = {"offer_id": hit["id"], "name": hit["name"], "vendor_code": hit["vendor_code"],
                     "price": _money(hit["price"]), "available": bool(hit["available"])}
            extra.update(_price_flags(hit))
            found.add(hit, offer_hit=True, extra=extra)


def _find_all_words(conn, q: Query, where: str, params: list, found: _Candidates) -> None:
    """Все слова запроса в названии, вендоре или категории продукта."""
    for row in conn.execute(_FTS_SQL.format(where=where), (q.fts("AND"), *params)):
        found.add(row)


def _find_in_offers(conn, q: Query, where: str, params: list, found: _Candidates) -> None:
    """Все слова запроса в имени или артикуле варианта.

    Фильтры (вендор, категория, наличие) стоят внутри выборки, до LIMIT: иначе общее слово занимает все 80 мест
    продуктами других вендоров, и с фильтром ответ пустой.
    """
    for row in conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM (SELECT o.product_id AS pid, COUNT(*) AS hits"
            " FROM offers_fts JOIN offers o ON o.id = offers_fts.rowid"
            f" JOIN products p ON p.product_id = o.product_id WHERE offers_fts MATCH ?{where}"
            " GROUP BY o.product_id ORDER BY hits DESC LIMIT 80) m"
            " JOIN products p ON p.product_id = m.pid",
            (q.fts("AND"), *params)):
        found.add(row, offer_hit=True)


def _find_any_word(conn, q: Query, where: str, params: list, found: _Candidates,
                   floor: float) -> None:
    """Часть слов запроса в продукте."""
    for row in conn.execute(_FTS_SQL.format(where=where), (q.fts("OR"), *params)):
        found.add(row, floor=floor)


def _find_fuzzy(conn, q: Query, where: str, params: list, found: _Candidates,
                floor: float) -> None:
    """Слова с опечаткой или в транслите: кандидаты по триграммам, проверка по числу ошибок."""
    forms_by_token = q.fuzzy_forms()
    grams = sorted({g for forms in forms_by_token.values() for f in forms for g in _trigrams(f)})
    if not grams:
        return
    for row in conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products_tri f JOIN products p"
            " ON p.product_id = f.product_id WHERE products_tri MATCH ?" + where +
            " ORDER BY bm25(products_tri) LIMIT 150",
            (" OR ".join(f'"{g}"' for g in grams), *params)):
        if row["product_id"] in found.items:
            continue
        words = [t for t in _doc_tokens(row["title"] + " " + row["vendor"]) if len(t) >= 4]
        similarity = [0.0] * len(q.tokens)
        for index, forms in forms_by_token.items():
            similarity[index] = max(
                (_similarity(form, word) for form in forms for word in words), default=0.0)
        if any(similarity):
            found.add(row, fuzzy=similarity, floor=floor)


def _list_by_filters(conn, query: str, where: str, params: list, limit: int) -> dict:
    """Пустой запрос: продукты под фильтры, доступные и крупные первыми."""
    rows = conn.execute(
        f"SELECT {_PRODUCT_COLS} FROM products p WHERE 1=1{where}"
        " ORDER BY p.n_available DESC, p.n_offers DESC LIMIT ?", (*params, limit)).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM products p WHERE 1=1{where}", params).fetchone()[0]
    return {"query": query, "total": total, "match": "filters",
            "products": [_product_dict(r) for r in rows]}


_ENOUGH = 8  # столько найденных продуктов достаточно, чтобы не добирать менее точным шагом


def _collect(conn, q: Query, where: str, params: list) -> tuple[_Candidates, str]:
    """Шаги поиска от точного к нечеткому. Возвращает найденное и чем оно найдено.

    От limit набор шагов не зависит: первые строки выдачи одинаковы при любом limit.
    """
    q.weigh(conn)
    found = _Candidates(q)
    _find_by_article(conn, q.text, where, params, found)
    _find_all_words(conn, q, where, params, found)
    if len(found) < _ENOUGH:
        _find_in_offers(conn, q, where, params, found)
    match = "all" if found else "none"
    if len(found) < _ENOUGH and len(q.groups) > 1:
        match = _widen(_find_any_word, "partial", match, conn, q, where, params, found)
    if len(found) < 3:
        match = _widen(_find_fuzzy, "fuzzy", match, conn, q, where, params, found)
    return found, match


def _widen(step, label: str, match: str, conn, q: Query, where: str, params: list,
           found: _Candidates) -> str:
    """Добирает менее точным шагом. Рядом с уже найденным берет только близкое по покрытию;
    если до этого не было ничего, оставляет лучший ярус найденного и меняет match на label."""
    if found:
        step(conn, q, where, params, found, floor=_PARTIAL_FLOOR)
        return match
    step(conn, q, where, params, found, floor=0.0)
    found.keep_best(_PARTIAL_SHARE)
    return label if found else match


def search_products(query: str, vendor: str | None = None, category_id: int | None = None,
                    only_available: bool = False, russian_only: bool = False,
                    product_type: str | None = None, limit: int = 8) -> dict:
    """Продукты по запросу клиента.

    Порядок: точный артикул; все слова запроса в названии, вендоре и категории;
    все слова в именах и артикулах вариантов; часть слов; если нашлось меньше трех,
    слова с опечаткой и в транслите по триграммам.

    В ответе match говорит, чем найден список: "all" (есть продукты со всеми словами
    запроса), "partial" (только с частью слов), "fuzzy" (только похожие слова),
    "none" (ничего не нашлось), "filters" (пустой запрос, список по фильтрам).
    total это число продуктов, подходящих примерно так же хорошо, как лучший.
    """
    conn = _conn()
    limit = max(1, min(int(limit), 50))
    q = Query(query or "")
    where, params = _product_filters(conn, vendor, category_id, only_available, russian_only,
                                     product_type)
    if not q.groups:
        return _list_by_filters(conn, query, where, params, limit)

    found, match = _collect(conn, q, where, params)
    result = {"query": query, "total": found.relevant(), "match": match, "products": found.top(limit)}
    if match == "none":
        result["note"] = ("По этим словам в каталоге ничего нет. Общее название класса ПО в названиях товаров "
                          "встречается редко: повторите поиск по названию конкретного продукта этого класса или "
                          "найдите раздел через browse_categories.")
    if match in ("partial", "fuzzy"):
        result["note"] = "Точного совпадения нет: в найденном совпала только часть слов запроса или похожие слова."
        if q.absent:
            result["note"] += (" Слов " + ", ".join(q.absent) + " нет ни в одном названии каталога: возможно, такого "
                               "продукта в каталоге нет, и найденное это другие продукты.")
    return result


# --- варианты продукта --------------------------------------------------------

_OFFER_DIMS = ("license_type", "term_months", "users_max", "nodes", "org_type", "user_type",
               "version", "platform")
_OFFER_EXTRA = ("nodes_min", "qty_min", "qty_max", "qty_pricing")
_SUBSTRING_FILTERS = ("license_type", "org_type", "user_type", "version", "platform")
_QUANTITY_FILTERS = ("users", "nodes", "qty")
_OFFER_SQL = ("SELECT id, name, vendor_code, price, available, url, sales_notes, russian_vendor,"
              " nodes_min, qty_min, qty_max, qty_pricing, same_code_prices, price_ambiguous, "
              + ", ".join(DIMENSIONS) + " FROM offers WHERE product_id = ?")
_FACET_LIMIT = 12
_VALUE_LIMIT = 60
_INF = float("inf")


def _short(value, limit: int = _VALUE_LIMIT):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit - 1].rstrip() + "…"
    return value


def _price_flags(row: sqlite3.Row) -> dict:
    """Пометки для артикула с несколькими ценами."""
    flags = {}
    if row["same_code_prices"]:
        flags["same_code_prices"] = json.loads(row["same_code_prices"])
    if row["price_ambiguous"]:
        flags["price_ambiguous"] = True
    return flags


def _offer_dict(row: sqlite3.Row) -> dict:
    """Вариант для списка: пустые необязательные поля опускаются ради размера ответа."""
    item = {"offer_id": row["id"], "name": row["name"]}
    if row["vendor_code"]:
        item["vendor_code"] = row["vendor_code"]
    item.update(price=_money(row["price"]), available=bool(row["available"]), url=row["url"])
    for key in _OFFER_DIMS + _OFFER_EXTRA:
        if row[key] is not None:
            item[key] = _short(row[key], 200 if key == "platform" else _VALUE_LIMIT)  # ОС нужны целиком
    item["russian_vendor"] = bool(row["russian_vendor"])
    item.update(_price_flags(row))
    price_for = _price_for(row)
    if price_for:
        item["price_for"] = price_for
    return item


def _price_for(row: sqlite3.Row) -> str | None:
    """За что цена варианта, словами: за одну лицензию в ступени количества или за комплект."""
    if row["price"] is None:
        return None
    if row["qty_pricing"] == "per_unit":
        top = f"до {row['qty_max']}" if row["qty_max"] else "и больше"
        return f"за 1 лицензию при покупке от {row['qty_min']} {top}"
    if row["qty_pricing"] == "per_tier":
        return "за весь комплект этой ступени"
    if row["users_max"] and row["qty_min"] is None:
        return f"за комплект на {row['users_max']} пользователей"
    return "за одну позицию, как в названии"


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tier_band(row: sqlite3.Row, key: str) -> tuple[int, float] | None:
    """Ступень от-до, в которую должно попасть количество, если она у варианта есть."""
    if key == "nodes" and row["nodes_min"] is not None:
        return row["nodes_min"], row["nodes"]
    if row["qty_min"] is not None:
        return row["qty_min"], row["qty_max"] or _INF
    return None


def _tier_gap(row: sqlite3.Row, key: str, need: int) -> float | None:
    """Подходит ли вариант под нужное количество: запас яруса, None если не подходит.

    У варианта со ступенью от-до количество должно в нее попасть. Иначе работает
    ярус "до N": users_max для users, nodes для nodes, он должен быть не меньше.
    Вариант без ярусов подходит всегда (запас inf).
    """
    band = _tier_band(row, key)
    if band:
        return 0 if band[0] <= need <= band[1] else None
    limit = {"users": row["users_max"], "nodes": row["nodes"]}.get(key)
    if limit is None:
        return _INF
    return limit - need if limit >= need else None


def _filter_quantity(rows: list, key: str, need: int) -> tuple[list, dict, dict | None]:
    """Оставляет варианты под количество need. Возвращает (варианты, запасы, подсказка).

    Если ни одна ступень количество не покрывает, но есть ступени, начинающиеся выше,
    оставляет ближайшую из них и объясняет это подсказкой.
    """
    gaps = {r["id"]: _tier_gap(r, key, need) for r in rows}
    kept = [r for r in rows if gaps[r["id"]] is not None]
    finite = [gaps[r["id"]] for r in kept if gaps[r["id"]] != _INF]
    if finite:
        # из ярусов "до N" нужен ближайший достаточный: на 50 пользователей не предлагаем пакет на 100
        nearest = min(finite)
        kept = [r for r in kept if gaps[r["id"]] in (nearest, _INF)]
        return kept, gaps, None
    bands = [(_tier_band(r, key), r) for r in rows if gaps[r["id"]] is None]
    higher = [(band[0], r) for band, r in bands if band and band[0] > need]
    if not higher:
        return kept, gaps, None
    nearest = min(floor for floor, _ in higher)
    for r in kept:  # ступени под это количество нет: вариант без ступени (цена за одну лицензию) подходит лучше всего
        gaps[r["id"]] = 0
    for floor, r in higher:
        if floor == nearest:
            kept.append(r)
            gaps[r["id"]] = floor - need
    return kept, gaps, {"filter": key, "requested": need, "nearest_tier_from": nearest}


def _apply_quantity_filters(rows: list, filters: dict) -> tuple[list, dict, list]:
    """Фильтры количества users, nodes, qty. Возвращает (варианты, запас яруса по id, подсказки)."""
    gaps: dict[int, float] = {}
    notes = []
    for key in _QUANTITY_FILTERS:
        need = _as_int(filters.get(key))
        if need is None:
            continue
        rows, key_gaps, note = _filter_quantity(rows, key, need)
        for offer_id, gap in key_gaps.items():
            if gap is not None:
                gaps[offer_id] = min(gaps.get(offer_id, _INF), gap)
        if note:
            notes.append(note)
    return rows, gaps, notes


_VALUE_SYNONYMS = {  # как клиент называет значение -> как оно записано в каталоге
    "org_type": (("образоват", "Академическая"), ("учебн", "Академическая"), ("школ", "Академическая"),
                 ("вуз", "Академическая"), ("универс", "Академическая"), ("гос", "Государственная"),
                 ("бюджет", "Государственная"), ("муницип", "Государственная"), ("нко", "Некоммерческая"),
                 ("дом", "Для дома"), ("физ", "Для дома"), ("частн", "Для дома"), ("коммер", "Коммерческая"),
                 ("юр", "Коммерческая"), ("бизнес", "Коммерческая")),
    "user_type": (("физ", "Физлицо"), ("частн", "Физлицо"), ("дом", "Физлицо"), ("юр", "Юрлицо"),
                  ("орг", "Юрлицо"), ("компан", "Юрлицо")),
    "license_type": (("нов", "Полная версия"), ("первичн", "Полная версия"), ("полн", "Полная версия"),
                     ("продл", "Продление"), ("подпис", "Подписка"), ("обновл", "Обновление"),
                     ("поддержк", "Техподдержка"), ("миграц", "Миграция")),
}
_WORD_FILTERS = ("org_type", "user_type")  # значения-перечисления: совпадение с начала слова
_EMPTY_PASSES = ("org_type", "user_type", "platform")  # поле не заполнено: ограничения нет, вариант подходит


def _catalog_value(key: str, value) -> str:
    """Значение фильтра словами каталога: "образовательная" -> "Академическая"."""
    text = norm(str(value))
    for stem, canonical in _VALUE_SYNONYMS.get(key, ()):
        if text.startswith(stem) or f" {stem}" in text:
            return canonical
    return str(value)


def _substring_check(columns: tuple[str, ...], value, empty_passes: bool = False, word_start: bool = False):
    """word_start: значение должно начинать слово, иначе "коммерческая" находится внутри "некоммерческая"."""
    needle = norm(str(value))
    found = re.compile(r"(?<![0-9a-zа-я])" + re.escape(needle)).search if word_start else (lambda text: needle in text)

    def check(row) -> bool:
        if empty_passes and all(not row[column] for column in columns):
            return True
        return any(found(norm(row[column])) for column in columns)
    return check


def _value_check(key: str, value):
    value = _catalog_value(key, value)
    if key == "version":  # "1.8" у Astra Linux записано в названии варианта, а не в поле версии
        return _substring_check(("version", "name"), value)
    return _substring_check((key,), value, empty_passes=key in _EMPTY_PASSES, word_start=key in _WORD_FILTERS)


def _apply_value_filters(rows: list, filters: dict) -> list:
    """Фильтры по сроку, подстрокам параметров, тексту имени или артикула и доступности."""
    checks = [_value_check(key, filters[key]) for key in _SUBSTRING_FILTERS if key in filters]
    term = _as_int(filters.get("term_months"))
    if term is not None:
        checks.append(lambda row: row["term_months"] == term)
    if "text" in filters:
        checks.append(_substring_check(("name", "vendor_code"), filters["text"]))
    if filters.get("only_available"):
        checks.append(lambda row: bool(row["available"]))
    return [row for row in rows if all(check(row) for check in checks)]


def _asked_dimensions(filters: dict) -> set[str]:
    """Измерения, про которые клиент уже ответил: их в clarify не предлагаем."""
    asked = {key for key in _SUBSTRING_FILTERS if key in filters}
    if "term_months" in filters:
        asked.add("term_months")
    if any(key in filters for key in _QUANTITY_FILTERS):
        asked.add("qty")  # количество названо: ступень цены уже выбрана
    if "users" in filters:
        asked.add("users_max")
    if "nodes" in filters:
        asked.add("nodes")
    return asked


def _clarify_order(rows: list[sqlite3.Row], facets: dict, asked: set[str]) -> list[str]:
    """Измерения, которые стоит уточнить, от сильнее всего дробящих варианты.

    Мера: какая доля вариантов в среднем останется после ответа клиента.
    Варианты без значения по измерению остаются при любом ответе.
    """
    n = len(rows)
    ranked = []
    for priority, dim in enumerate(DIMENSIONS):
        if dim not in facets or dim in asked:
            continue
        counts = [f["count"] for f in facets[dim]]
        with_value = sum(1 for r in rows if r[dim] is not None and r[dim] != "")
        missing = n - with_value
        total = sum(counts)
        remain = sum(c / total * (c + missing) for c in counts) / n
        ranked.append((round(remain, 2), priority, dim))
    return [dim for _, _, dim in sorted(ranked)]


def _facets_for_output(full: dict) -> tuple[dict, dict]:
    """Обрезает длинные списки значений; про обрезанное говорит facets_more."""
    facets, more = {}, {}
    for dim, values in full.items():
        facets[dim] = [{"value": _short(v["value"]), "count": v["count"]}
                       for v in values[:_FACET_LIMIT]]
        if len(values) > _FACET_LIMIT:
            more[dim] = {"distinct": len(values)}
            if dim in NUMERIC_DIMS:
                more[dim].update(min=values[0]["value"], max=values[-1]["value"])
    return facets, more


def get_product(product_id: str | int, filters: dict | None = None, limit: int = 10) -> dict:
    """Продукт, его варианты под фильтры, счетчики значений и что уточнить у клиента.

    filters:
      users, nodes  нужное количество. Подходят ярусы не меньше него ("до 25
                    пользователей" при users=20) и ступени от-до, в которые оно попадает;
                    ближайший ярус первым, варианты без ярусов остаются. Ближайший ярус
                    ищется среди вариантов, уже прошедших остальные фильтры.
      qty           количество лицензий для ступени цены (qty_min..qty_max).
      term_months   срок в месяцах, 0 = бессрочная.
      license_type, org_type, user_type, version, platform  подстрока без регистра.
      text          подстрока в имени или артикуле варианта.
      only_available  только доступные к заказу.

    В offers: price None значит "цена по запросу"; qty_pricing говорит, цена ступени
    за единицу ("per_unit") или за всю ступень ("per_tier"); same_code_prices это все
    цены артикула, если их несколько; price_ambiguous значит, что различие между
    ценами артикула из фида не извлекается и одну цену называть нельзя.
    """
    conn = _conn()
    filters = {k: v for k, v in (filters or {}).items() if v is not None and v != "" and v is not False}
    product = conn.execute(
        f"SELECT {_PRODUCT_COLS} FROM products p WHERE p.product_id = ?",
        (str(product_id),)).fetchone()
    if product is None:
        return {"error": "product_not_found", "product_id": str(product_id)}
    all_rows = conn.execute(_OFFER_SQL, (str(product_id),)).fetchall()
    # обязательные условия клиента (платформа, срок, тип, доступность) раньше выбора ближайшего пакета:
    # иначе пакет на 25 под Windows вытесняет подходящий пакет на 50 под Linux и вариантов не остается
    rows, gaps, notes = _apply_quantity_filters(_apply_value_filters(all_rows, filters), filters)
    rows.sort(key=_offer_order(gaps))
    full_facets = facet_counts(rows)
    facets, facets_more = _facets_for_output(full_facets)
    known = {"term_months", "text", "only_available", *_QUANTITY_FILTERS, *_SUBSTRING_FILTERS}
    result = {
        "product": _product_dict(product),
        "total_offers": len(all_rows),
        "matching": len(rows),
        "facets": facets,
        "clarify": _clarify_order(rows, full_facets, _asked_dimensions(filters)),
        "offers": [_offer_dict(r) for r in rows[:max(0, min(int(limit), 100))]],
    }
    optional = {
        "facets_more": facets_more,
        "tier_notes": notes,
        "price_ambiguous_offers": sum(1 for r in rows if r["price_ambiguous"]),
        "ignored_filters": sorted(set(filters) - known),
    }
    result.update({key: value for key, value in optional.items() if value})
    if len(rows) > len(result["offers"]):
        result["shown"] = (f"показаны {len(result['offers'])} из {len(rows)}, от дешевых к дорогим; "
                           "остальные откроются, если сузить фильтром из clarify или словом в text. "
                           "Вилку цен называйте по all_matching, а не по показанным")
        priced = [r["price"] for r in rows if r["price"] is not None]
        result["all_matching"] = {"price_min": _money(min(priced)) if priced else None,
                                  "price_max": _money(max(priced)) if priced else None,
                                  "without_price": len(rows) - len(priced)}
    if not rows and all_rows:
        result.update(_relaxed(all_rows, filters, limit))
    return result


def _offer_order(gaps: dict):
    """Порядок вариантов: сначала доступные с ценой, затем ближайший ярус количества, затем дешевле."""
    def order(r: sqlite3.Row) -> tuple:
        buyable = 0 if r["available"] and r["price"] is not None else 1
        price = r["price"] if r["price"] is not None else _INF
        return (buyable, gaps.get(r["id"], 0), price, r["id"])
    return order


def _relaxed(all_rows: list, filters: dict, limit: int) -> dict:
    """Фильтры дали ноль: показываем, что есть, если снять один из них (тот, без которого вариантов больше)."""
    best_key, best_rows, best_gaps, best_notes = None, [], {}, []
    for key in filters:
        rest = {k: v for k, v in filters.items() if k != key}
        rows, gaps, notes = _apply_quantity_filters(_apply_value_filters(all_rows, rest), rest)
        if len(rows) > len(best_rows):
            best_key, best_rows, best_gaps, best_notes = key, rows, gaps, notes
    if not best_rows:
        return {"note": "С такими фильтрами вариантов нет. В facets значения, которые есть у продукта без фильтров.",
                "facets": _facets_for_output(facet_counts(all_rows))[0]}
    best_rows.sort(key=_offer_order(best_gaps))
    return {"note": f"Вариантов с {best_key}={filters[best_key]} у продукта нет. Ниже варианты без этого условия: "
                    "скажите клиенту, что именно такого в каталоге нет, и предложите ближайшее.",
            "dropped_filter": best_key,
            **({"tier_notes": best_notes} if best_notes else {}),
            "facets": _facets_for_output(facet_counts(best_rows))[0],
            "offers": [_offer_dict(r) for r in best_rows[:max(0, min(int(limit), 100))]]}


def get_offer(offer_id: int | str) -> dict:
    """Один вариант целиком: все поля, параметры из фида и название продукта."""
    conn = _conn()
    offer_id = _as_int(offer_id)
    row = conn.execute(
        "SELECT o.*, p.title AS product_title, p.category_path FROM offers o"
        " LEFT JOIN products p ON p.product_id = o.product_id WHERE o.id = ?",
        (offer_id,)).fetchone()
    if row is None:
        return {"error": "offer_not_found", "offer_id": offer_id}
    item = {key: row[key] for key in row.keys() if key not in ("params_json", "id")}
    item = {"offer_id": row["id"], **item}
    item["price"], item["oldprice"] = _money(row["price"]), _money(row["oldprice"])
    item["available"] = bool(row["available"])
    item["russian_vendor"] = bool(row["russian_vendor"])
    item["price_ambiguous"] = bool(row["price_ambiguous"])
    item["same_code_prices"] = json.loads(row["same_code_prices"] or "null")
    item["params"] = json.loads(row["params_json"] or "{}")
    item["price_for"] = _price_for(row)
    return item


# --- категории ----------------------------------------------------------------

def _category_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    has_children = conn.execute(
        "SELECT 1 FROM categories WHERE parent_id = ? AND n_products > 0 LIMIT 1",
        (row["id"],)).fetchone() is not None
    return {"id": row["id"], "name": row["name"], "path": row["path"],
            "n_products": row["n_products"], "n_available": row["n_available"],
            "has_children": has_children}


def _category_children(conn: sqlite3.Connection, parent_id: int | None, limit: int) -> dict:
    """Непустые дочерние категории parent_id (или корневые)."""
    condition = "parent_id IS NULL" if parent_id is None else "parent_id = ?"
    args = () if parent_id is None else (int(parent_id),)
    rows = conn.execute(
        f"SELECT * FROM categories WHERE {condition} AND n_products > 0"
        " ORDER BY n_available DESC, n_products DESC, name LIMIT ?", (*args, limit)).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM categories WHERE {condition} AND n_products > 0",
        args).fetchone()[0]
    return {"total": total, "categories": [_category_dict(conn, r) for r in rows]}


def browse_categories(query: str | None = None, parent_id: int | None = None,
                      limit: int = 20) -> dict:
    """Категории: по запросу поиск в названиях и путях, без запроса дети parent_id или корень.

    Пустые категории (без товаров) не показываются. Сортировка по числу доступных
    вариантов, затем по числу продуктов; при поиске совпадение в самом названии
    категории идет раньше совпадения только в пути.
    """
    conn = _conn()
    limit = max(1, min(int(limit), 100))
    q = Query(query or "")
    if not q.groups:
        return _category_children(conn, parent_id, limit)

    scope = ""
    if parent_id is not None:
        ids = _subtree_ids(conn, parent_id)
        scope = f" AND c.id IN ({','.join(map(str, ids)) or 'NULL'})"
    sql = ("SELECT c.* FROM categories_fts f JOIN categories c ON c.id = f.rowid"
           " WHERE categories_fts MATCH ? AND c.n_products > 0" + scope)
    rows = conn.execute(sql, (q.fts("AND"),)).fetchall()
    if not rows and len(q.groups) > 1:
        rows = conn.execute(sql, (q.fts("OR"),)).fetchall()

    def hits(row: sqlite3.Row, field: str) -> int:
        return sum(1 for hit in q.coverage(_doc_tokens(row[field])) if hit)

    best = max((hits(r, "path") for r in rows), default=0)
    rows = [r for r in rows if hits(r, "path") == best]  # слабые совпадения по одному слову из многих отсекаются

    def order(row: sqlite3.Row) -> tuple:
        return (-hits(row, "name"), -row["n_available"], -row["n_products"], row["name"])

    rows.sort(key=order)
    return {"total": len(rows), "categories": [_category_dict(conn, r) for r in rows[:limit]]}


# --- аналоги ------------------------------------------------------------------

def _analog_levels(conn: sqlite3.Connection, category_id: int | None) -> list[sqlite3.Row]:
    """Категории, в которых искать аналоги: от ближайшей к корню.

    В выгрузке-файле лист дерева это сама линейка продукта, поэтому поиск начинается с ее родителя.
    В живом фиде линейки в дерево не входят: продукты лежат прямо в категории, и она идет в поиск первой.
    """
    chain = []
    while category_id is not None and len(chain) < 10:
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            break
        chain.append(row)
        category_id = row["parent_id"]
    def is_line(c) -> bool:  # категория без подкатегорий, в которой лежит один продукт: это сама его линейка
        return (conn.execute("SELECT 1 FROM categories WHERE parent_id = ? LIMIT 1", (c["id"],)).fetchone() is None
                and conn.execute("SELECT COUNT(*) FROM products WHERE category_id = ?", (c["id"],)).fetchone()[0] <= 1)

    return [c for c in chain if not is_line(c)] or chain


def _import_substitution(conn: sqlite3.Connection, category_id: int | None) -> list[sqlite3.Row]:
    """Ветки "(Импортозамещение)" в том же разделе каталога, что и продукт.

    Это список российских замен из самого фида ("Российские операционные системы
    (Импортозамещение)", "Российская коммуникационная платформа (Импортозамещение)").
    """
    root = None
    while category_id is not None:
        row = conn.execute("SELECT id, parent_id FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            break
        root, category_id = row["id"], row["parent_id"]
    if root is None:
        return []
    return conn.execute(
        "SELECT * FROM categories WHERE parent_id = ? AND name LIKE '%мпортозамещение)%'"
        " AND n_products > 0 ORDER BY n_available DESC", (root,)).fetchall()


def find_analogs(product_id: str | int, russian_only: bool = True, limit: int = 8,
                 same_vendor: bool = False) -> dict:
    """Продукты других вендоров из той же категории, что и продукт.

    Сначала соседние линейки (общая родительская категория); если нашлось меньше
    трех, вся категория верхнего уровня. russian_only оставляет продукты с номером
    в реестре отечественного ПО (in_ru_registry) или вендора из справочника
    data/ru_vendors.txt (russian_vendor).
    """
    conn = _conn()
    limit = max(1, min(int(limit), 50))
    product = conn.execute(
        f"SELECT {_PRODUCT_COLS} FROM products p WHERE p.product_id = ?",
        (str(product_id),)).fetchone()
    if product is None:
        return {"error": "product_not_found", "product_id": str(product_id)}
    levels = _analog_levels(conn, product["category_id"])

    where = " AND p.product_id != ?"
    params: list = [product["product_id"]]
    if russian_only:
        where += " AND p.is_russian = 1"
    if not same_vendor:
        where += " AND p.vendor != ?"
        params.append(product["vendor"])
    def products_in(categories: list) -> list:
        ids = [i for c in categories for i in _subtree_ids(conn, c["id"])]
        return conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products p"
            f" WHERE p.category_id IN ({','.join(map(str, ids))}){where}"
            " ORDER BY p.n_available DESC, p.n_offers DESC LIMIT ?",
            (*params, limit)).fetchall() if ids else []

    rows, basis, source = [], None, None
    # ближайшие категории; раздел верхнего уровня слишком широк ("Сеть и интернет"), его не берем
    for category in [c for c in levels if c["parent_id"] is not None]:
        rows, basis, source = products_in([category]), category, "та же категория каталога"
        if len(rows) >= min(3, limit):
            break
    if russian_only and len(rows) < min(3, limit):
        substitution = _import_substitution(conn, product["category_id"])
        extra = [r for r in products_in(substitution)
                 if r["product_id"] not in {x["product_id"] for x in rows}]
        if extra:
            if not rows:
                basis, source = substitution[0], "ветка импортозамещения того же раздела каталога"
            rows = (rows + extra)[:limit]
    result = {
        "product": {"product_id": product["product_id"], "title": product["title"],
                    "vendor": product["vendor"],
                    "in_ru_registry": product["registry_no"] is not None,
                    "russian_vendor": bool(product["russian_vendor"])},
        "basis": {"category_id": basis["id"], "path": basis["path"], "source": source} if basis else None,
        "russian_only": bool(russian_only),
        "products": [_product_dict(r) for r in rows],
    }
    if not rows:
        result["note"] = ("В этой категории каталога аналогов не нашлось. Попробуйте search_products "
                          "по описанию задачи" + (" с russian_only=true." if russian_only else "."))
    return result


_STAT_KEYS = ("offers", "products", "categories", "available", "with_price", "in_ru_registry",
              "russian_vendor_products")


def stats() -> dict:
    """Счетчики базы (посчитаны при сборке) и дата фида."""
    meta = dict(_conn().execute("SELECT key, value FROM meta").fetchall())
    result = {key: int(meta[key]) for key in _STAT_KEYS}
    result["feed_date"] = meta.get("feed_date")
    return result


# --- командная строка ---------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Поиск по каталогу")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="поиск продуктов")
    p.add_argument("query")
    p.add_argument("--vendor")
    p.add_argument("--category", type=int)
    p.add_argument("--available", action="store_true")
    p.add_argument("--russian", action="store_true")
    p.add_argument("--type", dest="product_type")
    p.add_argument("--limit", type=int, default=8)

    p = sub.add_parser("product", help="продукт и его варианты")
    p.add_argument("product_id")
    p.add_argument("--users", type=int)
    p.add_argument("--nodes", type=int)
    p.add_argument("--qty", type=int)
    p.add_argument("--term", type=int, dest="term_months")
    p.add_argument("--license", dest="license_type")
    p.add_argument("--org", dest="org_type")
    p.add_argument("--user-type", dest="user_type")
    p.add_argument("--version")
    p.add_argument("--platform")
    p.add_argument("--text")
    p.add_argument("--available", action="store_true", dest="only_available")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("offer", help="один вариант целиком")
    p.add_argument("offer_id", type=int)

    p = sub.add_parser("categories", help="категории")
    p.add_argument("query", nargs="?")
    p.add_argument("--parent", type=int)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("analogs", help="аналоги продукта")
    p.add_argument("product_id")
    p.add_argument("--all", action="store_true", help="не только отечественные")
    p.add_argument("--limit", type=int, default=8)

    sub.add_parser("stats", help="счетчики базы")

    args = parser.parse_args(argv)
    if args.command == "search":
        out = search_products(args.query, vendor=args.vendor, category_id=args.category,
                              only_available=args.available, russian_only=args.russian,
                              product_type=args.product_type, limit=args.limit)
    elif args.command == "product":
        keys = ("users", "nodes", "qty", "term_months", "license_type", "org_type", "user_type",
                "version", "platform", "text", "only_available")
        out = get_product(args.product_id, {k: getattr(args, k) for k in keys}, limit=args.limit)
    elif args.command == "offer":
        out = get_offer(args.offer_id)
    elif args.command == "categories":
        out = browse_categories(args.query, parent_id=args.parent, limit=args.limit)
    elif args.command == "analogs":
        out = find_analogs(args.product_id, russian_only=not args.all, limit=args.limit)
    else:
        out = stats()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
