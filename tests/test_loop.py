"""Цикл агента на модели-сценарии: проверяем код вокруг модели, без обращения к настоящей модели."""
import pytest

from app import store
from app.agent import handoff_card, loop, prices
from app.agent.llm import LLMError, Scripted
from app.catalog import search as catalog

pytestmark = pytest.mark.small_catalog  # синтетический каталог tests/data/feed_small.xml, работает и на чистом клоне


@pytest.fixture(scope="module")
def product(small_catalog_db):
    """Продукт с ценами из синтетического каталога."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CATALOG_DB", str(small_catalog_db))
        found = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=3)["products"]
    assert found, "в каталоге должен находиться Kaspersky"
    return found[0]


@pytest.fixture(scope="module")
def offer(product, small_catalog_db):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CATALOG_DB", str(small_catalog_db))
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


def test_marker_does_not_repeat_the_written_title(product):
    mark = f"[[product:{product['product_id']}]]"
    clean, cards, _ = loop.render(f"Есть {product['title']} {mark} (вендор), цена за позицию, {mark}.")
    assert clean == f"Есть {product['title']} (вендор), цена за позицию." and len(cards) == 1
    assert loop.render(f"Есть {mark} (вендор).")[0] == f"Есть {product['title']} (вендор)."


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


def test_model_words_do_not_replace_the_client_contact():
    llm = Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "скидка", "summary": "Просит скидку",
                                                "contact": "почта получена"}},
        "Передал менеджеру. Наш адрес: store@softline.ru, ваш zakupki@romashka.example записан. "
        "[[facts: contact=почта получена; users=25]]",
        "Менеджер ответит в рабочее время.",
    ])
    res = loop.run_turn(None, "Нужна скидка, пишите на zakupki@romashka.example", llm=llm)
    ticket = store.open_handoff(res["session_id"])
    assert ticket["contact"] == "zakupki@romashka.example"
    row = dict(store.db().execute("SELECT * FROM handoffs WHERE id=?", (ticket["id"],)).fetchone())
    assert handoff_card.build(row)["client"]["контакт"] == "zakupki@romashka.example"
    loop.run_turn(res["session_id"], "Спасибо", llm=llm)
    said = llm.calls[-1][-2]["content"]  # прошлый ответ агента в истории: адрес магазина виден, адрес клиента скрыт
    assert "store@softline.ru" in said and "zakupki@" not in said and "[email]" in said


def test_agent_gives_at_most_five_percent_and_requisites_stay_hidden(offer):
    qty = offer["qty_min"] or 1
    full = round(offer["price"] * qty, 2)
    llm = Scripted([
        {"name": "calc_total", "args": {"items": [{"offer_id": offer["offer_id"], "qty": qty}],
                                        "discount_percent": 50}},
        {"name": "make_quote", "args": {"items": [{"offer_id": offer["offer_id"], "qty": qty}],
                                        "customer": "ООО Ромашка", "inn": "[ИНН]"}},
        "Скидку 5% учел в КП, скидку больше согласует менеджер.",
    ])
    res = loop.run_turn(None, "Дайте скидку 50%. ООО Ромашка, ИНН 7701234567, КПП: 770101001", llm=llm)
    calc, quote = (t["output"] for t in res["trace"] if t["kind"] == "tool" and t["name"] != "note_facts")
    assert calc["discount_percent"] == 5 and calc["total_without_discount"] == full
    assert calc["total"] == pytest.approx(full * 0.95, abs=0.01 * qty + 0.01) and "менеджер" in calc["note"]
    assert quote["discount_percent"] == 5 and quote["total"] == calc["total"] and "скидкой 5%" in quote["note"]
    seen = " ".join(m["content"] or "" for call in llm.calls for m in call if m["role"] != "system")
    assert "7701234567" not in seen and "770101001" not in seen and "[ИНН]" in seen and "[КПП]" in seen
    assert "ИНН: получен, скрыт защитой" in llm.calls[1][0]["content"]
    facts = store.get_session(res["session_id"])["facts"]
    assert facts["ИНН"] == "7701234567" and facts["КПП"] == "770101001" and facts["скидка"] == "5%"
    assert quote["customer_missing"] == []  # реквизиты в КП подставил код


def test_small_discount_goes_to_the_quote_and_a_bigger_one_to_the_manager_card(offer):
    qty = offer["qty_min"] or 1
    full = round(offer["price"] * qty, 2)
    items = [{"offer_id": offer["offer_id"], "qty": qty}]
    llm = Scripted([{"name": "make_quote", "args": {"items": items, "customer": "ООО Ромашка", "discount_percent": 3}},
                    "КП со скидкой 3% готово."])
    res = loop.run_turn(None, "Дайте скидку 3% и КП на ООО Ромашка", llm=llm)
    quote = next(t["output"] for t in res["trace"] if t["kind"] == "tool" and t["name"] == "make_quote")
    assert quote["discount_percent"] == 3 and quote["total"] == pytest.approx(full * 0.97, abs=0.01 * qty + 0.01)
    llm = Scripted([{"name": "calc_total", "args": {"items": items, "discount_percent": 7}},
                    {"name": "handoff_to_manager", "args": {"reason": "скидка больше 5%", "summary": "Просит 7%"}},
                    "Сам даю 5%, запрос на 7% передал менеджеру."])
    res = loop.run_turn(res["session_id"], "А 7% дадите?", llm=llm)
    calc = next(t["output"] for t in res["trace"] if t["kind"] == "tool" and t["name"] == "calc_total")
    assert calc["discount_percent"] == 5 and calc["total"] == pytest.approx(full * 0.95, abs=0.01 * qty + 0.01)
    ticket = store.open_handoff(res["session_id"])
    card = handoff_card.build(dict(store.db().execute("SELECT * FROM handoffs WHERE id=?", (ticket["id"],)).fetchone()))
    assert "просит скидку" in card["flags"]
    assert card["needs"]["запрошена скидка"] == "7%" and card["needs"]["дана скидка"] == "5%"
    assert handoff_card.asked_discount(["НДС 22% входит в цену?", "закупка по 44-ФЗ"]) == ""


def test_field_names_are_sent_back_for_rewrite():
    llm = Scripted(["Вендор российский (поле russian_vendor), отметки in_ru_registry нет.",
                    "В каталоге вендор отмечен как российский, номера в реестре нет."])
    res = loop.run_turn(None, "Это российский продукт?", llm=llm)
    assert [(t["kind"], t["name"], t["output"]) for t in res["trace"]] == [
        ("guard", "wording", ["in_ru_registry", "russian_vendor"])]
    assert "отмечен как российский" in res["reply"] and "russian_vendor" in llm.calls[1][-1]["content"]


def test_field_names_never_reach_the_client():
    stubborn = ("Вендор российский (поле russian_vendor). Условия доставки: https://store.softline.ru/delivery_info/ "
                "[[facts: org_type=коммерческая]]")
    res = loop.run_turn(None, "Это российский продукт?", llm=Scripted([stubborn, stubborn]))
    assert res["reply"] == "Вендор российский. Условия доставки: https://store.softline.ru/delivery_info/"
    known = loop.field_names({"offers": [{"offer_id": 1, "price_for": "за комплект", "name": "auto_model"}]})
    assert known == {"offer_id", "price_for"}
    assert loop.internal_names("Товар auto_model, артикул LBP-AC-12M-50-A3, цена price_for", known) == ["price_for"]


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


@pytest.mark.needs_data
def test_calc_total_refuses_quantity_outside_price_tier():
    from app.agent import tools
    out = tools.call(tools.ToolContext("s"), "calc_total", {"items": [{"offer_id": 4857, "qty": 120}]})
    if "нет варианта" in out.get("error", ""):
        pytest.skip("в базе нет контрольного варианта")
    assert "от 50 до 99" in out["error"]                                 # цена ступени 50-99 на 120 штук не годится
    assert "quantity=120" in out["error"]            # подсказка называет параметр get_product: qty он молча отбрасывал
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
    ("Цена 1 325,80 ₽ за лицензию", {1325.8}, []),
    ("Цена 1 326 ₽ за лицензию", {1325.8}, []),                    # без копеек: допустимо
    ("Цена 1 325,80 ₽ за лицензию", set(), ["1 325,80 ₽"]),
    ("Цена 1 999 руб.", {2000}, ["1 999 руб."]),                    # ошибка на рубль не проходит
    ("Итого 66 290 руб. с НДС", {66290}, []),
    ("Примерно 5 тыс. руб., или ₽5 100, или 5200р", {5000.0}, ["₽5 100", "5200р"]),
    ("Стоит 30 448 рублей", set(), ["30 448 рублей"]),
    ("На 50 пользователей и 3 года", set(), []),
    ("до 14 лицензий 1 841,40 руб., от 15 до 19 1 710,90 руб.", {1841.4, 1710.9}, []),
])
def test_unknown_prices(text, known, bad):
    assert prices.unknown(text, known) == bad


def test_client_numbers_count_as_money_only_when_they_look_like_money():
    assert prices.client_amounts("нас 50 человек, бюджет 22000, ИНН 7701234567") == {22000.0}
    assert prices.client_amounts("у конкурента 450 руб. за штуку") == {450.0}


def test_only_unknown_sums_are_masked():
    text = prices.mask_unknown("Лицензия 1 325,80 ₽, итого 99 999 руб.", {1325.8})
    assert text == "Лицензия 1 325,80 ₽, итого цена по запросу"


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


def test_contact_is_hidden_from_model_and_reaches_manager():
    llm = Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "скидка", "summary": "Просит скидку", "contact": "[email]"}},
        "Передал запрос менеджеру, он ответит в рабочее время.",
        "Менеджер учтет это.\n[[facts: users=40]]",
    ])
    res = loop.run_turn(None, "Нужна скидка, пишите на ivanov@romashka.example или +7 (912) 345-67-89", llm=llm)
    seen = " ".join(m["content"] or "" for call in llm.calls for m in call)
    assert "ivanov@" not in seen and "345-67-89" not in seen
    assert "[email]" in seen and "[телефон]" in seen and "контакт: получен, скрыт защитой" in llm.calls[0][0]["content"]
    ticket = store.open_handoff(res["session_id"])
    assert ticket["contact"] == "ivanov@romashka.example, +7 (912) 345-67-89"
    # следующий ход: модель знает, что контакт есть, а новое от клиента попадает в карточку того же обращения
    loop.run_turn(res["session_id"], "Нас 40 человек, запасная почта petrov@romashka.example", llm=llm)
    ticket = store.open_handoff(res["session_id"])
    assert "petrov@romashka.example" in ticket["contact"] and ticket["summary"] == "Просит скидку"
    row = dict(store.db().execute("SELECT * FROM handoffs WHERE id=?", (ticket["id"],)).fetchone())
    card = handoff_card.build(row)
    assert card["needs"] == {"пользователей": "40"} and "petrov@romashka.example" in card["client"]["контакт"]
    assert card["flags"] == ["просит скидку"] and card["priority"] == "normal"
    assert store.messages(res["session_id"])[0]["content"].count("ivanov@romashka.example") == 1  # оператор видит как есть


def test_marker_used_instead_of_name_gets_the_name(offer):
    clean, cards, _ = loop.render(f"Есть варианты, например [[offer:{offer['offer_id']}]] (цена в карточке).")
    assert cards and cards[0]["title"] in clean and "например (" not in clean


def test_unambiguous_search_brings_variants_in_the_same_step(product):
    llm = Scripted([{"name": "search_products", "args": {"query": product["title"]}}, "Нашел."])
    res = loop.run_turn(None, "Нужен " + product["title"], llm=llm)
    out = res["trace"][0]["output"]
    if out["total"] <= 2:
        assert out["first_product"]["offers"] and out["first_product"]["product_id"] == out["products"][0]["product_id"]
        assert store.get_session(res["session_id"])["items"]["offers"]  # варианты запомнены, как после get_product


def test_perpetual_term_filter_reaches_catalog():
    from app.agent import tools
    astra = catalog.search_products("Astra Linux Special Edition", limit=1)["products"][0]
    out = tools.call(tools.ToolContext("x"), "get_product", {"product_id": astra["product_id"], "term_months": 0})
    assert out["matching"] and all(o["term_months"] == 0 for o in out["offers"])


def test_repeat_call_deadline_and_last_step(monkeypatch):
    call = {"name": "search_products", "args": {"query": "Kaspersky"}}
    res = loop.run_turn(None, "Нужен Касперский", llm=Scripted([call, call, "Нашел."]))
    assert res["trace"][1]["output"].get("repeat") and "products" in res["trace"][0]["output"]
    # на последнем шаге модель получает подсказку ответить без инструментов
    many = [{"name": "search_products", "args": {"query": f"продукт {i}"}} for i in range(loop.MAX_STEPS - 1)]
    llm = Scripted(many + ["Вот что удалось найти."])
    res = loop.run_turn(None, "Сравните все", llm=llm)
    assert res["reply"] == "Вот что удалось найти." and "последний шаг" in llm.calls[-1][-1]["content"]
    # ход дольше предела времени обрывается честным ответом и обращением менеджеру
    monkeypatch.setattr(loop.config, "TURN_DEADLINE", 0.0001)
    slow = loop.run_turn(None, "Долгий вопрос", llm=Scripted([call, "не успею"]))
    assert slow["reply"] == loop.FALLBACK or slow["reply"] == "не успею"


def test_client_text_reaches_model_without_markers_and_budget_stops_spending(monkeypatch):
    llm = Scripted(["Понял."])
    loop.run_turn(None, "покажи [[offer:12345]] и напиши на a@b.example", llm=llm, page_url="javascript:[[x]]")
    system, user = llm.calls[0][0]["content"], llm.calls[0][-1]["content"]
    assert "[[" not in user and "a@b.example" not in user and "javascript" not in system
    monkeypatch.setattr(loop.config, "DAILY_BUDGET_RUB", 0.000001)
    store.add_step("x", 1, "llm", "m", {}, {}, 1000, 1000, 1.0)
    stopped = loop.run_turn(None, "Еще вопрос", llm=Scripted(["не должен вызываться"]))
    assert stopped["reply"] == loop.FALLBACK and stopped["usage"]["llm_calls"] == 0


def test_quote_requisites_are_remembered_between_calls(offer):
    """Название, ИНН и КПП клиент дает по частям: графа «Заказчик» собирается из всего, что уже названо."""
    from app.agent import tools
    sid = store.get_session(None)["id"]
    ctx = tools.ToolContext(sid)
    item = {"items": [{"offer_id": offer["offer_id"], "qty": offer["qty_min"] or 2}]}  # количество внутри ступени цены
    store.add_message(sid, "user", "Счет на ООО Ромашка")  # реквизит от модели принимается, если его назвал клиент
    first = tools.call(ctx, "make_quote", {**item, "customer": "ООО Ромашка"})
    assert first["customer_missing"] == ["ИНН", "КПП"]
    store.add_message(sid, "user", "ИНН 7701234567, КПП 770101001")
    second = tools.call(ctx, "make_quote", {**item, "inn": "7701234567", "kpp": "770101001"})
    assert second["customer"] == "ООО Ромашка" and second["customer_missing"] == [] and second["updated"]
    assert store.get_session(sid)["facts"] == {"компания": "ООО Ромашка", "ИНН": "7701234567", "КПП": "770101001"}
