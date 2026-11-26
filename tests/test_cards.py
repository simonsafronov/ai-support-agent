"""Описание продукта со страницы магазина: разбор страницы, хранение, поведение при отказе сайта. Сеть подменена."""
import httpx
import pytest

from app import config
from app.catalog import cards

URL = "https://store.softline.ru/mdaemon/mdaemon-messaging-server/"
PAGE = """<html><body><ul class="tabs">
<li class="tab" data-tab="licensing"><article><p>Правила лицензирования.</p></article></li>
<li class="tab is-active" data-tab="description"><div class="wrap"><article class="text">
<p>MDaemon Messaging Server &ndash; сервер электронной почты для малого и среднего бизнеса.</p>
<script>track("x")</script><style>.a{color:red}</style>
<ul><li>Поддерживает IMAP, SMTP, POP3&nbsp;и ActiveSync.</li><li>Защищает от спама и вирусов.</li></ul>
<h3>Для кого</h3><p>Компании до 500 почтовых ящиков.</p>
</article></div></li></ul></body></html>"""


@pytest.fixture(autouse=True)
def cards_on(monkeypatch):
    monkeypatch.setattr(config, "PRODUCT_CARDS", True)
    monkeypatch.setattr(cards, "DELAY", 0)
    monkeypatch.setitem(cards._state, "last", 0.0)
    monkeypatch.setitem(cards._state, "paused_until", 0.0)


def test_description_tab_becomes_plain_lines():
    text = cards.extract(PAGE)
    assert text.splitlines() == [
        "MDaemon Messaging Server – сервер электронной почты для малого и среднего бизнеса.",
        "Поддерживает IMAP, SMTP, POP3 и ActiveSync.", "Защищает от спама и вирусов.", "Для кого",
        "Компании до 500 почтовых ящиков."]
    assert "track" not in text and "color" not in text and "лицензирования" not in text  # только вкладка описания
    assert cards.extract("<html><body>страница без вкладок</body></html>") == ""


def test_page_is_requested_once_and_then_read_from_the_store(monkeypatch):
    asked = []
    monkeypatch.setattr(cards, "_fetch", lambda url: asked.append(url) or PAGE)
    first = cards.about("3101", URL)
    assert first.startswith("MDaemon Messaging Server") and cards.about("3101", URL) == first
    assert asked == [URL]
    assert cards.about("3101", URL, fetch=False) == first  # поиск берет описание только из сохраненного


def test_long_description_is_cut_at_the_end_of_a_sentence(monkeypatch):
    monkeypatch.setattr(cards, "_fetch", lambda url: PAGE)
    short = cards.about("3101", URL, limit=120)
    assert short == "MDaemon Messaging Server – сервер электронной почты для малого и среднего бизнеса."


def test_site_refusal_is_not_stored_and_agent_works_without_description(monkeypatch):
    answers = [None, PAGE]
    monkeypatch.setattr(cards, "_fetch", lambda url: answers.pop(0))
    assert cards.about("3101", URL) == ""          # сайт не ответил: описания нет, ошибки нет
    assert cards.about("3101", URL, fetch=False) == ""
    assert cards.about("3101", URL).startswith("MDaemon")  # в следующий раз страница запрошена снова


def test_refusal_pauses_all_requests(monkeypatch):
    calls = []

    def get(url, **kw):
        calls.append(url)
        return httpx.Response(429, text="Too Many Requests", request=httpx.Request("GET", url))

    monkeypatch.setattr(cards.httpx, "get", get)
    assert cards._fetch(URL) is None and cards._fetch(URL) is None
    assert calls == [URL]  # после отказа сайта второй запрос не уходит: пауза


def test_only_store_pages_are_requested_and_setting_turns_it_off(monkeypatch):
    monkeypatch.setattr(cards, "_fetch", lambda url: pytest.fail("запроса быть не должно"))
    assert cards.about("1", "https://example.com/product/") == "" and cards.about("1", None) == ""
    monkeypatch.setattr(config, "PRODUCT_CARDS", False)
    assert cards.about("3101", URL) == ""


def test_agent_gets_the_description_with_product_variants_and_no_catalog_wide_price_range(monkeypatch):
    from app.agent import tools
    from app.catalog import search as catalog

    found = catalog.search_products("MDaemon", limit=1)["products"][0]
    monkeypatch.setattr(cards, "_fetch", lambda url: PAGE)
    ctx = tools.ToolContext(session_id="s1")
    out = tools.slim("get_product", tools.call(ctx, "get_product", {"product_id": found["product_id"]}))
    assert out["product"]["about"].startswith("MDaemon Messaging Server")
    # диапазон цен по всем вариантам продукта модель не видит: "от N руб." по нему вводит клиента в заблуждение
    assert "price_min" not in out["product"] and "price_max" not in out["product"]
    assert all(o.get("price") for o in out["offers"])  # цены конкретных вариантов на месте


def test_search_shows_a_short_description_only_from_the_store(monkeypatch):
    from app.agent import tools

    ctx = tools.ToolContext(session_id="s1")
    monkeypatch.setattr(cards, "_fetch", lambda url: pytest.fail("поиск не ходит на сайт за описаниями"))
    before = tools.slim("search_products", tools.call(ctx, "search_products", {"query": "MDaemon"}))
    assert all("about" not in p and "price_min" not in p for p in before["products"])
    monkeypatch.setattr(cards, "_fetch", lambda url: PAGE)
    tools.call(ctx, "get_product", {"product_id": before["products"][0]["product_id"]})  # описание сохранено
    after = tools.slim("search_products", tools.call(ctx, "search_products", {"query": "MDaemon"}))
    about = after["products"][0]["about"]
    assert about.startswith("MDaemon Messaging Server") and len(about) <= tools.ABOUT_IN_SEARCH
