"""Волна 3, сервер: данные клиента в режиме просмотра, PDF КП, поток ответа, поля для чата и экрана оператора,
проверка входа, страница 404, чистка базы, показ проверки цен, витрина."""
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from app import cases, config, store
from app.agent import handoff_card, loop, proof
from app.agent.llm import Scripted
from app.web import server

client = TestClient(server.app, raise_server_exceptions=False)
KEY = {"X-Admin-Token": "secret"}


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    server._recent.clear()


def _dialog(text: str, client_id: str = "w3-exC-client") -> str:
    sid = store.get_session(None, client_id)["id"]
    store.add_message(sid, "user", text)
    return sid


def _rub(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")


def _stream(r) -> list[dict]:
    return [json.loads(line) for line in r.text.strip().splitlines()]


# --- режим просмотра без ключа: данные клиента скрыты (F-03-01, F-07-01, F-08-02) ---

SECRETS = ["1157746123456",              # ОГРН, 13 цифр, контрольная цифра неверна: клиенты пишут и выдуманные
           "1027736009333",              # ОГРН с верной контрольной цифрой
           "304500116000157",            # ОГРНИП, 15 цифр
           "40702810502300000368",       # расчетный счет, 20 цифр
           "7701 234 567",               # ИНН с пробелами
           "770 101 001",                # КПП с пробелами
           "Иван Петров",                # имя после "контактное лицо"
           "Анна Сергеевна",             # имя после "меня зовут"
           "Сидоров Петр",               # имя после "ФИО"
           "@ivan_petrov", "t.me/ivan_petrov", "vk.com/id123456", "wa.me/79161234567",
           "4276 1234 5678 9012",        # карта группами по четыре цифры
           "4111111111111111",           # карта одним рядом, верная по Луну
           "4510 123456"]                # паспорт
MESSAGE = ("Компания ООО Тест, ОГРН 1157746123456, ОГРН поставщика 1027736009333, ИП 304500116000157, "
           "р/с 40702810502300000368, ИНН 7701 234 567, КПП 770 101 001, контактное лицо Иван Петров. "
           "Меня зовут Анна Сергеевна. ФИО: Сидоров Петр. Пишите @ivan_petrov, t.me/ivan_petrov, vk.com/id123456 "
           "или wa.me/79161234567. Карта 4276 1234 5678 9012 или 4111111111111111, паспорт 4510 123456.")
# что режим просмотра оставляет: суммы, цены, артикулы, количество и даты
KEPT = ["1 234 567 890 руб.", "1234567890 руб.", "388 737 руб.", "6 103,80 ₽", "123 456 789 ₽", "KL4863RAQFS",
        "арт. 100600878", "артикул 2900001931908", "4276 1234 руб.", "50 пользователей", "26.11.2026"]
KEPT_MESSAGE = ("Итого 1 234 567 890 руб., без пробелов 1234567890 руб., сумма 388 737 руб., цена 6 103,80 ₽, "
                "бюджет 123 456 789 ₽, KL4863RAQFS, арт. 100600878, артикул 2900001931908, остаток 4276 1234 руб., "
                "50 пользователей, до 26.11.2026")


def _handed_dialog() -> str:
    sid = _dialog(MESSAGE, "eval-w3-exC")  # без ключа видны только диалоги прогона и показа
    store.add_message(sid, "user", KEPT_MESSAGE)
    store.handoff(sid, "скидка", "Клиент ООО Тест (контактное лицо Иван Петров), ОГРН 1157746123456", "")
    store.add_step(sid, 1, "tool", "get_product", {"product_id": "1"},
                   {"offers": [{"offer_id": 503516, "vendor_code": "2900001931908", "name": "Лицензия"}]})
    return sid


@pytest.mark.parametrize("secret", SECRETS)
def test_view_mode_hides_client_data_in_every_api_answer(secret):
    sid = _handed_dialog()
    for path in (f"/api/admin/session/{sid}", "/api/admin/sessions", "/api/admin/stats"):
        r = client.get(path)
        assert r.status_code == 200 and r.json()["role"] == "demo"
        assert secret not in r.text, path


def test_view_mode_keeps_sums_prices_and_articles():
    sid = _handed_dialog()
    text = client.get(f"/api/admin/session/{sid}").text
    for kept in KEPT:
        assert kept in text, kept
    assert '"vendor_code": "2900001931908"' in text.replace('":"', '": "')  # артикул в поле не маскируется
    marks = json.loads(text)["messages"][0]["content"]
    assert "контактное лицо [имя]" in marks and "[карта]" in marks and "паспорт [паспорт]" in marks


def test_operator_with_key_sees_everything():
    sid = _handed_dialog()
    text = client.get(f"/api/admin/session/{sid}", headers=KEY).text
    assert all(s in text for s in SECRETS)


def test_card_and_passport_do_not_reach_the_model_and_are_not_a_contact():
    said = "Паспорт 4510 123456, карта 4276 1234 5678 9012, почта a@romashka.example"
    seen = loop.for_model(said)
    assert "4510" not in seen and "4276" not in seen and "[паспорт]" in seen and "[карта]" in seen
    llm = Scripted(["Спасибо, записал почту."])
    res = loop.run_turn(None, said, client_id="w3-exC-card", llm=llm)
    assert "4276" not in json.dumps(llm.calls, ensure_ascii=False)
    assert store.get_session(res["session_id"])["facts"]["контакт"] == "a@romashka.example"


@pytest.mark.parametrize("text", ["заказ 1234567890123456", "арт. 9500321311-000331", "итого 1 234 567 руб.",
                                  "серийный 4606892013621 050 79"])
def test_card_mask_leaves_long_numbers(text):
    assert loop.mask_contacts(text)[0] == text


# --- PDF КП открывается во вкладке и не кэшируется (F-01-06, F-07-03, F-07-04) ---

def _quote_file() -> str:
    sid = _dialog("нужно КП")
    config.QUOTES_DIR.mkdir(parents=True, exist_ok=True)
    (config.QUOTES_DIR / "KP-T-1.pdf").write_bytes(b"%PDF-1.4 test")
    store.add_quote(sid, "KP-T-1", "ООО Тест ИНН 7701234567", [], 1000.0, str(config.QUOTES_DIR / "KP-T-1.pdf"))
    return "/quotes/KP-T-1.pdf?k=" + server.quote_key("KP-T-1.pdf")


def test_quote_pdf_opens_inline_and_is_not_cacheable():
    r = client.get(_quote_file())
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["cache-control"] == "private, no-store"


# --- поток ответа: первая строка с номером диалога, ожидание без потока из общего пула (F-07-02, договор п. 1) ---

def _fake_turn(sid, text, **_):
    store.add_message(sid, "user", text)
    return {"session_id": sid, "reply": "ok", "cards": [], "choices": [], "events": [], "trace": []}


def test_chat_stream_starts_with_session_line(monkeypatch):
    monkeypatch.setattr(server.loop, "run_turn", _fake_turn)
    lines = _stream(client.post("/api/chat", json={"message": "привет", "client_id": "w3-exC-s"}))
    assert lines[0] == {"type": "session", "session_id": lines[-1]["session_id"]} and lines[-1]["type"] == "reply"


def test_chat_stream_waits_without_a_pool_thread(monkeypatch):
    monkeypatch.setattr(server.loop, "run_turn", _fake_turn)
    r = server.chat(server.ChatIn(message="привет", client_id="w3-exC-a"))
    assert inspect.isasyncgen(r.body_iterator)  # синхронный генератор держал бы поток пула все время ожидания модели


# --- история диалога: расход и шаги хода с аргументами (договор п. 2) ---

def test_session_history_has_usage_and_trace_with_args(monkeypatch):
    steps = [{"name": "handoff_to_manager", "args": {"reason": "скидка", "summary": "просит скидку 10%"}},
             "Это стоит 12345 руб.", "Передал менеджеру."]
    monkeypatch.setattr(server.loop, "get_llm", lambda: Scripted(steps))
    reply = _stream(client.post("/api/chat", json={"message": "Дайте скидку 10%", "client_id": "w3-exC-h"}))[-1]
    client.post("/api/chat/handoff", json={"session_id": reply["session_id"], "client_id": "w3-exC-h"})
    h = client.get(f"/api/session/{reply['session_id']}?client_id=w3-exC-h").json()
    agent, button = [m for m in h["messages"] if m["role"] == "assistant"]
    assert [{k: v for k, v in t.items() if k != "args"} for t in agent["trace"]] == reply["trace"]
    tool = agent["trace"][0]
    assert tool["args"] == steps[0]["args"] and [t["kind"] for t in agent["trace"]] == ["tool", "guard"]
    assert "trace" not in button  # кнопка "Позвать менеджера" не ход агента
    s = store.get_session(reply["session_id"])
    assert h["usage"] == {"llm_calls": 3, "tool_calls": 1, "prompt_tokens": s["prompt_tokens"],
                          "completion_tokens": s["completion_tokens"],
                          "cost_rub": loop.cost_rub(s["prompt_tokens"], s["completion_tokens"])}


# --- срок реакции менеджера в событии передачи на всех путях (договор п. 3) ---

def test_handoff_event_has_sla_minutes_on_every_path(monkeypatch):
    monkeypatch.setattr(server.loop, "get_llm", lambda: Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "закупка", "summary": "закупка по 223-ФЗ"}}, "Передал."]))
    reply = _stream(client.post("/api/chat", json={"message": "Закупка по 223-ФЗ", "client_id": "w3-exC-sla"}))[-1]
    (event,) = [e for e in reply["events"] if e["type"] == "handoff"]
    h = dict(store.db().execute("SELECT * FROM handoffs WHERE id=?", (event["id"],)).fetchone())
    assert event["sla_minutes"] == handoff_card.build(h)["sla_minutes"] == handoff_card.SLA_MINUTES["high"]
    history = client.get(f"/api/session/{reply['session_id']}?client_id=w3-exC-sla").json()
    assert [e["sla_minutes"] for m in history["messages"] for e in m["events"] if e["type"] == "handoff"] == \
        [event["sla_minutes"]]
    button = client.post("/api/chat/handoff", json={"client_id": "w3-exC-btn"}).json()
    assert button["events"][0]["sla_minutes"] == handoff_card.SLA_MINUTES["normal"]
    monkeypatch.setattr(server.loop, "run_turn", lambda *a, **k: 1 / 0)  # сбой хода: обращение заводит сервер
    failed = _stream(client.post("/api/chat", json={"message": "Привет", "client_id": "w3-exC-fail"}))[-1]
    assert failed["events"][0]["type"] == "handoff" and failed["events"][0]["sla_minutes"] > 0


# --- ссылки на страницы справки в шаге kb_search (договор п. 4) ---

def test_kb_step_has_urls_only_of_store_and_docs():
    out = {"results": [{"title": "Продление", "heading": "h", "url": "https://docs.softline.com/ru-ru/lk/renewal"},
                       {"title": "Чужая", "heading": "h", "url": "https://evil.example/softline"}]}
    brief = server._brief("kb_search", out)
    assert brief["results"][0]["url"] == "https://docs.softline.com/ru-ru/lk/renewal"
    assert "url" not in brief["results"][1]
    out["results"][0]["url"] = "https://store.softline.ru/delivery_info/"
    assert server._brief("kb_search", out)["results"][0]["url"] == "https://store.softline.ru/delivery_info/"
    out["results"][0]["url"] = "javascript:alert(1)//store.softline.ru"
    assert "url" not in server._brief("kb_search", out)["results"][0]


# --- следующий шаг и заготовки по пометкам (договор п. 5, F-10-10) ---

def _card_for(text: str, reason: str = "вопрос") -> dict:
    sid = _dialog(text)
    hid = store.add_handoff(sid, reason, "", "")
    return handoff_card.build(dict(store.db().execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()))


def test_next_step_and_templates_follow_flags():
    card = _card_for("Нужна скидка 15%, закупка пойдет по 223-ФЗ")
    assert card["next"] == "Запросить ТЗ и условия закупки, проверить срок подачи заявок"
    assert [t["title"] for t in card["templates"]][:3] == ["Условия закупки", "Скидка", "Уточню наличие"]
    assert _card_for("Дайте скидку 15%")["next"] == "Согласовать скидку 15% с вендором или подтвердить 5%"
    assert _card_for("Безобразие, никто не отвечает, это жалоба")["next"] == "Позвонить клиенту сегодня"
    assert _card_for("Сколько стоит лицензия?")["next"] == "Ответить клиенту в чате"
    plain = _card_for("Как получить лицензию?")["templates"]
    assert [t["title"] for t in plain] == [t for t, _ in handoff_card.TEMPLATES]


def test_templates_have_no_sums_or_prices():
    texts = [t for _, _, t in handoff_card.BY_FLAG if t] + list(handoff_card.TEMPLATES)
    assert not any(ch.isdigit() or "руб" in body or "₽" in body for _, body in texts for ch in body)


# --- карточка не противоречит себе, когда КП собран после передачи в том же ходе (F-05-03) ---

@pytest.fixture(scope="module")
def priced_offer():
    from app.catalog import search as catalog
    product = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=1)["products"][0]
    offer = next(o for o in catalog.get_product(product["product_id"], limit=20)["offers"] if o.get("price"))
    return {**offer, "qty": offer.get("qty_min") or 1}


@pytest.mark.needs_data
def test_card_agrees_with_quote_made_after_handoff(priced_offer):
    o, qty = priced_offer, priced_offer["qty"]
    items = [{"offer_id": o["offer_id"], "qty": qty}]
    list_sum = round(o["price"] * qty, 2)
    first = loop.run_turn(None, "Сделайте КП на ООО Ромашка, ИНН 7701234567", client_id="w3-exC-kp", llm=Scripted([
        {"name": "make_quote", "args": {"items": items, "customer": "ООО Ромашка"}}, "КП готово."]))
    sid = first["session_id"]
    summary = (f"ООО Ромашка. Выбрано: {o['name']} ({qty} шт., {_rub(list_sum)} руб.), итого {_rub(list_sum)} руб. "
               "Клиент просит скидку 15%.")
    # как в ходе 4 сценария питча: сначала передача менеджеру, затем КП со скидкой в том же ходе
    loop.run_turn(sid, "Нужна скидка 15%, закупка пойдет по 223-ФЗ", client_id="w3-exC-kp", llm=Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "скидка 15% и 223-ФЗ", "summary": summary}},
        {"name": "make_quote", "args": {"items": items, "discount_percent": 15}}, "Передал менеджеру."]))
    (h,) = client.get("/api/admin/stats", headers=KEY).json()["handoffs"]
    card, total = h["card"], store.last_quote(sid)["total"]
    assert total < list_sum and card["quote"]["total"] == total
    (line,) = card["offers"]
    assert line["discount_percent"] == 5 and line["list_price"] == o["price"] and line["price"] < o["price"]
    assert _rub(list_sum) not in h["summary"] and f"итого {_rub(total)} руб. со скидкой 5%" in h["summary"]
    assert h["summary"].startswith(f"ООО Ромашка. Выбрано: {o['name']} ({qty} шт.).")
    text = server.notify.card_text(h, card)  # та же карточка уходит в Telegram
    assert f"со скидкой 5%, по каталогу {server.notify._rub(o['price'])}" in text and _rub(list_sum) not in text


def test_summary_without_quote_is_left_as_is():
    assert handoff_card.agreed_summary("итого 1 000 руб.", None, [], 0) == "итого 1 000 руб."


# --- проверка входа (F-07-07, F-08-06, F-07-08, F-07-09, F-07-10) ---

def test_blank_message_does_not_start_a_turn(monkeypatch):
    started = []
    monkeypatch.setattr(server.loop, "run_turn", lambda *a, **k: started.append(a))
    r = client.post("/api/chat", json={"message": "   \n\t ", "client_id": "w3-exC-blank"})
    assert r.status_code == 422 and started == []
    assert client.post("/api/admin/handoff/1/answer", json={"answer": "  "}, headers=KEY).status_code == 422


@pytest.mark.parametrize("action", ["answer", "take", "return"])
def test_huge_handoff_id_is_rejected_not_500(action):
    r = client.post(f"/api/admin/handoff/99999999999999999999/{action}", json={"answer": "x"}, headers=KEY)
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/api/admin/sessions?limit=-1", "/api/admin/sessions?limit=0",
                                  "/api/admin/sessions?limit=501", "/api/products?limit=0",
                                  "/api/products?q=kaspersky&limit=-5", "/api/products?limit=25"])
def test_limit_outside_bounds_is_rejected(path):
    assert client.get(path).status_code == 422


def test_large_body_is_413_and_422_does_not_echo_the_body():
    r = client.post("/api/chat", content=json.dumps({"message": "a" * 70_000}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post("/api/chat", json={"message": "b" * 5000, "client_id": "w3-exC-long"})
    assert r.status_code == 422 and "bbbb" not in r.text and r.json()["detail"][0]["loc"] == ["body", "message"]


# --- страница 404 (F-05-07) ---

@pytest.mark.parametrize("path", ["/no-such", "/admin/x/y", "/quotes/KP-404.pdf"])
def test_missing_page_is_html_with_links_to_three_screens(path):
    r = client.get(path)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert all(f'href="{a}"' in r.text for a in ("/", "/admin", "/proof")) and 'href="/app.css"' in r.text


@pytest.mark.parametrize("method,path", [("GET", "/api/no-such"), ("POST", "/api/no-such/deeper")])
def test_missing_api_address_stays_json(method, path):
    r = client.request(method, path)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


# --- чистка по сроку и кейсы со стенда (F-07-05, F-07-06) ---

def test_purged_contacts_do_not_stay_in_database_files():
    sid = _dialog("моя почта purge-check-w3exc@example.com")
    con = store.db()
    con.execute("UPDATE sessions SET updated_at=0 WHERE id=?", (sid,))
    con.commit()
    assert store.purge(1) == 1
    raw = b"".join(p.read_bytes() for p in config.APP_DB.parent.glob(config.APP_DB.name + "*"))
    assert b"purge-check-w3exc@example.com" not in raw


def test_case_from_stand_drops_requisites(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "ADDED", tmp_path / "cases_added.jsonl")
    monkeypatch.setattr(cases, "OLD", tmp_path / "old.jsonl")
    sid = _dialog("ИНН 7701234567, ОГРН 1157746123456, почта a@b.example, нужен Kaspersky на 50 ПК")
    r = client.post(f"/api/admin/session/{sid}/case", json={"handoff": True}, headers=KEY)
    assert r.status_code == 200
    saved = json.loads((tmp_path / "cases_added.jsonl").read_text(encoding="utf-8"))
    assert saved["turns"] == ["ИНН [реквизит], ОГРН [реквизит], почта [email], нужен Kaspersky на 50 ПК"]


# --- /proof: второй исход не обещает особую цену, первый не спорит с правилом 5% (F-09-08) ---

@pytest.mark.needs_data
def test_proof_does_not_promise_special_price():
    corrected, insisted = proof.price_guard()["runs"]
    assert "по запросу" in insisted["client_saw"]
    for run in (corrected, insisted):
        assert "специальн" not in run["client_saw"].lower() and "постоянн" not in run["client_saw"].lower()
    assert f"до {config.AGENT_DISCOUNT_MAX:g}%" in corrected["client_saw"]


# --- витрина: "от N" только по новым коммерческим лицензиям (F-08-08) ---

@pytest.mark.needs_data
def test_showcase_price_is_from_new_commercial_licenses():
    shown = {p["title"]: p for p in client.get("/api/products").json()["products"]}
    office = next(p for t, p in shown.items() if t.startswith("МойОфис"))
    assert office["price_min"] is None and office["price_on_request"]  # в каталоге только обновления
    kes = next(p for t, p in shown.items() if t.startswith("Kaspersky Endpoint Security"))
    assert kes["price_min"] > 827.1  # не продление для академических учреждений


@pytest.mark.small_catalog
def test_showcase_search_keeps_product_shape():
    res = client.get("/api/products?q=лицензия&limit=3").json()
    assert all({"price_min", "price_max", "price_on_request", "product_id"} <= set(p) for p in res["products"])
