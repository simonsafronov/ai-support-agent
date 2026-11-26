"""Тесты базы знаний: нормализация запроса, разбор HTML, поиск по реальной базе.

Тесты поиска идут на собранной data/db/kb.sqlite (python -m app.kb.crawl fetch && build);
если базы нет, они пропускаются. Ожидаемые слова взяты из фактического текста страниц.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.kb import crawl
from app.kb.search import (
    DB_PATH,
    DOCS_BASE,
    SHOP_DOCS_SECTIONS,
    SNIPPET_MAX,
    best_snippet,
    fold,
    fts_query,
    kb_search,
    query_terms,
    stats,
)

needs_db = pytest.mark.skipif(not DB_PATH.exists(), reason=f"нет базы знаний {DB_PATH}")


# --------------------------------------------------------------------------- #
# Без базы: нормализация запроса, выбор фрагмента, разбор и нарезка HTML
# --------------------------------------------------------------------------- #


def test_query_terms_normalize_case_yo_and_morphology():
    assert query_terms("СЧЁТ") == query_terms("счета") == [("счет", False)]
    assert query_terms("Лицензии") == query_terms("лицензия") == [("лиценз", True)]


def test_query_terms_drop_stopwords_and_keep_abbreviations():
    stems = [stem for stem, _ in query_terms("как получить закрывающие документы по НДС и ЭДО")]
    assert "как" not in stems and "по" not in stems and "и" not in stems
    assert "ндс" in stems and "эдо" in stems  # сокращения не стемятся
    assert "документ" in stems
    # служебные слова отбрасываются и в косвенных формах («таких», «каком»)
    assert query_terms("что такое Softline Checkout") == [("softline", True), ("checkout", True)]
    assert query_terms("в каких случаях возможен возврат") == query_terms("случаи возможен возврат")


def test_query_terms_merge_word_forms_and_shop_synonyms():
    assert dict(query_terms("сроки поставки")) == {"срок": False, "доставк": True}  # поставка = доставка
    assert query_terms("доставить") == query_terms("доставка") == [("доставк", True)]
    assert query_terms("оплаченный") == query_terms("оплата") == [("оплат", True)]
    assert query_terms("юрлицо") == query_terms("юр") == query_terms("юридический") == [("юридическ", True)]
    # короткий стем ищется точно, длинный по префиксу; «лиценз*» покрывает «лицензион*»
    assert fts_query(query_terms("срок лицензии и лицензионный ключ")) == '"срок" OR "лиценз"* OR "ключ"'


def test_query_of_stopwords_only_is_not_empty():
    assert query_terms("как и что") != []
    assert query_terms("?!") == []


def test_best_snippet_picks_relevant_window_within_limit():
    filler = "Общие сведения о магазине и порядке работы с каталогом. " * 30
    target = "Возврат товара надлежащего качества возможен в течение 7 дней."
    text = filler.strip() + "\n" + target + "\n" + filler.strip()
    snippet = best_snippet(text, query_terms("возврат товара"))
    assert len(snippet) <= SNIPPET_MAX
    assert target in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    short = "Короткий кусок возвращается целиком."
    assert best_snippet(short, query_terms("кусок")) == short


STORE_HTML = """
<html><head><title>Оплата и доставка</title></head><body>
<header><a href="/catalog/">Каталог</a></header>
<main class="b-page__main">
  <div class="b-pure-content-navigation"><ul><li><a href="#q1">Как оплатить заказ?</a></li></ul></div>
  <h1>Информация об оплате</h1>
  <ul><li><a href="#q1">Как оплатить заказ?</a></li></ul>
  <h2><a id="q1"></a>Как оплатить заказ?</h2>
  <p>Доступны следующие&nbsp;варианты оплаты:</p>
  <ol><li>Банковский платёж;</li><li>Банковская карта.</li></ol>
  <table><tr><th>Зона</th><th>Стоимость</th></tr><tr><td>В пределах МКАД</td><td>590</td></tr></table>
  <button class="spoiler__trigger"><p>Можно ли оформить возврат?</p></button>
  <div class="spoiler__content"><p>Да, но есть исключения.</p></div>
  <form><p>Оставьте заявку</p></form>
  <script>var hidden = "скрипт";</script>
</main>
<footer><p>Подвал</p></footer>
</body></html>
"""


def test_extract_keeps_main_content_and_headings_only():
    content = crawl.extract(STORE_HTML, "store")
    assert content.title == "Информация об оплате"
    kinds = [(kind, level, text) for kind, level, text in content.blocks]
    assert ("h", 1, "Информация об оплате") in kinds
    assert ("h", 2, "Как оплатить заказ?") in kinds
    assert ("p", 0, "Доступны следующие варианты оплаты:") in kinds
    assert ("p", 0, "- Банковский платёж;") in kinds
    assert ("t", 0, "Зона | Стоимость\nВ пределах МКАД | 590") in kinds
    assert ("h", crawl.SPOILER_LEVEL, "Можно ли оформить возврат?") in kinds  # вопрос FAQ
    text = "\n".join(text for _, _, text in content.blocks)
    for noise in ("Каталог", "Подвал", "Оставьте заявку", "скрипт"):
        assert noise not in text
    # оглавление из ссылок-якорей в контент не попадает: заголовок вопроса встречается один раз
    assert text.count("Как оплатить заказ?") == 1


def test_chunks_carry_heading_chain_and_respect_size():
    content = crawl.extract(STORE_HTML, "store")
    chunks = crawl.chunk_content(content)
    headings = [heading for heading, _ in chunks]
    assert "Информация об оплате > Как оплатить заказ?" in headings
    assert "Информация об оплате > Как оплатить заказ? > Можно ли оформить возврат?" in headings

    long_page = crawl.Content(
        title="Правила",
        blocks=[("h", 1, "Правила")] + [("p", 0, f"Пункт {i}. " + "Условие продажи продукции. " * 12) for i in range(12)],
    )
    sizes = [len(text) for _, text in crawl.chunk_content(long_page)]
    assert len(sizes) > 1
    assert max(sizes) <= crawl.CHUNK_MAX
    assert min(sizes) >= crawl.CHUNK_MIN


def test_docs_soft_404_is_detected():
    not_found = '<html><head><title>undefined — База Знаний Softline</title></head><body><main class="container"><!----></main></body></html>'
    article = '<html><head><title>Счета — База Знаний Softline</title></head><body><main class="container"><article class="article"><h1>Счета</h1><p>Текст статьи о счетах.</p></article></main></body></html>'
    assert crawl.is_docs_soft_404(not_found)
    assert not crawl.is_docs_soft_404(article)
    assert crawl.extract(article, "docs").title == "Счета"


# --------------------------------------------------------------------------- #
# На реальной базе
# --------------------------------------------------------------------------- #


@needs_db
def test_stats_and_schema():
    result = stats()
    assert set(result) == {"docs", "chunks", "by_source"}
    assert result["by_source"]["store"] >= 10
    assert result["by_source"]["docs"] >= 30
    assert sum(result["by_source"].values()) == result["docs"]
    assert result["chunks"] > result["docs"]

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        assert [row[1] for row in db.execute("PRAGMA table_info(docs)")] == [
            "id", "url", "title", "source", "fetched_at", "text",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_info(chunks)")] == ["id", "doc_id", "heading", "text"]
        fts_rows = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        orphans = db.execute(
            "SELECT count(*) FROM chunks WHERE id NOT IN (SELECT rowid FROM chunks_fts)"
        ).fetchone()[0]
        longest = db.execute("SELECT max(length(text)) FROM chunks").fetchone()[0]
    finally:
        db.close()
    assert fts_rows == result["chunks"] and orphans == 0
    assert longest <= crawl.CHUNK_MAX


# (запрос, факты со страниц: хотя бы один должен быть в заголовке или фрагменте куска из топ-3)
CASES = [
    ("доставка электронной лицензии", ["на электронную почту заказчика", "от 5 мин до 30 рабочих дней"]),
    ("способы оплаты", ["банковский платеж", "банковская карта"]),
    (
        "оплата по счету для юридических лиц",
        ["сформировать счет онлайн через корзину", "счет на оплату без ожиданий", "банковский платеж (для юр. лиц и ип)"],
    ),
    ("возврат", ["условия возврата денежных средств"]),
    ("сроки поставки", ["от 5 мин до 30 рабочих дней", "2-3 рабочих дня по москве"]),
    ("НДС", ["может не облагаться ндс", "поставка облагается ндс"]),
    ("закрывающие документы", ["упд", "в течение 7 рабочих дней"]),
]


def _is_shop_page(url: str) -> bool:
    """Страница магазина или раздел docs о личном кабинете и учетной записи."""
    return url.startswith("https://store.softline.ru/") or any(
        url == f"{DOCS_BASE}/{section}" or url.startswith(f"{DOCS_BASE}/{section}/")
        for section in SHOP_DOCS_SECTIONS
    )


@needs_db
@pytest.mark.parametrize(("query", "expected"), CASES, ids=[f"case{i}" for i in range(1, len(CASES) + 1)])
def test_top3_contains_expected_fact(query, expected):
    results = kb_search(query, limit=3)["results"]
    assert results, f"пустая выдача по запросу «{query}»"
    top = "\n".join(fold(f"{r['heading']}\n{r['snippet']}") for r in results)
    assert any(word in top for word in expected), f"в топ-3 по «{query}» нет ни одного из {expected}:\n{top}"
    # на вопрос покупателя наверху страницы магазина и личного кабинета, а не справка для продавцов
    assert all(_is_shop_page(r["url"]) for r in results), [r["url"] for r in results]


@needs_db
def test_other_softline_services_stay_searchable():
    results = kb_search("webhook-оповещения о платеже", limit=3)["results"]
    assert results and results[0]["url"].startswith(f"{DOCS_BASE}/payment/")
    assert results[0]["source"] == "docs"


@needs_db
def test_result_contract():
    response = kb_search("как оплатить заказ", limit=5)
    assert response["query"] == "как оплатить заказ"
    results = response["results"]
    assert 1 <= len(results) <= 5
    for item in results:
        assert set(item) == {"title", "url", "heading", "snippet", "source", "score"}
        assert item["source"] in ("docs", "store")
        assert item["url"].startswith(("https://docs.softline.com/ru-ru", "https://store.softline.ru/"))
        assert item["title"] and item["heading"]
        assert 0 < len(item["snippet"]) <= SNIPPET_MAX
        assert item["score"] > 0
    scores = [item["score"] for item in results]
    assert scores == sorted(scores, reverse=True)


@needs_db
def test_limit_source_filter_and_empty_query():
    assert len(kb_search("заказ", limit=2)["results"]) == 2
    for source in ("store", "docs"):
        results = kb_search("заказ", limit=10, source=source)["results"]
        assert results and all(item["source"] == source for item in results)
    assert kb_search("", limit=5) == {"query": "", "results": []}
    assert kb_search("qwertyzzz несуществующееслово")["results"] == []
    with pytest.raises(ValueError):
        kb_search("заказ", source="blog")


@needs_db
def test_word_forms_and_yo_give_same_results():
    def urls(query):
        return [(r["url"], r["heading"]) for r in kb_search(query, limit=5)["results"]]

    assert urls("счёт на оплату") == urls("счет на оплату") == urls("СЧЕТА НА ОПЛАТУ")
    assert urls("возврат лицензии") == urls("возвраты лицензий")


@pytest.mark.needs_data
def test_unknown_product_name_marks_results_as_off_topic():
    res = kb_search("XSpider продление лицензии")
    assert "xspider" in res["note"]
    assert "note" not in kb_search("доставка электронной лицензии")
