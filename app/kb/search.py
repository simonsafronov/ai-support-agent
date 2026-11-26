"""Поиск по базе знаний (SQLite FTS5, bm25).

    python -m app.kb.search "как получить лицензию после оплаты"
    python -m app.kb.search "способы оплаты" --source store --limit 3
    python -m app.kb.search --stats

База собирается командами `python -m app.kb.crawl fetch` и `python -m app.kb.crawl build`.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

import snowballstemmer

from app.config import KB_DB

DB_PATH = KB_DB  # data/db/kb.sqlite, переопределяется переменной окружения KB_DB
SOURCES = ("docs", "store")
# Разделы docs.softline.com о покупках в самом магазине: личный кабинет lk.softline.ru (заказы,
# счета, документы, ключи) и учетная запись Softline ID. Остальные разделы портала описывают
# другие сервисы Softline: подписки, прием платежей, кабинеты продавцов и вендоров; сюда же
# относится справка по API для разработчиков. Их куски остаются в выдаче, но с понижением:
# иначе на вопрос покупателя «возврат» или «способы оплаты» наверх выходит справка для
# продавцов. OTHER_SERVICE_FACTOR = 1.0 дает чистый bm25.
SHOP_DOCS_SECTIONS = ("lk", "id")
API_HEADING_MARK = "> API "  # подраздел «API Softline ID (oAuth2)» в цепочке заголовков
OTHER_SERVICE_FACTOR = 0.5
DOCS_BASE = "https://docs.softline.com/ru-ru"
SNIPPET_MAX = 700
HEADING_WEIGHT = 4.0  # совпадение в цепочке заголовков весит больше, чем в тексте
PREFIX_MIN_STEM = 5  # стем короче ищется точно, иначе «лиц*» ловит «лицензия»

_STEMMER = snowballstemmer.stemmer("russian")
_TOKEN_RE = re.compile(r"[a-zа-я0-9]+")
_CYRILLIC_RE = re.compile(r"[а-я]")
_SENTENCE_RE = re.compile(r"(?<=[.!?…;])\s+")
_STOPWORDS = frozenset(
    """и в во на по с со к ко у о об от до за из для при или а но же ли бы не ни что как это этот эта
    эти то так там тут вот я мы вы он она они оно мне нам вам меня вас его ее их мой наш ваш свой
    быть есть был была было были будет можно нужно надо где когда какой какая какие какое каким
    чем кто чтобы если да нет также тоже еще уже только очень после перед через между под над без
    про такой такая такое такие который которая которое которые весь все могу может можем можете
    хочу хотим подскажите скажите расскажите пожалуйста здравствуйте спасибо
    the a an of to in on for and or is are what how""".split()
)
# Стемы, которые сводятся к одному слову и в индексе, и в запросе: формы, которые стеммер
# разводит (оплата и оплаченный), и слова, которыми об одном говорят покупатель и сайт.
_CANON = {
    "достав": "доставк", "доставля": "доставк", "поставк": "доставк",  # сроки поставки = доставки
    "оплач": "оплат", "оплачен": "оплат", "оплачива": "оплат",
    "вариант": "способ",  # на сайте «варианты оплаты»
    "юрлиц": "юридическ", "юр": "юридическ",  # на сайте «для юр. лиц»
    "физлиц": "физическ", "физ": "физическ",
    "бухгалтерск": "закрыва",  # закрывающие = бухгалтерские документы
    "возвраща": "возврат", "вернут": "возврат",
    "денег": "деньг", "денежн": "деньг",
    "госзакупк": "тендер", "закупк": "тендер",
    "отм": "отмен",  # отмена, отменить
    "реж": "график",  # режим работы = график работы
    "покупк": "куп", "покупа": "куп", "приобрет": "куп", "приобрест": "куп", "приобрета": "куп",
    "приобретен": "куп",
}


def fold(text: str) -> str:
    """Нижний регистр и ё -> е."""
    return text.lower().replace("ё", "е")


@lru_cache(maxsize=None)
def stem(token: str) -> str:
    """Нормальная форма слова для индекса и запроса: стем, сведенный по _CANON.

    Латиница и числа не меняются; сокращения до трех букв (ндс, эдо, инн) не стемятся.
    """
    if not _CYRILLIC_RE.search(token):
        return token
    word = token if len(token) <= 3 else _STEMMER.stemWord(token)
    return _CANON.get(word, word)


def index_text(text: str) -> str:
    """Текст для индекса FTS: нормальные формы слов через пробел (исходный текст лежит в chunks)."""
    return " ".join(stem(token) for token in _TOKEN_RE.findall(fold(text)))


_STOP_STEMS = frozenset(stem(word) for word in _STOPWORDS)  # «таких», «каком» тоже служебные


def query_terms(query: str) -> list[tuple[str, bool]]:
    """Запрос -> [(стем, искать_по_префиксу)] без стоп-слов и повторов."""
    tokens = _TOKEN_RE.findall(fold(query))
    meaningful = [t for t in tokens if t not in _STOPWORDS and stem(t) not in _STOP_STEMS] or tokens
    terms = [(s, len(s) >= PREFIX_MIN_STEM and s.isalpha()) for s in dict.fromkeys(map(stem, meaningful))]
    # «лиценз*» уже покрывает «лицензион*»: одно совпадение дважды не считаем
    prefixes = [s for s, by_prefix in terms if by_prefix]
    return [t for t in terms if not any(t[0] != p and t[0].startswith(p) for p in prefixes)]


def fts_query(terms: list[tuple[str, bool]]) -> str:
    """OR по стемам запроса; стемы от PREFIX_MIN_STEM знаков ищутся как префиксы."""
    return " OR ".join(f'"{s}"*' if by_prefix else f'"{s}"' for s, by_prefix in terms)


def _hits(text: str, terms: list[tuple[str, bool]]) -> list[str]:
    """Стемы запроса, встреченные в тексте (с повторами)."""
    exact = {s for s, by_prefix in terms if not by_prefix}
    prefixes = [s for s, by_prefix in terms if by_prefix]
    found = []
    for token in _TOKEN_RE.findall(fold(text)):
        word = stem(token)
        if word in exact:
            found.append(word)
        else:
            found.extend(p for p in prefixes if word.startswith(p))
    return found


def best_snippet(text: str, terms: list[tuple[str, bool]], max_len: int = SNIPPET_MAX) -> str:
    """Самый релевантный фрагмент куска: окно из соседних строк и предложений до max_len знаков."""
    if len(text) <= max_len:
        return text
    budget = max_len - 2  # место под многоточия
    units: list[tuple[str, str]] = []  # (разделитель перед единицей, текст)
    for line in text.split("\n"):
        separator = "\n"
        for sentence in _SENTENCE_RE.split(line) if len(line) > budget // 2 else [line]:
            while len(sentence) > budget:
                cut = sentence.rfind(" ", 0, budget)
                cut = cut if cut > budget // 2 else budget
                units.append((separator, sentence[:cut].strip()))
                sentence, separator = sentence[cut:].strip(), " "
            if sentence:
                units.append((separator, sentence))
                separator = " "
    hits = [_hits(unit, terms) for _, unit in units]
    best, best_score = (0, 1), (-1, -1)
    for start in range(len(units)):
        length, end = 0, start
        while end < len(units) and length + len(units[end][1]) + (end > start) <= budget:
            length += len(units[end][1]) + (end > start)
            end += 1
        found = [h for unit_hits in hits[start:end] for h in unit_hits]
        score = (len(set(found)), len(found))
        if score > best_score:
            best, best_score = (start, end), score
    start, end = best
    body = units[start][1] + "".join(sep + unit for sep, unit in units[start + 1 : end])
    return ("…" if start > 0 else "") + body + ("…" if end < len(units) else "")


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"База знаний не собрана ({path}): python -m app.kb.crawl fetch && python -m app.kb.crawl build"
        )
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _shop_condition() -> str:
    """SQL-условие «кусок о покупках в магазине»: сам магазин и разделы SHOP_DOCS_SECTIONS без API."""
    parts = ["d.source = 'store'"]
    for section in SHOP_DOCS_SECTIONS:
        parts += [f"d.url = '{DOCS_BASE}/{section}'", f"d.url LIKE '{DOCS_BASE}/{section}/%'"]
    return f"({' OR '.join(parts)}) AND c.heading NOT LIKE '%{API_HEADING_MARK}%'"


def kb_search(query: str, limit: int = 5, source: str | None = None, db_path: Path | None = None) -> dict:
    """Поиск кусков базы знаний по запросу на естественном языке.

    Возвращает {"query", "results": [{"title", "url", "heading", "snippet", "source", "score"}]},
    results отсортированы по убыванию score. score — bm25 (совпадение в заголовках весит больше),
    для разделов docs о других сервисах Softline умноженный на OTHER_SERVICE_FACTOR.
    source: None, "docs" или "store" — искать только на одном сайте.
    """
    if source is not None and source not in SOURCES:
        raise ValueError(f"source: ожидается одно из {SOURCES} или None, получено {source!r}")
    terms = query_terms(query or "")
    if not terms:
        return {"query": query, "results": []}
    limit = max(1, min(int(limit), 50))
    sql = (
        "SELECT d.title, d.url, c.heading, c.text, d.source, "
        f"bm25(chunks_fts, {HEADING_WEIGHT}, 1.0) * "
        f"CASE WHEN {_shop_condition()} THEN 1.0 ELSE {OTHER_SERVICE_FACTOR} END AS rank "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN docs d ON d.id = c.doc_id "
        "WHERE chunks_fts MATCH ?"
    )
    params: list = [fts_query(terms)]
    if source:
        sql += " AND d.source = ?"
        params.append(source)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    db = _connect(db_path)
    try:
        rows = db.execute(sql, params).fetchall()
    finally:
        db.close()
    results = [
        {
            "title": title,
            "url": url,
            "heading": heading,
            "snippet": best_snippet(text, terms),
            "source": src,
            "score": round(-rank, 4),
        }
        for title, url, heading, text, src, rank in rows
    ]
    return {"query": query, "results": results}


def stats(db_path: Path | None = None) -> dict:
    """Счетчики базы: {"docs", "chunks", "by_source": {"docs": n, "store": m}}."""
    db = _connect(db_path)
    try:
        docs = db.execute("SELECT count(*) FROM docs").fetchone()[0]
        chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        by_source = dict(db.execute("SELECT source, count(*) FROM docs GROUP BY source ORDER BY source"))
    finally:
        db.close()
    return {"docs": docs, "chunks": chunks, "by_source": by_source}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.kb.search", description="Поиск по базе знаний")
    parser.add_argument("query", nargs="*", help="запрос на естественном языке")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--source", choices=SOURCES)
    parser.add_argument("--stats", action="store_true", help="показать счетчики базы")
    args = parser.parse_args(argv)
    if not args.stats and not args.query:
        parser.error("нужен запрос или --stats")
    try:
        result = stats() if args.stats else kb_search(" ".join(args.query), args.limit, args.source)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
