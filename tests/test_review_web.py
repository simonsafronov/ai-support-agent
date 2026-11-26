"""Правки по ревью: повтор ответа менеджера, карточка Telegram, файлы КП при чистке, поток обновления, CSV, кейсы."""
import csv
import html
import io
import json
import time
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import cases, notify, store
from app.agent import loop
from app.agent.llm import Scripted
from app.web import server

client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}
CHAT = "-100500"


@pytest.fixture(autouse=True)
def operator_key(monkeypatch):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    monkeypatch.setattr(server, "_answered", {})


def _handoff(client_id="visitor-1") -> tuple[str, int]:
    res = loop.run_turn(None, "Нужна скидка 15% на 30 лицензий", client_id=client_id, llm=Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "скидка больше 5%", "summary": "Просит 15% на 30 лицензий"}},
        "Передал вопрос менеджеру."]))
    return res["session_id"], store.open_handoff(res["session_id"])["id"]


def _from_manager(sid) -> list[str]:
    return [m["content"] for m in store.messages(sid) if m["meta"].get("from") == "manager"]


def test_double_click_on_answer_gives_the_client_one_message(monkeypatch):
    sid, hid = _handoff()
    body = {"answer": "Скидку 15% согласуем, пришлю счет.", "teach": False}
    first = client.post(f"/api/admin/handoff/{hid}/answer", headers=KEY, json=body)
    second = client.post(f"/api/admin/handoff/{hid}/answer", headers=KEY, json=body)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len(_from_manager(sid)) == 1
    monkeypatch.setattr(server, "REPEAT_SEC", 0)  # срок повтора прошел: тот же текст снова уходит клиенту
    time.sleep(0.01)
    client.post(f"/api/admin/handoff/{hid}/answer", headers=KEY, json=body)
    assert len(_from_manager(sid)) == 2


def test_daily_thread_survives_errors(monkeypatch):
    calls, sleeps = [], []

    class Stop(BaseException):
        pass

    def boom(*a):
        calls.append(a)
        raise RuntimeError("диск")

    def sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 2:
            raise Stop

    monkeypatch.setattr(server.store, "purge", boom)
    monkeypatch.setattr(server, "update_catalog", boom)
    monkeypatch.setattr(server.time, "sleep", sleep)
    with pytest.raises(Stop):
        server._daily_update()
    assert len(calls) == 4  # два круга: чистка и обновление каталога упали, поток живет дальше


def test_csv_cells_cannot_start_a_formula():
    s = store.get_session(None, "visitor-1")
    for text in ("=HYPERLINK(\"http://x\")", "+7 999", "-1", "@SUM(A1)", "\tTAB", "обычный текст"):
        store.add_message(s["id"], "user", text)
    rows = list(csv.reader(io.StringIO(client.get("/api/admin/export.csv", headers=KEY).content.decode("utf-8-sig")),
                           delimiter=";"))
    texts = [r[3] for r in rows[1:]]
    assert texts == ["'=HYPERLINK(\"http://x\")", "'+7 999", "'-1", "'@SUM(A1)", "'\tTAB", "обычный текст"]


def test_purge_removes_quote_files_of_old_dialogs():
    old, fresh = store.get_session(None, "old")["id"], store.get_session(None, "fresh")["id"]
    server.config.QUOTES_DIR.mkdir(parents=True, exist_ok=True)
    for sid, number in ((old, "KP-OLD"), (fresh, "KP-NEW")):
        (server.config.QUOTES_DIR / f"{number}.pdf").write_bytes(b"%PDF-1.4")
        store.add_quote(sid, number, "ООО Ромашка, ИНН 7701234567", [], 100.0, "")
    store.db().execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time() - 100 * 86400, old))
    store.db().commit()
    assert store.purge(90) == 1
    assert not (server.config.QUOTES_DIR / "KP-OLD.pdf").exists()
    assert (server.config.QUOTES_DIR / "KP-NEW.pdf").exists()


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.open = []

    def handle_starttag(self, tag, attrs):
        self.open.append(tag)

    def handle_endtag(self, tag):
        assert self.open and self.open.pop() == tag


def _card(**over):
    card = {"priority_name": "обычная", "sla_minutes": 5, "flags": [], "client": {"компания": "ООО <Ромашка>"},
            "needs": {}, "offers": [], "quote": None, "calc_total": None, "last_message": "", "summary": "", "next": ""}
    return {**card, **over}


def _check(text):
    assert len(text) <= notify.LIMIT and text.endswith(notify.HOWTO)
    tags = _Tags()
    tags.feed(text)
    assert not tags.open  # каждый тег закрыт: Telegram примет разметку


def test_long_card_is_cut_before_markup():
    h = {"id": 7, "status": "new", "reason": "скидка & условия", "summary": "Очень длинный запрос <b>" * 800}
    text = notify.card_text(h, _card(last_message="длинное сообщение & " * 600, summary=h["summary"],
                                     needs={f"поле {i}": "значение " * 50 for i in range(40)}))
    _check(text)
    assert "&lt;b&gt;" in text and "…" in text


def test_card_shows_at_most_twelve_offers():
    offers = [{"name": f"Лицензия {i} <Pro>", "price": 1000.0 + i, "qty": 2} for i in range(40)]
    text = notify.card_text({"id": 8, "status": "new", "reason": "КП"}, _card(offers=offers))
    _check(text)
    assert "Лицензия 11 &lt;Pro&gt;" in text and "Лицензия 12 " not in text and "и еще 28" in text


def test_refused_html_card_goes_once_without_markup(monkeypatch):
    calls = []

    def call(method, files=None, refusal=False, **params):
        calls.append((method, params))
        if params.get("parse_mode") == "HTML":
            return False if refusal else None  # Telegram: can't parse entities
        return {"message_id": 1000 + len(calls)}

    monkeypatch.setattr(notify, "call", call)
    monkeypatch.setattr(notify.config, "TG_TOKEN", "t")
    monkeypatch.setattr(notify.config, "TG_CHAT", CHAT)
    monkeypatch.setattr(notify, "SETTLE", 0)
    _, hid = _handoff()
    assert notify.sync() == 1
    assert [(m, "parse_mode" in p) for m, p in calls] == [("sendMessage", True), ("sendMessage", False)]
    plain = calls[1][1]["text"]
    assert "<b>" not in plain and f"Обращение №{hid}" in plain and plain == html.unescape(plain.replace("<", ""))
    assert store.db().execute("SELECT message_id FROM tg_cards WHERE handoff_id=?", (hid,)).fetchone()[0] == 1002
    store.take_handoff(hid, "Анна")  # карточка изменилась: правка тоже уходит без разметки после отказа
    notify.sync()
    assert [(m, "parse_mode" in p) for m, p in calls[2:]] == [("editMessageText", True), ("editMessageText", False)]


def test_html_sanitized_to_plain_text():
    assert notify.plain("<b>Клиент:</b> ООО &lt;Ромашка&gt; &amp; Ко") == "Клиент: ООО <Ромашка> & Ко"


def test_cases_live_in_data_and_old_file_is_still_read(tmp_path, monkeypatch):
    old, new = tmp_path / "eval" / "cases_added.jsonl", tmp_path / "data" / "cases_added.jsonl"
    old.parent.mkdir()
    old.write_text(json.dumps({"id": "s-old", "turns": ["вопрос"]}, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(cases, "OLD", old)
    monkeypatch.setattr(cases, "ADDED", new)
    assert [c["id"] for c in cases.load()] == ["s-old"]
    assert cases.add({"id": "s-new", "turns": ["другой"]}) == 2
    assert new.exists() and [c["id"] for c in cases.load()] == ["s-old", "s-new"]
    assert cases.config.CASES_ADDED.parent.name == "data"
