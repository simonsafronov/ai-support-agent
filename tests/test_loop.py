"""Цикл агента на модели-сценарии: проверяем код вокруг модели, без обращения к настоящей модели."""
import pytest

from app import store
from app.agent import loop
from app.agent.llm import LLMError, Scripted
from app.catalog import search as catalog


@pytest.fixture(scope="module")
def product():
    """Продукт с ценами из боевого каталога."""
    found = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=3)["products"]
    assert found, "в каталоге должен находиться Kaspersky"
    return found[0]


@pytest.fixture(scope="module")
def offer(product):
    offers = catalog.get_product(product["product_id"], limit=5)["offers"]
    priced = [o for o in offers if o["price"]]
    assert priced
    return priced[0]


def test_tool_call_then_reply_with_card(product):
    llm = Scripted([
        {"name": "search_products", "args": {"query": "Kaspersky Endpoint Security"}},
        f"Нашел продукт. [[product:{product['product_id']}]]\nНа сколько пользователей нужна лицензия?\n"
        "[[choices: до 10 | 10-49 | 50 и больше]]",
    ])
    events = []
    res = loop.run_turn(None, "Нужен Касперский для офиса", llm=llm, on_event=events.append)
    assert events == [{"type": "tool", "name": "search_products", "args": {"query": "Kaspersky Endpoint Security"}}]
    assert "[[" not in res["reply"]
    assert res["cards"][0]["id"] == product["product_id"]
    assert res["choices"] == ["до 10", "10-49", "50 и больше"]
    assert res["usage"]["llm_calls"] == 2 and res["usage"]["tool_calls"] == 1
    assert [t["kind"] for t in res["trace"]] == ["tool"]
    # второй вызов модели получил результат инструмента
    assert llm.calls[1][-1]["role"] == "tool" and "products" in llm.calls[1][-1]["content"]


def test_price_from_catalog_passes_guard(product, offer):
    llm = Scripted([
        {"name": "get_product", "args": {"product_id": str(product["product_id"])}},
        f"Подходит вариант за {offer['price']:.2f} руб. [[offer:{offer['offer_id']}]]".replace(".", ","),
    ])
    res = loop.run_turn(None, "Сколько стоит?", llm=llm)
    assert not [t for t in res["trace"] if t["kind"] == "guard"]
    assert res["cards"][0]["type"] == "offer" and res["cards"][0]["price"] == offer["price"]


def test_invented_price_is_sent_back_for_rewrite():
    llm = Scripted(["Лицензия стоит 12 345 ₽.", "Цена по запросу, уточню у менеджера."])
    res = loop.run_turn(None, "Сколько стоит редкий продукт?", llm=llm)
    assert [t["kind"] for t in res["trace"]] == ["guard"]
    assert "12 345" not in res["reply"] and "по запросу" in res["reply"]
    assert "Служебная проверка" in llm.calls[1][-1]["content"]


def test_invented_price_twice_is_masked():
    llm = Scripted(["Стоит 12 345 ₽.", "Точно стоит 12 345 руб."])
    res = loop.run_turn(None, "Сколько стоит?", llm=llm)
    assert "12 345" not in res["reply"] and "цена по запросу" in res["reply"]


def test_amount_named_by_client_is_allowed():
    llm = Scripted(["В бюджет 300 000 руб. подберу два варианта. Сколько сотрудников?"])
    res = loop.run_turn(None, "Бюджет 300 000 рублей, нужна защита почты", llm=llm)
    assert not res["trace"] and "300 000" in res["reply"]


def test_calc_total_feeds_the_guard(offer):
    qty = offer.get("qty_min") or 50                     # количество внутри ступени этого варианта
    total = round(offer["price"] * qty, 2)
    llm = Scripted([
        {"name": "calc_total", "args": {"items": [{"offer_id": offer["offer_id"], "qty": qty}]}},
        f"За {qty} лицензий выйдет {total:.2f} руб.",
    ])
    res = loop.run_turn(None, f"Посчитайте на {qty} человек", llm=llm)
    assert res["trace"][0]["output"]["total"] == total
    assert not [t for t in res["trace"] if t["kind"] == "guard"]


def test_calc_total_refuses_quantity_outside_price_tier():
    from app.agent import tools
    out = tools.call(tools.ToolContext("s"), "calc_total", {"items": [{"offer_id": 4857, "qty": 120}]})
    if "нет варианта" in out.get("error", ""):
        pytest.skip("в базе нет контрольного варианта")
    assert "от 50 до 99" in out["error"] and "qty=120" in out["error"]   # цена ступени 50-99 на 120 штук не годится
    ok = tools.call(tools.ToolContext("s"), "calc_total", {"items": [{"offer_id": 4857, "qty": 50}]})
    assert ok["total"] == 197010.0 and ok["lines"][0]["price_for"] == "за 1 лицензию при покупке от 50 до 99"


def test_model_failure_gives_fallback_and_handoff():
    class Broken:
        def chat(self, *a, **k):
            raise LLMError("модель недоступна")

    res = loop.run_turn(None, "Здравствуйте", llm=Broken())
    assert res["reply"] == loop.FALLBACK
    assert res["events"][0]["type"] == "handoff"
    assert store.db().execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


def test_step_limit_stops_endless_tool_calls():
    llm = Scripted([{"name": "kb_search", "args": {"query": "оплата"}}] * 20)
    res = loop.run_turn(None, "Как оплатить?", llm=llm)
    assert res["usage"]["llm_calls"] == loop.MAX_STEPS
    assert res["reply"] == loop.FALLBACK


def test_unknown_tool_and_bad_args_do_not_break_turn():
    llm = Scripted([
        {"name": "drop_database", "args": {}},
        {"name": "get_product", "args": {"wrong": 1}},
        "Уточните, пожалуйста, название продукта.",
    ])
    res = loop.run_turn(None, "Привет", llm=llm)
    assert "error" in res["trace"][0]["output"] and "error" in res["trace"][1]["output"]
    assert res["reply"].startswith("Уточните")


def test_facts_survive_between_turns_and_visits():
    llm = Scripted(["Записал. Какая задача?\n[[facts: company=ООО Ромашка; users=50; unknown_field=x]]"])
    first = loop.run_turn(None, "Мы ООО Ромашка, 50 человек", client_id="c1", llm=llm)
    sid = first["session_id"]
    assert first["reply"] == "Записал. Какая задача?"                       # метку клиент не видит
    assert first["trace"][-1]["output"]["saved"] == {"компания": "ООО Ромашка", "пользователей": "50"}
    assert first["usage"]["llm_calls"] == 1                                 # память не стоит вызова модели

    llm2 = Scripted(["Понял."])
    loop.run_turn(sid, "Защита почты", client_id="c1", llm=llm2)
    system = llm2.calls[0][0]["content"]
    assert "ООО Ромашка" in system and "пользователей: 50" in system

    llm3 = Scripted(["Здравствуйте снова."])
    loop.run_turn(None, "Добрый день", client_id="c1", llm=llm3)  # новый диалог того же клиента
    assert "уже обращался" in llm3.calls[0][0]["content"] and "ООО Ромашка" in llm3.calls[0][0]["content"]


def test_handoff_tool_creates_ticket_for_operator():
    llm = Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "скидка", "summary": "просит скидку на 200 лицензий",
                                                "contact": "it@romashka.ru"}},
        "Передал менеджеру, он ответит в рабочее время.",
    ])
    res = loop.run_turn(None, "Дайте скидку", llm=llm)
    assert res["events"] == [{"type": "handoff", "id": 1, "reason": "скидка"}]
    row = store.db().execute("SELECT reason, contact FROM handoffs").fetchone()
    assert tuple(row) == ("скидка", "it@romashka.ru")
    assert store.get_session(res["session_id"])["status"] == "handoff"


def test_page_hint_names_product_from_url():
    hint = loop.page_hint("https://store.softline.ru/kaspersky/kaspersky-endpoint-security-dlya-biznesa-standartnyy/")
    assert "Клиент пишет со страницы" in hint


@pytest.mark.parametrize("text,known,bad", [
    ("Цена 1 325,80 ₽ за лицензию", {1326}, []),
    ("Цена 1 325,80 ₽ за лицензию", set(), ["1 325,80 ₽"]),
    ("Итого 66 290 руб. с НДС", {66290}, []),
    ("На 50 пользователей и 3 года", set(), []),
])
def test_unknown_prices(text, known, bad):
    assert loop.unknown_prices(text, known) == bad


def test_long_tool_result_stays_valid_json():
    import json
    out = {"query": "x", "products": [{"title": "Продукт " * 30, "n": i} for i in range(40)], "total": 40}
    text = loop.fit_json(out, 3000)
    data = json.loads(text)
    assert len(text) <= 3000 and 1 <= len(data["products"]) < 40
    assert data["truncated"]["products"] == 40 - len(data["products"])
    assert json.loads(loop.fit_json({"a": 1}, 3000)) == {"a": 1}


def test_markers_leave_clean_punctuation():
    text, _, _ = loop.render("Вариант: 3 940,20 руб. [[offer:1]]. Также есть: [[product:2]] Dr.Web, цена ниже...")
    assert text == "Вариант: 3 940,20 руб. Также есть: Dr.Web, цена ниже..."


def test_model_sees_tool_output_without_links_and_unknown_flags():
    from app.agent import tools
    out = {"products": [{"product_id": "1", "title": "A", "url": "https://x", "in_ru_registry": False,
                         "russian_vendor": True, "price_min": None}],
           "offers": [{"offer_id": 2, "price": None, "vendor_code": "", "available": False, "file": "/tmp/x"}]}
    assert tools.slim("get_product", out) == {
        "products": [{"product_id": "1", "title": "A", "russian_vendor": True}],
        "offers": [{"offer_id": 2, "price": "по запросу", "available": False}]}
    kb = {"results": [{"title": "Оплата", "url": "https://store.softline.ru/help/pay/"}]}
    assert tools.slim("kb_search", kb) == kb                                # на справку агент ссылается


def test_total_from_earlier_turn_can_be_repeated(offer):
    qty = offer.get("qty_min") or 3
    total = round(offer["price"] * qty, 2)
    first = loop.run_turn(None, "Посчитайте", llm=Scripted([
        {"name": "calc_total", "args": {"items": [{"offer_id": offer["offer_id"], "qty": qty}]}},
        f"Итого {total:.2f} руб."]))
    again = loop.run_turn(first["session_id"], "Повторите сумму", llm=Scripted([f"Итого {total:.2f} руб."]))
    assert not again["trace"] and "Итого" in again["reply"] and "по запросу" not in again["reply"]


def test_second_handoff_updates_the_same_ticket():
    from app import store
    first = loop.run_turn(None, "Позовите менеджера", llm=Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "просит человека", "summary": "просит менеджера"}},
        "Передал менеджеру. Оставьте, пожалуйста, почту."]))
    sid = first["session_id"]
    second = loop.run_turn(sid, "it@romashka.ru", llm=Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "контакт", "summary": "просит менеджера, оставил почту",
                                                "contact": "it@romashka.ru"}},
        "Спасибо, менеджер напишет на эту почту."]))
    assert second["trace"][0]["output"]["updated"] and not second["events"]      # второго обращения нет
    assert store.open_handoff(sid)["contact"] == "it@romashka.ru"
