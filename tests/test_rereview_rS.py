"""Повторное ревью: режим просмотра без рабочих диалогов, две доли без менеджера, срок хода вместе с очередью к модели."""
import pytest
from fastapi.testclient import TestClient

from app import store
from app.web import server

client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    server._recent.clear()


# --- режим просмотра: только диалоги прогона и показа ---

# в свободном тексте посетителя имя, компания и бюджет без меток, которые узнает маска
VISITOR_TEXT = "Пишет Мария Кузнецова, ООО Ромашка-Север, бюджет 480 000 руб., закупка до конца квартала"


def _dialog(client_id: str, text: str, tokens: tuple[int, int], handed: bool = False, quote: bool = False) -> str:
    sid = store.get_session(None, client_id)["id"]
    store.add_message(sid, "user", text)
    store.add_message(sid, "assistant", "Подберу варианты.")
    store.add_usage(sid, *tokens, 1, 0)
    store.add_step(sid, 1, "llm", "m", {}, {"text": "Подберу варианты."}, *tokens, 2.0)
    if handed:
        store.handoff(sid, "просит скидку", text, "")
    if quote:
        store.add_quote(sid, f"KP-{sid}", "ООО Тест", [], 1000.0, "")
    return sid


@pytest.fixture
def stand_dialogs() -> dict[str, str]:
    """Диалог проверочного прогона с обращением и КП, диалог показа и рабочий диалог посетителя с обращением."""
    return {"eval": _dialog("eval-c01", "Нужен Kaspersky на 10 ПК, почта zakup@test.example", (1000, 100),
                            handed=True, quote=True),
            "demo": _dialog("demo", "Сколько стоит Astra Linux?", (500, 50)),
            "visitor": _dialog("w3-rS-visitor", VISITOR_TEXT, (7000, 700), handed=True, quote=True)}


def test_view_mode_lists_only_demo_dialogs(stand_dialogs):
    r = client.get("/api/admin/sessions")
    assert r.json()["role"] == "demo"
    assert {s["id"] for s in r.json()["sessions"]} == {stand_dialogs["eval"], stand_dialogs["demo"]}
    assert "Ромашка-Север" not in r.text
    full = client.get("/api/admin/sessions", headers=KEY).json()["sessions"]
    assert {s["id"] for s in full} == set(stand_dialogs.values())  # с ключом все как раньше


def test_view_mode_answers_visitor_dialog_like_a_missing_one(stand_dialogs):
    visitor = client.get(f"/api/admin/session/{stand_dialogs['visitor']}")
    missing = client.get("/api/admin/session/0000000000000000")
    assert visitor.status_code == missing.status_code == 404 and visitor.json() == missing.json()
    demo = client.get(f"/api/admin/session/{stand_dialogs['eval']}").json()
    assert demo["role"] == "demo" and demo["messages"][0]["content"] == "Нужен Kaspersky на 10 ПК, почта [email]"
    full = client.get(f"/api/admin/session/{stand_dialogs['visitor']}", headers=KEY).json()
    assert full["role"] == "operator" and full["messages"][0]["content"] == VISITOR_TEXT


def test_view_mode_counts_queue_and_economy_by_demo_dialogs(stand_dialogs):
    r = client.get("/api/admin/stats")
    st = r.json()
    assert "Ромашка-Север" not in r.text
    assert (st["dialogs"], st["turns"], st["handoff_dialogs"], st["quotes"]) == (2, 2, 1, 1)
    assert (st["prompt_tokens"], st["completion_tokens"]) == (1500, 150)
    assert [h["session_id"] for h in st["handoffs"]] == [stand_dialogs["eval"]] and st["handoffs"][0]["from_run"]
    assert st["queue"] == {"waiting": 0, "from_run": 1, "overdue": 0}
    assert st["economy"]["measured"]["dialogs"] == 2 and st["economy"]["measured"]["handed"] == 1
    full = client.get("/api/admin/stats", headers=KEY).json()
    assert (full["dialogs"], full["turns"], full["handoff_dialogs"], full["quotes"]) == (3, 3, 2, 2)
    assert full["prompt_tokens"] == 8500 and full["queue"] == {"waiting": 1, "from_run": 1, "overdue": 0}
    assert {h["session_id"] for h in full["handoffs"]} == {stand_dialogs["eval"], stand_dialogs["visitor"]}


# Все маршруты экрана оператора и переписки клиента: что каждый отдает без ключа оператора
ROUTES = {
    ("GET", "/api/admin/sessions"): 200,                          # только диалоги прогона и показа
    ("GET", "/api/admin/session/{session_id}"): 404,              # рабочий диалог как несуществующий
    ("GET", "/api/admin/stats"): 200,                             # показатели по диалогам прогона и показа
    ("GET", "/api/admin/catalog"): 200,                           # состояние каталога, диалогов в нем нет
    ("GET", "/api/admin/export.csv"): 403,
    ("POST", "/api/admin/catalog/update"): 403,
    ("POST", "/api/admin/handoff/{handoff_id}/take"): 403,
    ("POST", "/api/admin/handoff/{handoff_id}/return"): 403,
    ("POST", "/api/admin/handoff/{handoff_id}/answer"): 403,
    ("POST", "/api/admin/session/{session_id}/case"): 403,
    ("GET", "/api/session/{session_id}"): 404,                    # переписка только владельцу по его client_id
    ("GET", "/api/session/{session_id}/answers"): 404,
}


def test_every_operator_and_session_route_is_checked_without_key(stand_dialogs):
    found = {(m, r.path) for r in server.app.routes
             if getattr(r, "path", "").startswith(("/api/admin/", "/api/session/")) for m in r.methods}
    assert found == set(ROUTES)  # новый маршрут сначала попадает в этот список
    sid, hid = stand_dialogs["visitor"], store.open_handoff(stand_dialogs["visitor"])["id"]
    bodies = {"answer": {"answer": "Ответ"}, "case": {"handoff": False}, "take": {"who": "Анна"}}
    for (method, path), status in ROUTES.items():
        url = path.format(session_id=sid, handoff_id=hid)
        r = client.request(method, url, json=bodies.get(url.rsplit("/", 1)[1], {}))
        assert r.status_code == status, (method, path)
        assert "Ромашка-Север" not in r.text and "w3-rS-visitor" not in r.text, (method, path)
    assert store.open_handoff(sid)["id"] == hid  # без ключа обращение не тронуто
    assert set(client.get("/api/admin/catalog").json()) == {"offers", "products", "feed_date", "feed_url",
                                                            "update_hour_msk", "updating", "last_update"}


def test_client_chat_still_reads_its_own_dialog(stand_dialogs):
    sid = stand_dialogs["visitor"]
    own = client.get(f"/api/session/{sid}", params={"client_id": "w3-rS-visitor"})
    assert own.status_code == 200 and own.json()["messages"][0]["content"] == VISITOR_TEXT
    answers = client.get(f"/api/session/{sid}/answers", params={"client_id": "w3-rS-visitor"})
    assert answers.status_code == 200 and answers.json() == {"answers": []}
    assert client.get(f"/api/session/{sid}", params={"client_id": "demo"}).status_code == 404
