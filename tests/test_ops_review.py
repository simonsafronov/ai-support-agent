"""Правки по ревью: одна открытая заявка на диалог, доставка в Telegram после сбоя, очередь и статистика панели,
чистка карточек Telegram, выдача КП по записи, ежедневная чистка без фида. Telegram в тестах подменен."""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import economy, notify, store
from app.agent import loop
from app.agent.llm import LLMError, Scripted
from app.web import server

pytestmark = pytest.mark.small_catalog

client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}
CHAT = "-100500"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    monkeypatch.setattr(notify, "_retry_at", {})


class FailsAfter(Scripted):
    """Подставная модель: отдает заданные шаги, следующий вызов падает."""

    def chat(self, messages, tools, **kw):
        if not self.steps:
            raise LLMError("модель недоступна")
        return super().chat(messages, tools, **kw)


def _discount_handoff(client_id="visitor-1") -> str:
    res = loop.run_turn(None, "Нужна скидка 15% на 30 лицензий, почта ivanov@romashka.example", client_id=client_id,
                        llm=Scripted([{"name": "handoff_to_manager", "args": {
                            "reason": "скидка больше 5%", "summary": "Просит 15% на 30 лицензий"}},
                            "Передал вопрос менеджеру."]))
    return res["session_id"]


def _open(sid) -> list:
    return store.db().execute("SELECT * FROM handoffs WHERE session_id=? AND status IN ('new', 'taken')",
                              (sid,)).fetchall()


# --- одна открытая заявка на диалог ---

def test_model_failure_after_handoff_keeps_one_ticket():
    res = loop.run_turn(None, "Нужна скидка 15% на 30 лицензий, почта ivanov@romashka.example", llm=FailsAfter([
        {"name": "handoff_to_manager", "args": {"reason": "скидка больше 5%", "summary": "Просит 15%"}}]))
    sid = res["session_id"]
    assert res["reply"] == loop.FALLBACK
    rows = _open(sid)
    assert len(rows) == 1
    assert rows[0]["reason"] == "скидка больше 5%" and rows[0]["contact"] == "ivanov@romashka.example"
    assert [e["id"] for e in res["events"] if e["type"] == "handoff"] == [rows[0]["id"]]


def test_handoff_returns_the_open_ticket_and_fills_it():
    s = store.get_session(None, "visitor-1")["id"]
    first, created = store.handoff(s, "скидка", "просит скидку", "a@b.example")
    assert created
    again, created = store.handoff(s, "агент не смог ответить", "", "")
    assert (again, created) == (first, False)
    row = _open(s)[0]
    assert (row["reason"], row["summary"], row["contact"]) == ("скидка", "просит скидку", "a@b.example")
    store.handoff(s, "другая причина", "просит 15% на 30 лицензий", "")
    row = _open(s)[0]
    assert (row["reason"], row["summary"], row["contact"]) == ("скидка", "просит 15% на 30 лицензий", "a@b.example")
    store.return_handoff(first)  # закрытая заявка не мешает завести новую
    third, created = store.handoff(s, "снова скидка", "", "")
    assert created and third != first


def test_base_refuses_second_open_ticket():
    s = store.get_session(None, "visitor-1")["id"]
    store.handoff(s, "скидка", "", "")
    with pytest.raises(sqlite3.IntegrityError):
        store.db().execute("INSERT INTO handoffs(session_id, ts, reason, summary, contact) VALUES(?,?,?,?,?)",
                           (s, time.time(), "вторая", "", ""))
    store.db().rollback()


def test_old_duplicates_do_not_break_start(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(store._SCHEMA)
    for _ in range(2):
        con.execute("INSERT INTO handoffs(session_id, ts, reason, summary, contact) VALUES('s1', 1, 'r', '', '')")
    con.commit()
    con.close()
    monkeypatch.setattr(store.config, "APP_DB", path)
    store._local.con = None
    assert store.open_handoff("s1") is not None  # база открылась, индекса нет, работа идет
    assert store.db().execute("SELECT 1 FROM sqlite_master WHERE name='ux_handoffs_open'").fetchone() is None
    hid, created = store.handoff("s1", "еще", "", "")
    assert not created and len(_open("s1")) == 2


def test_button_after_tool_handoff_adds_no_second_ticket():
    sid = _discount_handoff()
    r = client.post("/api/chat/handoff", json={"session_id": sid, "client_id": "visitor-1"}).json()
    assert r["events"] == [] and len(_open(sid)) == 1


# --- Telegram ---

@pytest.fixture
def tg(monkeypatch):
    """Подставной Telegram: down=True значит связи нет, refuse_edit=True значит правка карточки отклонена."""
    state = {"down": False, "refuse_edit": False, "calls": []}

    def call(method, files=None, refusal=False, **params):
        state["calls"].append((method, params))
        if state["down"]:
            return None
        if method == "editMessageText" and state["refuse_edit"]:
            return False if refusal else None
        return {"message_id": 1000 + len(state["calls"])}

    monkeypatch.setattr(notify, "call", call)
    monkeypatch.setattr(notify.config, "TG_TOKEN", "t")
    monkeypatch.setattr(notify.config, "TG_CHAT", CHAT)
    monkeypatch.setattr(notify, "SETTLE", 0)
    return state


def _clock(monkeypatch, shift):
    real = time.time
    monkeypatch.setattr(notify.time, "time", lambda: real() + shift)


def test_card_reaches_manager_after_half_hour_outage(tg, monkeypatch):
    notify.sync()  # доставка уже работала на этой базе
    sid = _discount_handoff()
    tg["down"] = True
    for minute in (0, 5, 10, 20):
        _clock(monkeypatch, minute * 60)
        assert notify.sync() == 0
    tg["down"] = False
    _clock(monkeypatch, 31 * 60)
    assert notify.sync() == 1
    hid = _open(sid)[0]["id"]
    assert store.db().execute("SELECT handoff_id FROM tg_cards").fetchall()[0][0] == hid
    sends = [c for c in tg["calls"] if c[0] == "sendMessage"]
    assert len(sends) == 5 and f"Обращение №{hid}" in sends[-1][1]["text"]  # между попытками пауза RETRY


def test_closed_ticket_without_card_is_not_sent(tg):
    notify.sync()
    sid = _discount_handoff()
    store.return_handoff(_open(sid)[0]["id"])
    assert notify.sync() == 0 and tg["calls"] == []


def test_first_start_on_old_base_sends_nothing(tg):
    sid = _discount_handoff()
    store.db().execute("UPDATE handoffs SET ts = ts - 3600 WHERE session_id=?", (sid,))
    store.db().commit()
    assert notify.sync() == 0 and tg["calls"] == []
    # заявка за несколько минут до первого запуска уходит: ее могли не успеть отправить при смене версии
    other = _discount_handoff("visitor-2")
    store.db().execute("UPDATE handoffs SET ts = ts - 300 WHERE session_id=?", (other,))
    store.db().commit()
    assert notify.sync() == 1


def test_refused_edit_is_not_saved_as_done(tg, monkeypatch):
    sid = _discount_handoff()
    assert notify.sync() == 1
    hid = _open(sid)[0]["id"]
    sent_text = store.db().execute("SELECT text FROM tg_cards WHERE handoff_id=?", (hid,)).fetchone()[0]
    store.take_handoff(hid, "Анна")
    tg["refuse_edit"] = True
    assert notify.sync() == 0
    assert store.db().execute("SELECT text FROM tg_cards WHERE handoff_id=?", (hid,)).fetchone()[0] == sent_text
    tg["refuse_edit"] = False
    _clock(monkeypatch, notify.RETRY + 1)
    assert notify.sync() == 1
    assert "в работе" in store.db().execute("SELECT text FROM tg_cards WHERE handoff_id=?", (hid,)).fetchone()[0]


def test_not_modified_counts_as_success(monkeypatch):
    class Answer:
        def json(self):
            return {"ok": False, "description": "Bad Request: message is not modified: specified new message content "
                                               "and reply markup are exactly the same"}

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: Answer())
    assert notify.call("editMessageText", refusal=True, chat_id=1, message_id=2, text="x") is True


def test_refused_quote_file_is_not_marked_sent(tg, monkeypatch):
    sid = _discount_handoff()
    store.add_quote(sid, "KP-1", "ООО Ромашка", [], 100.0, "")
    server.config.QUOTES_DIR.mkdir(parents=True, exist_ok=True)
    (server.config.QUOTES_DIR / "KP-1.pdf").write_bytes(b"%PDF-1.4")
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda method, files=None, **p: None if method == "sendDocument"
                        else real(method, files, **p))
    notify.sync()
    hid = _open(sid)[0]["id"]
    assert store.db().execute("SELECT quote FROM tg_cards WHERE handoff_id=?", (hid,)).fetchone()[0] == ""
    monkeypatch.setattr(notify, "call", real)
    notify.sync()
    assert store.db().execute("SELECT quote FROM tg_cards WHERE handoff_id=?", (hid,)).fetchone()[0] == "KP-1:100.0"


# --- экран оператора ---

def test_old_urgent_ticket_is_seen_after_fifty_closed():
    old = store.get_session(None, "visitor-old")["id"]
    store.add_message(old, "user", "Тендер по 44-ФЗ, срочно нужен ответ")
    hid = store.add_handoff(old, "госзакупка", "тендер", "")
    store.db().execute("UPDATE handoffs SET ts = ts - 7200 WHERE id=?", (hid,))
    for i in range(60):
        s = store.get_session(None, f"visitor-{i}")["id"]
        store.db().execute("INSERT INTO handoffs(session_id, ts, reason, summary, contact, status) "
                           "VALUES(?,?,?,?,?,'answered')", (s, time.time(), "вопрос", "", ""))
    store.db().commit()
    st = client.get("/api/admin/stats", headers=KEY).json()
    assert st["handoffs"][0]["id"] == hid and st["handoffs"][0]["card"]["overdue"]
    assert len(st["handoffs"]) == 50
    assert st["queue"] == {"waiting": 1, "from_run": 0, "overdue": 1}


def test_queue_counts_beyond_the_page(monkeypatch):
    monkeypatch.setattr(server, "OPEN_MAX", 2)
    for i in range(4):
        s = store.get_session(None, f"visitor-{i}")["id"]
        hid = store.add_handoff(s, "вопрос", "", "")
        store.db().execute("UPDATE handoffs SET ts = ts - 7200 WHERE id=?", (hid,))
    store.db().commit()
    st = client.get("/api/admin/stats", headers=KEY).json()
    assert len(st["handoffs"]) == 2 and st["queue"]["waiting"] == 4 and st["queue"]["overdue"] == 4


# --- статистика панели ---

def test_without_manager_share_stays_between_zero_and_one():
    loop.run_turn(None, "Здравствуйте", client_id="visitor-1", llm=Scripted(["Здравствуйте, чем помочь?"]))
    for who in ("visitor-2", "visitor-3"):  # прямая передача кнопкой: модель не вызывалась
        client.post("/api/chat/handoff", json={"client_id": who})
    st = client.get("/api/admin/stats", headers=KEY).json()
    assert st["dialogs"] == 1 and 0 <= st["handoff_dialogs"] <= st["dialogs"]
    assert 0 <= st["without_human_share"] <= 1
    m = st["economy"]["measured"]
    assert 0 <= m["closed_share"] <= 1 and st["economy"]["stand"]["saved_hours"] >= 0


def test_economy_never_counts_more_handed_than_dialogs():
    e = economy.calc(1, 3, 1.0)
    assert e["measured"]["handed"] == 1 and e["measured"]["closed_share"] == 0 and e["stand"]["saved_rub"] >= 0


# --- хранение данных ---

def test_purge_removes_telegram_card_copy(tg):
    sid = _discount_handoff()
    notify.sync()
    keep = _discount_handoff("visitor-2")
    notify.sync()
    store.db().execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time() - 100 * 86400, sid))
    store.db().commit()
    assert store.purge(90) == 1
    left = [r[0] for r in store.db().execute("SELECT h.session_id FROM tg_cards t JOIN handoffs h "
                                             "ON h.id = t.handoff_id")]
    assert left == [keep] and store.db().execute("SELECT COUNT(*) FROM tg_cards").fetchone()[0] == 1


def test_drop_session_without_telegram_table():
    sid = store.get_session(None, "visitor-1")["id"]
    store.add_handoff(sid, "вопрос", "", "")
    store.drop_session(sid)
    assert store.find_session(sid) is None


def test_quote_file_needs_its_record():
    sid = store.get_session(None, "visitor-1")["id"]
    server.config.QUOTES_DIR.mkdir(parents=True, exist_ok=True)
    for number in ("KP-LIVE", "KP-GONE"):
        (server.config.QUOTES_DIR / f"{number}.pdf").write_bytes(b"%PDF-1.4")
    store.add_quote(sid, "KP-LIVE", "ООО Ромашка", [], 100.0, "")
    signed = lambda n: f"/quotes/{n}.pdf?k={server.quote_key(n + '.pdf')}"
    assert client.get(signed("KP-LIVE")).status_code == 200
    assert client.get(signed("KP-GONE")).status_code == 404  # файл лежит, записи КП нет: не отдается
    assert client.get("/quotes/KP-GONE.pdf", headers=KEY).status_code == 404
    assert client.get("/quotes/KP-LIVE.pdf").status_code == 404  # без подписи и ключа по-прежнему нельзя


def test_update_thread_starts_without_feed(monkeypatch):
    started = []

    class Thread:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(server.config, "FEED_URL", "")
    monkeypatch.setattr(server.threading, "Thread", Thread)
    monkeypatch.setattr(server.notify, "start", lambda answer: None)
    with TestClient(server.app):
        pass
    assert server._update_loop in started


def test_scheduled_work_without_feed_purges_and_skips_catalog(monkeypatch):
    done = []
    monkeypatch.setattr(server.config, "FEED_URL", "")
    monkeypatch.setattr(server.store, "purge", lambda days: done.append("purge"))
    monkeypatch.setattr(server, "update_catalog", lambda: done.append("catalog"))
    server._scheduled_work()
    assert done == ["purge"]
    monkeypatch.setattr(server.config, "FEED_URL", "https://feed.example/f.yml.gz")
    server._scheduled_work()
    assert done == ["purge", "purge", "catalog"]
