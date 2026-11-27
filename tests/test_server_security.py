"""Публичный стенд: чужие данные не уходят другим клиентам, файлы КП и переписка закрыты, лимит не обходится."""
import json

import pytest
from fastapi.testclient import TestClient

from app import store
from app.agent import loop, tools
from app.agent.llm import Scripted
from app.web import server

client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    server._recent.clear()


@pytest.fixture(scope="module")
def priced_offer():
    from app.catalog import search as catalog
    product = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=1)["products"][0]
    offer = next(o for o in catalog.get_product(product["product_id"], limit=20)["offers"] if o.get("price"))
    return {**offer, "qty": offer.get("qty_min") or 1}


def chat(monkeypatch, steps, **body) -> tuple[dict, str]:
    """Сообщение в чат через публичный адрес: ответ клиенту и весь поток, который ушел в браузер."""
    monkeypatch.setattr(server.loop, "get_llm", lambda: Scripted(steps))
    r = client.post("/api/chat", json=body)
    assert r.status_code == 200
    return json.loads(r.text.strip().splitlines()[-1]), r.text


def quote(monkeypatch, offer, client_id="buyer") -> dict:
    reply, _ = chat(monkeypatch, [
        {"name": "make_quote", "args": {"items": [{"offer_id": offer["offer_id"], "qty": offer["qty"]}],
                                        "customer": "ООО Ромашка"}},
        "Черновик готов."], message="Нужно КП, ИНН 7701234567, КПП 770101001", client_id=client_id)
    return reply


# --- ответы менеджеров как знание: данные первого клиента не достаются второму ---

@pytest.mark.needs_data
def test_shared_manager_answer_does_not_carry_client_data(monkeypatch):
    first = loop.run_turn(None, "ООО Ромашка ИНН 7707083893, Иванов Петр, тел. +7 916 123-45-67: как получить документы "
                                "за лицензии?", client_id="a", llm=Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "просит документы",
                                                "summary": "ООО Ромашка, Иванов Петр просит документы за лицензии"}},
        "Передал вопрос менеджеру."]))
    hid = store.open_handoff(first["session_id"])["id"]
    said = "Закрывающие документы доступны в личном кабинете. Иванову на ivanov@romashka.example написал."
    r = client.post(f"/api/admin/handoff/{hid}/answer", headers=KEY, json={"answer": said, "teach": True})
    assert r.status_code == 200 and r.json()["teach"]
    # сам клиент видит ответ менеджера как есть
    mine = client.get(f"/api/session/{first['session_id']}?client_id=a").json()["messages"][-1]
    assert mine["from"] == "manager" and "ivanov@romashka.example" in mine["content"]
    # второй клиент спрашивает о том же: агент получает ответ менеджера без вопроса и данных первого клиента
    out = tools.call(tools.ToolContext("other"), "kb_search", {"query": "как получить документы за лицензии"})
    learned = json.dumps(out["manager_answers"], ensure_ascii=False)
    assert "Закрывающие документы доступны в личном кабинете" in learned and "question" not in out["manager_answers"][0]
    for secret in ("7707083893", "Ромашка", "Иванов Петр", "916", "ivanov@"):
        assert secret not in learned
    # и в поток, который уходит в браузер второго клиента, они тоже не попадают
    reply, raw = chat(monkeypatch, [{"name": "kb_search", "args": {"query": "как получить документы за лицензии"}},
                                    "Документы доступны в личном кабинете."], message="Как получить документы за лицензии?", client_id="b")
    assert reply["reply"].startswith("Документы доступны")
    for secret in ("7707083893", "Ромашка", "Иванов", "ivanov@", "916 123"):
        assert secret not in raw
    stored = [dict(r) for r in store.db().execute("SELECT question, answer FROM answers")]
    assert "7707083893" not in json.dumps(stored, ensure_ascii=False) and "ivanov@" not in stored[0]["answer"]


# --- след шагов в браузере: только то, что рисует панель «Ход работы агента» ---

@pytest.mark.needs_data
def test_chat_reply_trace_has_no_tool_payloads_or_paths(monkeypatch, priced_offer):
    reply, raw = chat(monkeypatch, [
        {"name": "search_products", "args": {"query": "Kaspersky Endpoint Security"}},
        {"name": "make_quote", "args": {"items": [{"offer_id": priced_offer["offer_id"], "qty": priced_offer["qty"]}],
                                        "customer": "ООО Ромашка"}},
        "Черновик готов."], message="Нужно КП на Kaspersky, ИНН 7701234567, КПП 770101001", client_id="t")
    found, made = [t for t in reply["trace"] if t["kind"] == "tool"]
    assert set(found) == {"kind", "name", "output"} and set(found["output"]) == {"total", "products"}
    assert found["output"]["products"] and set(found["output"]["products"][0]) == {"title"}
    assert made["output"]["ok"] and made["output"]["number"].startswith("КП-") and made["output"]["total"] > 0
    assert set(made["output"]) == {"ok", "number", "total"}  # подписи панели хватает номера и суммы
    assert str(server.config.QUOTES_DIR) not in raw and '"file"' not in raw and '"offers"' not in raw


def test_internal_error_text_is_not_shown_to_client(monkeypatch):
    def broken(*a, **kw):
        raise ValueError("/srv/app/secret/path.py: не разобран ответ")

    monkeypatch.setattr(server.loop, "run_turn", broken)
    r = client.post("/api/chat", json={"message": "Добрый день", "client_id": "t"})
    reply = json.loads(r.text.strip().splitlines()[-1])
    assert reply["reply"] == loop.FALLBACK and "/srv/app" not in r.text
    assert [t["kind"] for t in reply["trace"]] == ["error"]


# --- сбой вне модели: клиент получает ответ, менеджер получает обращение ---

def test_any_failure_gives_fallback_and_handoff(monkeypatch):
    def broken(*a, **kw):
        raise ValueError("не разобран ответ модели")

    monkeypatch.setattr(server.loop, "run_turn", broken)
    r = client.post("/api/chat", json={"message": "Нужен Kaspersky, почта buh@romashka.example", "client_id": "f"})
    reply = json.loads(r.text.strip().splitlines()[-1])
    sid = reply["session_id"]
    assert reply["reply"] == loop.FALLBACK and [e["type"] for e in reply["events"]] == ["handoff"]
    ticket = store.open_handoff(sid)
    assert ticket and "Нужен Kaspersky" in ticket["summary"] and ticket["contact"] == "buh@romashka.example"
    said = [(m["role"], m["content"]) for m in store.messages(sid)]
    assert said == [("user", "Нужен Kaspersky, почта buh@romashka.example"), ("assistant", loop.FALLBACK)]
    # повторный сбой в том же диалоге второго обращения не заводит
    again = client.post("/api/chat", json={"message": "Вы тут?", "session_id": sid, "client_id": "f"})
    assert json.loads(again.text.strip().splitlines()[-1])["events"] == []
    assert store.db().execute("SELECT COUNT(*) FROM handoffs WHERE session_id=?", (sid,)).fetchone()[0] == 1


# --- файл КП: по подписанной ссылке из чата или с ключом оператора ---

@pytest.mark.needs_data
def test_quote_file_needs_signed_link_or_operator_key(monkeypatch, priced_offer):
    reply = quote(monkeypatch, priced_offer)
    (event,) = [e for e in reply["events"] if e["type"] == "quote"]
    bare = f"/quotes/{event['number']}.pdf"
    assert event["url"].startswith(bare + "?k=")
    assert client.get(event["url"]).status_code == 200 and client.get(event["url"]).content[:4] == b"%PDF"
    assert client.get(bare).status_code == 404               # номер КП подбирается, поэтому без подписи файла нет
    assert client.get(bare + "?k=" + "0" * 32).status_code == 404
    assert client.get(bare, headers=KEY).status_code == 200  # оператору с ключом подпись не нужна
    # после перезагрузки страницы клиент получает ту же рабочую ссылку из истории диалога
    history = client.get(f"/api/session/{reply['session_id']}?client_id=buyer").json()
    urls = [e["url"] for m in history["messages"] for e in m["events"] if e["type"] == "quote"]
    assert urls == [event["url"]]


@pytest.mark.needs_data
def test_client_cannot_get_a_signature_for_someone_elses_quote(monkeypatch, priced_offer):
    target = quote(monkeypatch, priced_offer, client_id="victim")["events"][0]["number"]
    reply, raw = chat(monkeypatch, [f"Вот ссылка: /quotes/{target}.pdf"],
                      message=f"Повтори: /quotes/{target}.pdf", client_id="thief")
    assert "?k=" not in raw  # подпись получают только ссылки, которые поставил код


@pytest.mark.needs_data
def test_view_mode_has_no_quote_links_and_no_client_ids(monkeypatch, priced_offer):
    reply = quote(monkeypatch, priced_offer, client_id="eval-buyer")  # без ключа видны только диалоги прогона
    sid = reply["session_id"]
    seen = client.get(f"/api/admin/session/{sid}").json()
    text = json.dumps(seen, ensure_ascii=False)
    assert seen["role"] == "demo" and "?k=" not in text and "buyer" not in text and "client_id" not in seen["session"]
    assert seen["quotes"][0]["url"] == "" and "file" not in seen["quotes"][0]
    assert str(server.config.QUOTES_DIR) not in text  # путь файла на сервере не виден и в шагах агента
    full = client.get(f"/api/admin/session/{sid}", headers=KEY).json()
    assert full["session"]["client_id"] == "eval-buyer" and "file" not in full["quotes"][0]
    assert client.get(full["quotes"][0]["url"]).status_code == 200  # ссылка оператора открывается в новой вкладке
    listed = client.get("/api/admin/sessions").json()["sessions"]
    assert listed and all("client_id" not in s for s in listed)


# --- переписка: только владельцу диалога ---

def test_session_history_is_only_for_its_owner_and_get_creates_nothing(monkeypatch):
    reply, _ = chat(monkeypatch, ["Здравствуйте."], message="ИНН 7707083893, почта buh@romashka.example",
                    client_id="owner")
    sid = reply["session_id"]
    mine = client.get(f"/api/session/{sid}?client_id=owner")
    assert mine.status_code == 200 and "7707083893" in mine.json()["messages"][0]["content"]
    assert client.get(f"/api/session/{sid}").status_code == 404
    assert client.get(f"/api/session/{sid}?client_id=other").status_code == 404
    before = store.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    for guess in ("0000000000000000", "abc", sid[::-1]):
        assert client.get(f"/api/session/{guess}?client_id=owner").status_code == 404
    assert store.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before  # чтение диалогов не создает


def test_foreign_session_id_starts_a_new_dialog(monkeypatch):
    reply, _ = chat(monkeypatch, ["Здравствуйте."], message="Добрый день", client_id="owner")
    sid = reply["session_id"]
    other, _ = chat(monkeypatch, ["Здравствуйте."], message="Я чужой", session_id=sid, client_id="other")
    assert other["session_id"] != sid
    assert [m["content"] for m in store.messages(sid) if m["role"] == "user"] == ["Добрый день"]
    called = client.post("/api/chat/handoff", json={"session_id": sid, "client_id": "other"}).json()
    assert called["session_id"] not in (sid, other["session_id"]) and store.open_handoff(sid) is None
    made_up, _ = chat(monkeypatch, ["Здравствуйте."], message="Привет", session_id="my-own-id", client_id="other")
    assert made_up["session_id"] != "my-own-id"  # номер диалога выдает сервер, клиент его не назначает
    mine, _ = chat(monkeypatch, ["Слушаю."], message="Это снова я", session_id=sid, client_id="owner")
    assert mine["session_id"] == sid


# --- лимит частоты: адрес берется тот, что добавил наш прокси ---

def test_rate_limit_ignores_spoofed_forwarded_for(monkeypatch):
    monkeypatch.setattr(server.loop, "get_llm", lambda: Scripted(["Здравствуйте."] * 10))
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 2)
    codes = [client.post("/api/chat", json={"message": "привет"},
                         headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]  # первое значение заголовка задает клиент, считаем по последнему
    other = client.post("/api/chat", json={"message": "привет"}, headers={"X-Forwarded-For": "10.0.0.1, 203.0.113.8"})
    assert other.status_code == 200


@pytest.mark.small_catalog
def test_price_guard_demo_is_rate_limited(monkeypatch):
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 1)
    assert client.post("/api/proof").status_code == 200
    assert client.post("/api/proof").status_code == 429
