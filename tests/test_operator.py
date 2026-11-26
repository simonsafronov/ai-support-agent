"""Экран оператора и то, что добавилось по ответам заказчика: журнал, ответы менеджера как знание, обновление каталога."""
import json
import sqlite3

import pytest

from fastapi.testclient import TestClient

from app import store
from app.agent import loop, tools
from app.agent.llm import Scripted
from app.catalog import update
from app.kb import answers
from app.web import server

client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}


@pytest.fixture(autouse=True)
def operator_key(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)


def _handoff_dialog() -> tuple[str, int]:
    res = loop.run_turn(None, "Продаете ли вы Articulate 360? Почта ivanov@romashka.example", llm=Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "продукта нет в каталоге", "summary": "Нужен Articulate 360"}},
        "Передал вопрос менеджеру."]))
    return res["session_id"], store.open_handoff(res["session_id"])["id"]


def test_manager_answer_reaches_client_and_teaches_agent():
    sid, hid = _handoff_dialog()
    r = client.post(f"/api/admin/handoff/{hid}/answer", headers=KEY, json={
        "answer": "Articulate 360 не поставляем. Для разработки курсов предлагаем iSpring Suite.", "teach": True})
    assert r.status_code == 200 and r.json()["teach"]
    # клиент видит ответ в своем чате, обращение закрыто, агент снова ведет диалог сам
    last = client.get(f"/api/session/{sid}").json()["messages"][-1]
    assert last["from"] == "manager" and "iSpring" in last["content"]
    assert store.open_handoff(sid) is None and store.get_session(sid)["status"] == "bot"
    # следующий клиент спрашивает о том же: ответ менеджера приходит агенту вместе с результатом поиска
    out = tools.call(tools.ToolContext("other"), "search_products", {"query": "Articulate 360"})
    assert "iSpring" in out["manager_answers"][0]["answer"]
    assert "ivanov@" not in json.dumps(out, ensure_ascii=False)  # контакт первого клиента в знания не попал
    assert "manager_answers" not in tools.call(tools.ToolContext("other"), "search_products", {"query": "Kaspersky"})


def test_private_answer_is_not_used_for_other_clients():
    _, hid = _handoff_dialog()
    client.post(f"/api/admin/handoff/{hid}/answer", headers=KEY, json={"answer": "Вам дадим скидку 10%.", "teach": False})
    assert answers.find("Articulate 360") == [] and answers.count() == 0


def test_export_has_every_message_with_tools_and_handoff_flag():
    sid, _ = _handoff_dialog()
    text = client.get("/api/admin/export.csv", headers=KEY).content.decode("utf-8-sig")
    rows = [line.split(";") for line in text.strip().splitlines()]
    assert rows[0][:3] == ["время (МСК)", "диалог", "кто"] and len(rows) == 3
    assert rows[1][1:3] == [sid, "клиент"] and rows[2][2] == "агент"
    assert rows[2][4] == "handoff_to_manager" and rows[2][5] == "да"


def test_without_key_operator_screen_is_read_only_and_hides_contacts(monkeypatch):
    sid, hid = _handoff_dialog()
    demo = client.get(f"/api/admin/session/{sid}").json()
    assert demo["role"] == "demo" and "ivanov@" not in json.dumps(demo, ensure_ascii=False)
    assert "[email]" in demo["messages"][0]["content"]
    full = client.get(f"/api/admin/session/{sid}", headers=KEY).json()
    assert full["role"] == "operator" and "ivanov@romashka.example" in full["messages"][0]["content"]
    assert client.get("/api/admin/export.csv").status_code == 403
    assert client.post(f"/api/admin/handoff/{hid}/answer", json={"answer": "x"}).status_code == 403
    assert client.post("/api/admin/catalog/update").status_code == 403
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "")  # ключ не задан: полного доступа нет ни у кого
    assert client.get("/api/admin/export.csv", headers=KEY).status_code == 403


def test_chat_stream_limits_and_headers(monkeypatch):
    monkeypatch.setattr(server.loop, "get_llm", lambda: Scripted(["Здравствуйте. Чем помочь?"]))
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 2)
    server._recent.clear()
    r = client.post("/api/chat", json={"message": "Добрый день", "client_id": "t1"})
    last = json.loads(r.text.strip().splitlines()[-1])
    assert r.status_code == 200 and last["type"] == "reply" and last["reply"] == "Здравствуйте. Чем помочь?"
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'self'" in r.headers["content-security-policy"]
    assert client.post("/api/chat", json={"message": "еще"}).status_code == 200
    assert client.post("/api/chat", json={"message": "и еще"}).status_code == 429  # третье сообщение за минуту
    assert client.get("/quotes/../app.sqlite.pdf").status_code == 404 and client.get("/quotes/нет.pdf").status_code == 404


def _tiny_catalog(path, prices: dict):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE offers(id INTEGER PRIMARY KEY, price REAL)")
    con.executemany("INSERT INTO offers VALUES(?,?)", prices.items())
    con.commit()
    con.close()


def test_catalog_update_swaps_database_and_reports_changes(tmp_path, monkeypatch):
    db = tmp_path / "catalog.sqlite"
    _tiny_catalog(db, {1: 100, 2: 200, 3: None})
    monkeypatch.setattr(update, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(update, "download", lambda url, dest: dest.write_text("<yml/>"))

    def build(feed, new_db, verbose=True):
        _tiny_catalog(new_db, {1: 100, 2: 250, 4: 400})
        return {"products": 3, "feed_date": "2026-11-27 04:00"}

    monkeypatch.setattr(update.ingest, "build", build)
    res = update.run("https://example.test/feed.yml.gz", db)
    assert res["ok"] and (res["added"], res["removed"], res["price_changed"]) == (1, 1, 1)
    assert sqlite3.connect(db).execute("SELECT price FROM offers WHERE id=2").fetchone()[0] == 250
    assert update.last()["feed_date"] == "2026-11-27 04:00" and not (tmp_path / "feed.new.xml").exists()


def test_broken_feed_keeps_working_catalog(tmp_path, monkeypatch):
    db = tmp_path / "catalog.sqlite"
    _tiny_catalog(db, {i: 10 for i in range(10)})
    monkeypatch.setattr(update, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(update, "download", lambda url, dest: dest.write_text("<yml/>"))
    monkeypatch.setattr(update.ingest, "build", lambda feed, new_db, verbose=True: _tiny_catalog(new_db, {1: 10}) or {})
    res = update.run("https://example.test/feed.yml.gz", db)
    assert not res["ok"] and "подмена отменена" in res["error"]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 10


def test_changed_feed_markup_keeps_working_catalog(tmp_path, monkeypatch):
    """Магазин переименовал параметр: предложений столько же, но число пользователей больше не читается."""
    def catalog(path, users):
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE offers(id INTEGER PRIMARY KEY, price REAL, users_max INTEGER)")
        con.executemany("INSERT INTO offers VALUES(?,?,?)", [(i, 100, users) for i in range(600)])
        con.commit()
        con.close()

    db = tmp_path / "catalog.sqlite"
    catalog(db, 30)
    monkeypatch.setattr(update, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(update, "download", lambda url, dest: dest.write_text("<yml/>"))
    monkeypatch.setattr(update.ingest, "build", lambda feed, new_db, verbose=True: catalog(new_db, None) or {})
    res = update.run("https://example.test/feed.yml.gz", db)
    assert not res["ok"] and "разметка фида изменилась" in res["error"] and "users_max: было 600, стало 0" in res["error"]
    assert sqlite3.connect(db).execute("SELECT COUNT(users_max) FROM offers").fetchone()[0] == 600


def _tender_dialog(offer: dict) -> tuple[str, int]:
    """Клиент назвал организацию и объем, получил расчет и попросил скидку по госзакупке."""
    res = loop.run_turn(None, "Мы школа, 30 компьютеров, закупка по 44-ФЗ, нужна скидка. Почта zakupki@school.example",
                        llm=Scripted([
                            {"name": "calc_total", "args": {"items": [{"offer_id": offer["offer_id"], "qty": offer["qty"]}]}},
                            {"name": "handoff_to_manager", "args": {"reason": "госзакупка и скидка",
                                                                    "summary": "Школа, 30 лицензий, 44-ФЗ"}},
                            "Передал менеджеру.\n[[facts: company=Школа 17; users=30; org_type=образовательная]]"]))
    return res["session_id"], store.open_handoff(res["session_id"])["id"]


@pytest.fixture(scope="module")
def priced_offer():
    from app.catalog import search as catalog
    product = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=1)["products"][0]
    offer = next(o for o in catalog.get_product(product["product_id"], limit=20)["offers"] if o.get("price"))
    return {**offer, "qty": offer.get("qty_min") or 1}  # количество внутри ценовой ступени варианта


def test_handoff_card_is_built_by_code_and_sorted_by_urgency(priced_offer):
    _handoff_dialog()  # обычное обращение без срочной темы
    sid, hid = _tender_dialog(priced_offer)
    first = client.get("/api/admin/stats", headers=KEY).json()["handoffs"][0]
    card = first["card"]
    assert first["id"] == hid and card["priority"] == "high" and card["sla_minutes"] == 30
    assert {"госзакупка", "просит скидку"} <= set(card["flags"])
    assert card["client"] == {"компания": "Школа 17", "тип организации": "образовательная",
                              "контакт": "zakupki@school.example"}
    assert card["needs"] == {"пользователей": "30"} and card["calc_total"] == round(priced_offer["price"] * priced_offer["qty"], 2)
    assert card["turns"] == 1 and len(card["templates"]) == 3 and not card["overdue"]
    # без ключа карточка видна, но контакт в ней скрыт
    demo = client.get("/api/admin/stats").json()["handoffs"][0]["card"]
    assert demo["client"]["контакт"] == "[email]"


def test_view_mode_hides_requisites_but_keeps_links(priced_offer):
    res = loop.run_turn(None, "Нужно КП, ИНН 7701234567, КПП 770101001", llm=Scripted([
        {"name": "make_quote", "args": {"items": [{"offer_id": priced_offer["offer_id"], "qty": priced_offer["qty"]}],
                                        "customer": "ООО Ромашка", "inn": "7701234567", "kpp": "770101001"}},
        "Черновик готов."]))
    sid = res["session_id"]
    full = client.get(f"/api/admin/session/{sid}", headers=KEY).json()
    seen = client.get(f"/api/admin/session/{sid}").json()
    assert full["session"]["facts"]["ИНН"] == "7701234567" and "7701234567" in full["messages"][0]["content"]
    text = json.dumps(seen, ensure_ascii=False)
    assert "7701234567" not in text and "770101001" not in text and seen["session"]["facts"]["ИНН"] == "[реквизит]"
    assert seen["session"]["id"] == sid and seen["quotes"][0]["number"] == full["quotes"][0]["number"]
    assert seen["quotes"][0]["total"] == full["quotes"][0]["total"]  # суммы и время остаются числами


def test_manager_takes_and_returns_a_request(priced_offer):
    sid, hid = _tender_dialog(priced_offer)
    assert client.post(f"/api/admin/handoff/{hid}/take", json={"who": "Анна"}).status_code == 403
    assert client.post(f"/api/admin/handoff/{hid}/take", headers=KEY, json={"who": "Анна"}).json() == {"ok": True}
    card = client.get(f"/api/admin/session/{sid}", headers=KEY).json()["handoffs"][0]
    assert card["status"] == "taken" and card["card"]["assignee"] == "Анна"
    assert store.open_handoff(sid)["id"] == hid  # взятое обращение агент дополняет, а не заводит второе
    assert client.post(f"/api/admin/handoff/{hid}/return", headers=KEY).json() == {"ok": True}
    assert store.open_handoff(sid) is None and store.get_session(sid)["status"] == "bot"
    assert client.post(f"/api/admin/handoff/{hid}/return", headers=KEY).json() == {"ok": False}


def test_economy_separates_measurements_from_assumptions():
    from app import economy
    e = economy.calc(dialogs=100, handed=25, cost_total=1000.0, minutes=10, rub_hour=600, card_share=0.5, volume=500)
    assert e["measured"] == {"dialogs": 100, "closed": 75, "handed": 25, "closed_share": 0.75,
                             "cost_per_dialog_rub": 10.0}
    # стенд: 75 диалогов по 10 минут и 25 по 5 минут = 875 минут = 14,6 часа = 8 750 руб.
    assert e["stand"] == {"saved_hours": 14.6, "saved_rub": 8750, "model_cost_rub": 1000, "net_rub": 7750,
                          "return_x": 8.8}
    assert e["half_year"]["requests"] == 500 and e["half_year"]["closed"] == 375
    assert e["half_year"]["saved_rub"] == 43750 and e["half_year"]["model_cost_rub"] == 5000
    assert e["half_year"]["model_budget_rub"] == 7000 and "model_budget_rub" not in e["stand"]
    assert economy.calc(0, 0, 0.0)["measured"] is None
    stats = client.get("/api/admin/stats?minutes=20&rub_hour=900").json()["economy"]
    assert stats["assumptions"]["minutes_per_request"] == 20 and stats["assumptions"]["manager_rub_per_hour"] == 900
    assert client.get("/api/admin/stats?minutes=0").status_code == 422


def test_failed_dialog_becomes_a_regression_case(tmp_path, monkeypatch):
    from app import cases
    monkeypatch.setattr(cases, "ADDED", tmp_path / "cases_added.jsonl")
    sid, _ = _handoff_dialog()
    assert client.post(f"/api/admin/session/{sid}/case", json={"handoff": False}).status_code == 403
    r = client.post(f"/api/admin/session/{sid}/case", headers=KEY,
                    json={"handoff": False, "note": "iSpring есть в каталоге, менеджер не нужен"}).json()
    assert r["ok"] and r["added_cases"] == 1
    (case,) = cases.load()
    assert case["turns"] == ["Продаете ли вы Articulate 360? Почта [email]"]  # контакт клиента в набор не попал
    assert case["expect"]["handoff"] is False and case["expect"]["tools_any"] == ["handoff_to_manager"]
    client.post(f"/api/admin/session/{sid}/case", headers=KEY, json={"handoff": None})
    assert len(cases.load()) == 1 and cases.load()[0]["expect"]["handoff"] == "any"  # повтор заменяет кейс


def test_price_guard_demo_shows_both_outcomes():
    before = store.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    r = client.post("/api/proof").json()
    fixed, stubborn = r["runs"]
    assert fixed["caught"] and stubborn["caught"] and r["fake_price"] != r["real_price"]
    assert "цена по запросу" in stubborn["client_saw"] and fixed["caught"][0] not in stubborn["client_saw"]
    assert fixed["client_saw"] == fixed["model_second"] and fixed["caught"][0] not in fixed["client_saw"]
    assert store.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before  # показ не копит диалоги
    assert client.get("/proof").status_code == 200
