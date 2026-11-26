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
