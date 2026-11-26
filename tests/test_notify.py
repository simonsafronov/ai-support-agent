"""Обращение менеджеру в Telegram и ответ менеджера оттуда в чат клиента. Telegram в тестах подменен."""
import pytest
from fastapi.testclient import TestClient

from app import notify, store
from app.agent import loop
from app.agent.llm import Scripted
from app.web import server

CHAT = "-100500"


@pytest.fixture
def tg(monkeypatch):
    """Подставной Telegram: запоминает вызовы и отвечает как настоящий."""
    calls = []

    def call(method, files=None, **params):
        calls.append((method, {**params, "has_file": bool(files)}))
        return {"message_id": 1000 + len(calls)}

    monkeypatch.setattr(notify, "call", call)
    monkeypatch.setattr(notify.config, "TG_TOKEN", "t")
    monkeypatch.setattr(notify.config, "TG_CHAT", CHAT)
    monkeypatch.setattr(notify.config, "PUBLIC_URL", "https://stand.example")
    monkeypatch.setattr(notify, "SETTLE", 0)
    monkeypatch.setattr(notify, "_answer", server.manager_answer)
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    return calls


def _handoff(client_id="visitor-1") -> tuple[str, int]:
    res = loop.run_turn(None, "Нужна скидка 15% на 30 лицензий. ООО Ромашка, почта ivanov@romashka.example",
                        client_id=client_id, llm=Scripted([
                            {"name": "handoff_to_manager", "args": {"reason": "скидка больше 5%",
                                                                    "summary": "Просит 15% на 30 лицензий"}},
                            "Передал вопрос менеджеру."]))
    return res["session_id"], store.open_handoff(res["session_id"])["id"]


def test_new_handoff_goes_to_telegram_once_with_the_card(tg):
    sid, hid = _handoff()
    assert notify.sync() == 1
    method, sent = tg[0]
    assert method == "sendMessage" and sent["chat_id"] == CHAT
    text = sent["text"]
    assert f"Обращение №{hid}" in text and "ждет ответа" in text
    assert "скидка больше 5%" in text and "Просит 15% на 30 лицензий" in text
    assert "просит скидку" in text and "ivanov@romashka.example" in text  # контакт подставил код, а не модель
    buttons = [b for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert [b.get("callback_data") for b in buttons[:2]] == [f"take:{hid}", f"back:{hid}"]
    assert buttons[2]["url"] == "https://stand.example/admin"
    assert notify.sync() == 0 and len(tg) == 1  # второй раз та же карточка не уходит


def test_run_dialogs_and_old_handoffs_are_not_sent(tg):
    _handoff(client_id="eval-c01")  # диалог прогона, а не посетителя
    _, old = _handoff()
    store.db().execute("UPDATE handoffs SET ts = ts - 3600 WHERE id=?", (old,))
    store.db().commit()
    assert notify.sync() == 0 and tg == []


def test_manager_reply_in_telegram_reaches_the_client_chat(tg):
    sid, hid = _handoff()
    notify.sync()
    card_id = 1001
    done = notify.handle({"message": {"message_id": 7, "chat": {"id": int(CHAT)}, "from": {"first_name": "Анна"},
                                      "text": "Дадим 12% при оплате до пятницы.",
                                      "reply_to_message": {"message_id": card_id}}})
    assert done == "answered"
    # клиент видит ответ в своем чате: его отдает тот же адрес, который опрашивает страница
    got = TestClient(server.app).get(f"/api/session/{sid}/answers", params={"client_id": "visitor-1"}).json()
    assert [a["content"] for a in got["answers"]] == ["Ответ менеджера: Дадим 12% при оплате до пятницы."]
    h = store.db().execute("SELECT status, assignee FROM handoffs WHERE id=?", (hid,)).fetchone()
    assert (h["status"], h["assignee"]) == ("answered", "Анна")
    assert tg[-1][0] == "sendMessage" and "отправлен клиенту" in tg[-1][1]["text"]
    # карточка в Telegram обновляется: обращение закрыто, кнопок действия больше нет
    notify.sync()
    method, edit = tg[-1]
    assert method == "editMessageText" and "ответ дан" in edit["text"]
    assert all("callback_data" not in b for row in edit["reply_markup"]["inline_keyboard"] for b in row)


def test_answers_are_only_for_the_owner_of_the_dialog(tg):
    sid, _ = _handoff()
    r = TestClient(server.app).get(f"/api/session/{sid}/answers", params={"client_id": "someone-else"})
    assert r.status_code == 404


def test_foreign_chat_cannot_answer_clients(tg):
    sid, hid = _handoff()
    notify.sync()
    done = notify.handle({"message": {"message_id": 8, "chat": {"id": 42}, "from": {"first_name": "Чужой"},
                                      "text": "Скидка 90%", "reply_to_message": {"message_id": 1001}}})
    assert done == "foreign"
    assert all(m["meta"].get("from") != "manager" for m in store.messages(sid))


def test_buttons_take_and_return_the_handoff(tg):
    sid, hid = _handoff()
    notify.sync()
    press = {"id": "q1", "from": {"first_name": "Анна", "last_name": "К."}, "message": {"chat": {"id": int(CHAT)}}}
    assert notify.handle({"callback_query": {**press, "data": f"take:{hid}"}}) == "take"
    h = store.db().execute("SELECT status, assignee FROM handoffs WHERE id=?", (hid,)).fetchone()
    assert (h["status"], h["assignee"]) == ("taken", "Анна К.")
    assert notify.handle({"callback_query": {**press, "data": f"take:{hid}"}}) == "stale"  # уже занято
    assert notify.handle({"callback_query": {**press, "data": f"back:{hid}"}}) == "back"
    assert store.get_session(sid)["status"] == "bot"


def test_message_without_reply_gets_a_hint_and_nothing_is_sent_to_clients(tg):
    sid, _ = _handoff()
    notify.sync()
    assert notify.handle({"message": {"message_id": 9, "chat": {"id": int(CHAT)}, "text": "привет"}}) == "hint"
    assert all(m["meta"].get("from") != "manager" for m in store.messages(sid))


def test_quote_file_follows_the_card(tg, monkeypatch):
    sid, hid = _handoff()
    pdf = notify.config.QUOTES_DIR / "KP-1.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4")
    store.add_quote(sid, "KP-1", "ООО Ромашка", [{"offer_id": 1, "name": "Лицензия", "price": 100.0, "qty": 30}],
                    3000.0, str(pdf))
    notify.sync()
    assert [m for m, _ in tg] == ["sendMessage", "sendDocument"]
    assert tg[1][1]["has_file"] and tg[1][1]["reply_to_message_id"] == 1001
    assert "Черновик KP-1" in tg[0][1]["text"] and "3 000,00 руб." in tg[0][1]["text"]
    notify.sync()
    assert len(tg) == 2  # тот же КП второй раз не отправляется


def test_without_settings_nothing_starts(monkeypatch):
    monkeypatch.setattr(notify.config, "TG_TOKEN", "")
    assert not notify.enabled()
    notify.start(lambda *a: None)  # потоки не запускаются, сервер работает как раньше
