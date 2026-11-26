"""Режим "ответ проверяет менеджер": ход агента идет как обычно, а ответ клиенту ждет проверки черновиком.

Модель отвечает по сценарию, каталог синтетический, Telegram подменен (notify.call).
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import notify, store
from app.agent import handoff_card, loop
from app.agent.llm import Scripted
from app.catalog import search as catalog
from app.web import server

pytestmark = pytest.mark.small_catalog
client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}
CHAT = "-100500"
VISITOR = "r3-visitor"


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    server._recent.clear()


@pytest.fixture
def tg(monkeypatch):
    """Подставной Telegram: запоминает вызовы и отвечает как настоящий."""
    calls = []

    def call(method, files=None, **params):
        calls.append((method, params))
        return {"message_id": 1000 + len(calls)}

    monkeypatch.setattr(notify, "call", call)
    monkeypatch.setattr(notify.config, "TG_TOKEN", "t")
    monkeypatch.setattr(notify.config, "TG_CHAT", CHAT)
    monkeypatch.setattr(notify.config, "PUBLIC_URL", "https://stand.example")
    monkeypatch.setattr(notify, "_answer", server.manager_answer)
    monkeypatch.setattr(notify, "_review", server.review_draft)
    monkeypatch.setattr(notify, "_retry_at", {})
    return calls


def _review(on: bool):
    assert client.post("/api/admin/review", headers=KEY, json={"on": on}).json() == {"on": on}


def _turn(monkeypatch, message: str, steps: list, session_id: str | None = None,
          client_id: str = VISITOR) -> tuple[list[dict], Scripted]:
    model = Scripted(steps)
    monkeypatch.setattr(server.loop, "get_llm", lambda: model)
    r = client.post("/api/chat", json={"message": message, "client_id": client_id, "session_id": session_id})
    return [json.loads(line) for line in r.text.strip().splitlines()], model


def _quote_steps() -> list:
    """Ход с вариантом лицензии, черновиком КП и кнопками выбора: все, что ход строит для клиента."""
    product = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=1)["products"][0]
    offer = next(o for o in catalog.get_product(product["product_id"], limit=20)["offers"] if o.get("price"))
    item = {"offer_id": offer["offer_id"], "qty": offer.get("qty_min") or 1}
    return [{"name": "get_product", "args": {"product_id": product["product_id"]}},
            {"name": "make_quote", "args": {"items": [item], "customer": "ООО Ромашка"}},
            f"Подобрал вариант [[offer:{offer['offer_id']}]] и подготовил черновик КП. "
            "[[choices: Оформить заказ | Другой срок]]"]


def _answers(sid: str) -> list[dict]:
    return client.get(f"/api/session/{sid}/answers", params={"client_id": VISITOR}).json()["answers"]


def _history(sid: str) -> dict:
    return client.get(f"/api/session/{sid}", params={"client_id": VISITOR}).json()


def _stats(**kw) -> dict:
    return client.get("/api/admin/stats", **kw).json()["review"]


def _decide(draft_id: int, action: str, answer: str = "", headers: dict = KEY):
    return client.post(f"/api/admin/draft/{draft_id}", json={"action": action, "answer": answer}, headers=headers)


def test_mode_is_off_by_default_and_the_turn_goes_as_before(monkeypatch):
    assert _stats(headers=KEY)["on"] is False
    lines, _ = _turn(monkeypatch, "Добрый день", ["Здравствуйте. Чем помочь?"])
    reply = lines[-1]
    assert reply["reply"] == "Здравствуйте. Чем помочь?" and "review" not in reply
    history = _history(reply["session_id"])
    assert history["messages"][-1]["content"] == "Здравствуйте. Чем помочь?" and "review" not in history
    assert "reviewed" not in history["messages"][-1]
    assert store.drafts() == [] and _stats(headers=KEY)["drafts"] == []


def test_only_the_operator_switches_the_mode_and_decides_drafts(monkeypatch):
    assert client.post("/api/admin/review", json={"on": True}).status_code == 403
    assert client.post("/api/admin/review", headers={"X-Admin-Token": "wrong"}, json={"on": True}).status_code == 403
    assert _stats()["on"] is False  # без ключа переключатель виден, но не нажимается
    _review(True)
    assert _stats()["on"] is True and store.setting(server.REVIEW) == "1"  # режим хранится в базе приложения
    lines, _ = _turn(monkeypatch, "Сколько стоит Kaspersky?", ["Вариантов несколько, уточните число ПК."])
    (draft,) = _stats(headers=KEY)["drafts"]
    assert _stats()["drafts"] == []  # черновик рабочего диалога без ключа не виден, как и сам диалог
    for action in ("send", "edit", "drop"):
        assert _decide(draft["id"], action, "текст", headers={}).status_code == 403
    assert _answers(lines[-1]["session_id"]) == [] and store.draft(draft["id"])["status"] == "new"
    assert _decide(draft["id"], "edit", "   ").status_code == 422  # пустая правка клиенту не уходит
    assert _decide(10**9, "send").status_code == 404


def test_client_sees_steps_and_review_time_but_not_the_text(monkeypatch):
    _review(True)
    lines, _ = _turn(monkeypatch, "Нужен Kaspersky на 1 год", _quote_steps())
    reply = lines[-1]
    assert [e["name"] for e in lines if e["type"] == "tool"] == ["get_product", "make_quote"]  # шаги видны сразу
    minutes = handoff_card.SLA_MINUTES[handoff_card.priority("Нужен Kaspersky на 1 год")]
    assert reply["reply"] == "" and reply["cards"] == reply["choices"] == reply["events"] == []
    assert reply["review"] == server.review_note(minutes) and reply["review"]["minutes"] == minutes
    assert reply["review"]["text"] == f"Ответ проверяет менеджер, обычно это занимает до {minutes} минут."
    assert [t["name"] for t in reply["trace"]] == ["get_product", "make_quote"]
    assert "Подобрал" not in json.dumps(lines, ensure_ascii=False)
    # после перезагрузки страницы чат видит ожидание проверки и шаги хода, но не текст ответа
    sid = reply["session_id"]
    history = _history(sid)
    assert [m["role"] for m in history["messages"]] == ["user"]
    assert {k: history["review"][k] for k in ("minutes", "text")} == reply["review"]
    assert [t["name"] for t in history["review"]["trace"]] == ["get_product", "make_quote"]
    assert _answers(sid) == []
    # черновик хранит все, что ход построил: текст, карточку, кнопки выбора и КП
    (draft,) = _stats(headers=KEY)["drafts"]
    assert draft["content"].startswith("Подобрал вариант") and draft["asked"] == "Нужен Kaspersky на 1 год"
    assert [c["type"] for c in draft["cards"]] == ["offer"] and draft["choices"] == ["Оформить заказ", "Другой срок"]
    (quote,) = [e for e in draft["events"] if e["type"] == "quote"]
    assert quote["url"].startswith(f"/quotes/{quote['number']}.pdf?k=")  # ссылка на КП для оператора подписана
    assert draft["due"] == pytest.approx(draft["ts"] + minutes * 60)


def test_send_delivers_exactly_the_draft_once(monkeypatch):
    _review(True)
    lines, _ = _turn(monkeypatch, "Нужен Kaspersky на 1 год", _quote_steps())
    sid = lines[-1]["session_id"]
    raw = store.pending_draft(sid)
    assert _decide(raw["id"], "send").json() == {"ok": True, "status": "sent"}
    (got,) = _answers(sid)
    assert got["reviewed"] is True and got["content"] == raw["content"]
    assert got["cards"] == raw["meta"]["cards"] and got["choices"] == raw["meta"]["choices"]
    (quote,) = got["events"]
    assert quote["number"] == raw["meta"]["events"][0]["number"] and "?k=" in quote["url"]
    assert client.get(quote["url"]).status_code == 200  # клиент открывает КП по ссылке из ответа
    # повтор нажатия второго ответа не дает
    assert _decide(raw["id"], "send").json() == {"ok": False, "status": "sent"}
    assert _decide(raw["id"], "drop").json() == {"ok": False, "status": "sent"}
    assert len(_answers(sid)) == 1 and store.open_handoff(sid) is None
    # после перезагрузки страницы это обычный ответ агента с шагами хода и пометкой о проверке
    history = _history(sid)
    last = history["messages"][-1]
    assert "review" not in history and last["reviewed"] is True and last["content"] == raw["content"]
    assert [t["name"] for t in last["trace"]] == ["get_product", "make_quote"]
    stats = _stats(headers=KEY)
    assert (stats["sent"], stats["edited"], stats["dropped"], stats["clean_share"]) == (1, 0, 0, 1.0)
    assert stats["drafts"] == []


def test_edit_delivers_the_manager_text_instead_of_the_draft(monkeypatch):
    _review(True)
    lines, _ = _turn(monkeypatch, "Сколько стоит Kaspersky?", ["Цена от 100 000 руб."])
    sid = lines[-1]["session_id"]
    draft = store.pending_draft(sid)
    assert _decide(draft["id"], "edit", "Kaspersky на 5 ПК стоит 20 990 руб. в год.").json()["ok"] is True
    assert [a["content"] for a in _answers(sid)] == ["Ответ менеджера: Kaspersky на 5 ПК стоит 20 990 руб. в год."]
    assert "reviewed" not in _answers(sid)[0]
    h = store.db().execute("SELECT reason, status FROM handoffs WHERE session_id=?", (sid,)).fetchone()
    assert (h["reason"], h["status"]) == ("менеджер поправил ответ агента", "answered")
    assert _decide(draft["id"], "edit", "Еще раз").json() == {"ok": False, "status": "edited"}
    assert len(_answers(sid)) == 1
    stats = _stats(headers=KEY)
    assert (stats["sent"], stats["edited"], stats["dropped"], stats["clean_share"]) == (0, 1, 0, 0.0)


def test_drop_gives_the_client_nothing_and_leaves_the_request_with_the_manager(monkeypatch):
    _review(True)
    lines, _ = _turn(monkeypatch, "Нужен Kaspersky на 1 год", _quote_steps())
    sid = lines[-1]["session_id"]
    draft = store.pending_draft(sid)
    assert _decide(draft["id"], "drop").json() == {"ok": True, "status": "dropped"}
    assert _answers(sid) == [] and [m["role"] for m in _history(sid)["messages"]] == ["user"]
    assert "review" not in _history(sid)
    ticket = store.open_handoff(sid)
    assert ticket["reason"] == "менеджер не отправил ответ агента" and store.get_session(sid)["status"] == "handoff"
    assert _decide(draft["id"], "send").json() == {"ok": False, "status": "dropped"}
    assert _answers(sid) == []
    # менеджер отвечает сам по обращению: клиент получает его ответ
    client.post(f"/api/admin/handoff/{ticket['id']}/answer", headers=KEY, json={"answer": "Пришлю КП на почту."})
    assert [a["content"] for a in _answers(sid)] == ["Ответ менеджера: Пришлю КП на почту."]
    stats = _stats(headers=KEY)
    assert (stats["sent"], stats["edited"], stats["dropped"], stats["clean_share"]) == (0, 0, 1, 0.0)


def test_next_message_goes_to_the_agent_and_the_waiting_draft_goes_stale(monkeypatch):
    _review(True)
    first, _ = _turn(monkeypatch, "Сколько стоит Kaspersky?", ["Секретный черновик про Kaspersky."])
    sid = first[-1]["session_id"]
    old = store.pending_draft(sid)
    second, model = _turn(monkeypatch, "А Dr.Web?", ["Dr.Web тоже есть."], session_id=sid)
    assert second[-1]["review"] and store.draft(old["id"])["status"] == "stale"
    # модель видит только то, что клиент получил: неотправленного черновика в ее истории нет
    assert "Секретный черновик" not in json.dumps(model.calls, ensure_ascii=False)
    assert _decide(old["id"], "send").json() == {"ok": False, "status": "stale"}
    (waiting,) = _stats(headers=KEY)["drafts"]
    assert waiting["content"] == "Dr.Web тоже есть." and waiting["asked"] == "А Dr.Web?"
    assert _decide(waiting["id"], "send").json()["ok"] is True
    assert [a["content"] for a in _answers(sid)] == ["Dr.Web тоже есть."]
    stats = _stats(headers=KEY)
    assert (stats["sent"], stats["stale"], stats["clean_share"]) == (1, 1, 1.0)
    # режим выключен: следующий ответ клиент получает сразу, как раньше
    _review(False)
    third, _ = _turn(monkeypatch, "Спасибо", ["Пожалуйста."], session_id=sid)
    assert third[-1]["reply"] == "Пожалуйста." and "review" not in third[-1]


def test_review_note_names_minutes_in_russian():
    assert [server.review_note(n)["text"].rsplit(" ", 2)[1:] for n in (1, 2, 5, 11, 21)] == [
        ["1", "минуты."], ["2", "минут."], ["5", "минут."], ["11", "минут."], ["21", "минуты."]]


def test_other_channels_get_the_checked_answer_through_one_delivery_point(monkeypatch):
    """Канал, который не читает переписку сам (например Webim), получает проверенный ответ функцией по началу
    client_id; чат сайта забирает его опросом, сбой канала решение менеджера не отменяет."""
    got = []
    monkeypatch.setitem(server.DELIVERY, "chan-", lambda sid, result: got.append((sid, result)))
    _review(True)
    lines, _ = _turn(monkeypatch, "Нужен Kaspersky на 1 год", _quote_steps(), client_id="chan-7")
    sid = lines[-1]["session_id"]
    assert lines[-1]["review"]["text"].startswith("Ответ проверяет менеджер") and got == []
    draft = store.pending_draft(sid)
    assert _decide(draft["id"], "send").json()["ok"] is True
    ((to, result),) = got
    assert to == sid and result["reviewed"] is True and result["reply"] == draft["content"]
    assert result["cards"] == draft["meta"]["cards"] and result["choices"] == draft["meta"]["choices"]
    (quote,) = result["events"]
    assert quote["url"] == f"/quotes/{quote['number']}.pdf"  # ссылку канал подписывает сам, как результат хода
    _decide(draft["id"], "send")
    assert len(got) == 1  # повтор нажатия второй доставки не дает

    _turn(monkeypatch, "А на 2 года?", ["Черновик про 2 года."], session_id=sid, client_id="chan-7")
    assert _decide(store.pending_draft(sid)["id"], "edit", "Пришлю расчет на 2 года.").json()["ok"] is True
    assert got[-1][1]["reply"] == "Пришлю расчет на 2 года." and got[-1][1]["from"] == "manager"
    _turn(monkeypatch, "А на 3 года?", ["Черновик про 3 года."], session_id=sid, client_id="chan-7")
    _decide(store.pending_draft(sid)["id"], "drop")
    assert len(got) == 2  # снятый черновик клиенту не уходит ни в каком канале

    def broken(sid, result):
        raise RuntimeError("канал недоступен")
    monkeypatch.setitem(server.DELIVERY, "chan-", broken)
    _turn(monkeypatch, "Спасибо", ["Пожалуйста."], session_id=sid, client_id="chan-7")
    assert _decide(store.pending_draft(sid)["id"], "send").json() == {"ok": True, "status": "sent"}
    # диалог чата сайта в чужой канал не уходит
    lines, _ = _turn(monkeypatch, "Добрый день", ["Здравствуйте."])
    _decide(store.pending_draft(lines[-1]["session_id"])["id"], "send")
    assert len(got) == 2


def test_run_and_price_proof_go_past_the_mode_and_purge_removes_drafts(monkeypatch):
    _review(True)
    res = loop.run_turn(None, "Добрый день", client_id="eval-c01", llm=Scripted(["Здравствуйте."]))
    assert res["reply"] == "Здравствуйте." and store.drafts() == []  # проверочный прогон идет мимо сервера
    lines, _ = _turn(monkeypatch, "Добрый день", ["Здравствуйте."])
    sid = lines[-1]["session_id"]
    assert len(store.drafts()) == 1
    store.drop_session(sid)
    assert store.drafts() == []


def test_telegram_buttons_and_reply_decide_the_draft(monkeypatch, tg):
    _review(True)
    lines, _ = _turn(monkeypatch, "Нужен Kaspersky на 1 год", _quote_steps())
    sid = lines[-1]["session_id"]
    draft = store.pending_draft(sid)
    assert notify.sync() == 1
    method, sent = tg[-1]
    assert method == "sendMessage" and sent["chat_id"] == CHAT
    assert f"Черновик ответа агента №{draft['id']}" in sent["text"] and "Подобрал вариант" in sent["text"]
    assert "Нужен Kaspersky на 1 год" in sent["text"]
    assert f"Черновик {draft['meta']['events'][0]['number']}:" in sent["text"]
    buttons = [b for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert [(b["text"], b.get("callback_data")) for b in buttons[:2]] == [
        ("Отправить", f"send:{draft['id']}"), ("Не отправлять", f"drop:{draft['id']}")]
    assert notify.sync() == 0  # второй раз тот же черновик не уходит
    message_id = 1000 + len(tg)

    press = {"callback_query": {"id": "cb1", "data": f"send:{draft['id']}", "from": {"first_name": "Анна"},
                                "message": {"chat": {"id": int(CHAT)}, "message_id": message_id}}}
    assert notify.handle(press) == "send"
    assert tg[-1] == ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Ответ отправлен клиенту"})
    assert [a["content"] for a in _answers(sid)] == [draft["content"]]
    assert notify.handle(press) == "stale" and len(_answers(sid)) == 1  # повтор кнопки не дублирует ответ
    assert "отправлен клиенту без правок" in tg[-1][1]["text"]
    # сообщение в Telegram показывает итог и теряет кнопки решения
    assert notify.sync() == 1
    method, edit = tg[-1]
    assert method == "editMessageText" and edit["message_id"] == message_id
    assert "отправлен клиенту без правок" in edit["text"]
    assert all(b.get("callback_data") is None for row in edit["reply_markup"]["inline_keyboard"] for b in row)

    # реплай на черновик: клиент получает текст менеджера вместо черновика
    _turn(monkeypatch, "А на 2 года?", ["Черновик про 2 года."], session_id=sid)
    second = store.pending_draft(sid)
    notify.sync()
    reply_to = 1000 + len(tg)
    said = {"message": {"message_id": 7, "chat": {"id": int(CHAT)}, "from": {"first_name": "Анна"},
                        "text": "На 2 года пришлю расчет в течение часа.",
                        "reply_to_message": {"message_id": reply_to}}}
    assert notify.handle(said) == "edited"
    assert _answers(sid)[-1]["content"] == "Ответ менеджера: На 2 года пришлю расчет в течение часа."
    assert "вместо черновика" in tg[-1][1]["text"]
    assert notify.handle(said) == "stale" and len(_answers(sid)) == 2
    assert store.draft(second["id"])["status"] == "edited"

    # кнопка "Не отправлять": клиенту ничего, обращение у менеджера
    _turn(monkeypatch, "А на 3 года?", ["Черновик про 3 года."], session_id=sid)
    third = store.pending_draft(sid)
    drop = {"callback_query": {"id": "cb2", "data": f"drop:{third['id']}", "from": {"first_name": "Анна"},
                               "message": {"chat": {"id": int(CHAT)}, "message_id": 1}}}
    assert notify.handle(drop) == "drop" and len(_answers(sid)) == 2
    assert store.open_handoff(sid)["reason"] == "менеджер не отправил ответ агента"
    stats = _stats(headers=KEY)
    assert (stats["sent"], stats["edited"], stats["dropped"]) == (1, 1, 1)


def test_draft_from_another_chat_is_ignored(tg):
    press = {"callback_query": {"id": "cb", "data": "send:1", "from": {}, "message": {"chat": {"id": 1}}}}
    assert notify.handle(press) == "ignored" and tg == []
