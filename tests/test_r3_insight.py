"""Экран оператора: чего нет в каталоге, причины передач, оценка диалогов, "было -> стало", свежесть цен."""
import datetime
import json
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import cases, config, economy, insights, store
from app.catalog import update
from app.web import server

pytestmark = pytest.mark.small_catalog
client = TestClient(server.app)
KEY = {"X-Admin-Token": "secret"}
NOKEY = {}


@pytest.fixture(autouse=True)
def operator_key(monkeypatch, tmp_path):
    monkeypatch.setattr(server.config, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(cases, "ADDED", tmp_path / "cases_added.jsonl")
    monkeypatch.setattr(cases, "OLD", tmp_path / "none.jsonl")
    monkeypatch.setattr(update, "STATE", tmp_path / "catalog_update.json")


def dialog(client_id="eval-t", turns=(), steps=(), handoff=None):
    """Диалог в базе. turns: (кто, текст[, meta]); steps: (ход, вид, имя, вход, выход); handoff: (причина, сводка)."""
    sid = store.get_session(None, client_id)["id"]
    for who, text, *meta in turns:
        store.add_message(sid, who, text, meta[0] if meta else None)
    for turn, kind, name, inp, out in steps:
        store.add_step(sid, turn, kind, name, inp, out)
    if handoff:
        store.handoff(sid, *handoff, "")
    return sid


def rated(sid):
    return insights.rate_session(store.db(), sid)


def codes(sid):
    return [p["code"] for p in rated(sid)["problems"]]


def tool(turn, name, out=None, **inp):
    return (turn, "tool", name, inp, out or {})


EMPTY = {"total": 0, "products": []}
FOUND = {"total": 2, "products": [{"title": "x"}]}


# --- 1. чего нет в каталоге ---

def test_customer_aggregate_has_counts_sources_and_no_personal_data():
    doc = json.loads(insights.GAPS_FILE.read_text(encoding="utf-8"))
    assert "495 обращений" in doc["source"] and "docs/chat_analysis.md" in doc["source"]
    names = {i["query"] for i in doc["items"]}
    assert {"RuPost", "Indeed PAM", "R-Studio", "Articulate 360", "VK WorkMail"} <= names
    for item in doc["items"]:
        assert item["count"] >= 1 and item["match"] and item["example"]
        text = json.dumps(item, ensure_ascii=False)
        assert "@" not in text and not re.search(r"\d{9,}", text) and "ооо" not in text.lower()
        assert server.hide_personal(item["example"]) == item["example"]  # маска персональных данных ничего не находит


@pytest.mark.needs_data
def test_products_from_customer_aggregate_are_absent_from_the_catalog():
    """Проверка по каталогу: ни в названиях позиций, ни в поле производителя этих продуктов нет."""
    con = sqlite3.connect(f"{config.CATALOG_DB.resolve().as_uri()}?mode=ro", uri=True)
    names = [f"{name} {vendor}".lower() for name, vendor in con.execute("SELECT name, COALESCE(vendor, '') FROM offers")]
    for product in (r"rupost", r"\bindeed\b", r"(?<![a-z])r[- ]studio", r"articulate", r"workmail", r"gitlab", r"carbonio"):
        assert not [n for n in names if re.search(product, n)], product


def test_empty_search_counts_only_when_nothing_was_found_afterwards():
    lost = dialog(turns=[("user", "Нужен ZetaSoft Pro"), ("assistant", "Не нашел.")],
                  steps=[tool(1, "search_products", EMPTY, query="ZetaSoft Pro")])
    found_later = dialog(turns=[("user", "Нужен Альт"), ("assistant", "Вот.")],
                         steps=[tool(1, "search_products", EMPTY, query="Альт Рабочая станция", vendor="Базальт"),
                                tool(1, "search_products", FOUND, query="Альт")])
    rows = insights.stand_gaps(insights.load(store.db(), "1=1"))
    assert [(r["query"], r["count"]) for r in rows] == [("ZetaSoft Pro", 1)]
    assert rows[0]["example"] == "Нужен ZetaSoft Pro" and found_later


def test_handoff_not_in_catalog_names_the_product_and_dialogs_are_merged():
    for client_id in ("eval-1", "eval-2"):
        dialog(client_id, turns=[("user", "Цена Zeta Server?"), ("assistant", "Передал.")],
               steps=[tool(1, "search_products", EMPTY, query="Zeta Server")],
               handoff=("продукта Zeta Server нет в каталоге, клиент спрашивает цену", "Нужна цена"))
    dialog("eval-3", turns=[("user", "Zeta Server Ultimate на 5 мест")],
           steps=[tool(1, "search_products", EMPTY, query="Zeta Server Ultimate")])
    rows = insights.stand_gaps(insights.load(store.db(), "1=1"))
    assert [(r["query"], r["count"]) for r in rows] == [("Zeta Server", 3)]


def test_price_on_request_handoff_is_demand_named_by_last_search():
    dialog(turns=[("user", "Сколько стоит САПР для лекал?"), ("assistant", "Цена по запросу.")],
           steps=[tool(1, "search_products", FOUND, query="САПР лекала")],
           handoff=("у подходящих вариантов цена по запросу", "Цена по запросу"))
    assert [(r["query"], r["count"]) for r in insights.stand_gaps(insights.load(store.db(), "1=1"))] == [("САПР лекала", 1)]


def test_gaps_block_merges_customer_and_stand_sources():
    dialog(turns=[("user", "Изучаю RuPost Enterprise")], steps=[tool(1, "search_products", EMPTY, query="RuPost")],
           handoff=("продукта RuPost Enterprise нет в каталоге", "Нужна цена"))
    rows = insights.gaps(insights.load(store.db(), "1=1"))["rows"]
    row = next(r for r in rows if r["query"] == "RuPost")
    sources = {s["name"]: s["count"] for s in row["sources"]}
    assert sources == {"переписки заказчика": 3, "диалоги стенда": 1} and row["count"] == 4
    assert [r["count"] for r in rows] == sorted((r["count"] for r in rows), reverse=True)


def test_gaps_in_stats_follow_visibility_rules():
    dialog("eval-visible", turns=[("user", "Нужен Qwertyx Suite")], steps=[tool(1, "search_products", EMPTY, query="Qwertyx Suite")])
    dialog("visitor-1", turns=[("user", "Нужен Uniqumo Cloud")], steps=[tool(1, "search_products", EMPTY, query="Uniqumo Cloud")])
    queries = lambda headers: {r["query"] for r in client.get("/api/admin/stats", headers=headers).json()["gaps"]["rows"]}
    assert {"Qwertyx Suite", "Uniqumo Cloud"} <= queries(KEY)
    demo = queries(NOKEY)
    assert "Qwertyx Suite" in demo and "Uniqumo Cloud" not in demo and "RuPost" in demo  # агрегат заказчика виден всем


# --- 2. причины передач ---

@pytest.mark.parametrize("reason, summary, kind", [
    ("госзакупка по 223-ФЗ и запрос скидки 15%", "", "tender"),
    ("конкурентная закупка, запрошено КП", "", "tender"),
    ("партнер просит скидку 15% и особые условия", "", "discount"),
    ("клиент просит скидку", "", "discount"),
    ("просьба об отсрочке платежа на месяц", "", "deferral"),
    ("оплата после поставки", "", "deferral"),
    ("продукта Cursor AI нет в каталоге, клиент хочет купить", "", "catalog"),
    ("GitLab отсутствует в каталоге", "", "catalog"),
    ("клиент спрашивает цену САПР, у вариантов цена по запросу", "", "price"),
    ("Клиент просит позвать человека и узнать цену", "", "human"),
    ("клиент позвал менеджера кнопкой", "Клиент еще ничего не написал.", "human"),
    ("сбой при ответе агента", "Последнее сообщение клиента: привет", "failure"),
    ("агент не смог ответить", "", "failure"),
    ("получение бухгалтерских документов через ЭДО", "", "other"),
    ("причина без подсказок", "клиент хочет скидку на лицензии", "discount"),  # по причине тип не ясен: берется сводка
    ("партнер просит скидку 3%", "", "other"),  # до 5% агент дает сам: это уже не причина передачи
    ("клиент просит отправить кп, передайте менеджеру", "", "other"),  # "дайте менеджера" внутри слова не просьба
])
def test_handoff_type_by_saved_fields(reason, summary, kind):
    assert insights.handoff_type({"reason": reason, "summary": summary}) == kind


def test_handoff_reasons_count_and_share_for_every_type():
    handoffs = [{"reason": r, "summary": ""} for r in (
        "сбой при ответе агента", "клиент позвал менеджера кнопкой", "тендер", "скидка 20%", "отсрочка платежа",
        "нет в каталоге", "нет в каталоге", "цена по запросу", "что-то другое", "что-то другое")]
    res = insights.handoff_reasons(handoffs)
    by = {i["code"]: i for i in res["items"]}
    assert res["total"] == 10 and [i["code"] for i in res["items"]] == [
        "failure", "human", "tender", "discount", "deferral", "catalog", "price", "other"]
    assert (by["catalog"]["count"], by["catalog"]["share"]) == (2, 0.2) and by["discount"]["label"] == "Скидка больше 5%"
    assert sum(i["count"] for i in res["items"]) == 10
    assert insights.handoff_reasons([])["items"][0]["share"] == 0


def test_handoff_reasons_in_stats_use_stored_handoffs():
    dialog("eval-a", turns=[("user", "Нужна скидка 30%")], handoff=("клиент просит скидку 30%", "скидка"))
    dialog("eval-b", turns=[("user", "Нужен тендер")], handoff=("госзакупка", "44-ФЗ"))
    res = client.get("/api/admin/stats", headers=KEY).json()["handoff_reasons"]
    assert {i["code"]: i["count"] for i in res["items"]}["discount"] == 1 and res["total"] == 2


# --- 3. проблемные диалоги ---

def test_price_guard_error_and_streak_rules():
    guard = dialog(turns=[("user", "Цена?"), ("assistant", "Цена 100 руб.")], steps=[(1, "guard", "price", {}, {"unknown": ["21 000 руб."]})])
    assert codes(guard) == ["price"] and rated(guard)["score"] == 3
    broken = dialog(turns=[("user", "Привет"), ("assistant", "Не получилось.")], steps=[(1, "error", "deadline", {}, {"error": "x"})])
    assert codes(broken) == ["error"] and rated(broken)["score"] == 3
    loop_steps = [tool(1, "get_product", product_id="1") for _ in range(4)]
    circle = dialog(turns=[("user", "Цена?"), ("assistant", "Вот цена.")], steps=loop_steps)
    assert codes(circle) == ["streak"] and rated(circle)["score"] == 4
    three = dialog(turns=[("user", "Цена?"), ("assistant", "Вот цена.")], steps=loop_steps[:3])
    assert rated(three)["problems"] == [] and rated(three)["score"] == 5


def test_streak_is_broken_by_another_tool_and_by_next_turn():
    steps = [tool(1, "get_product"), tool(1, "get_product"), tool(1, "kb_search"), tool(1, "get_product"),
             tool(1, "get_product"), tool(2, "get_product"), tool(2, "get_product")]
    sid = dialog(turns=[("user", "Цена?"), ("assistant", "Вот.")], steps=steps)
    assert codes(sid) == []


def test_repeated_question_and_short_replies():
    sid = dialog(turns=[("user", "Сколько стоит Kaspersky Endpoint Security на 20 мест?"), ("assistant", "Уточните срок."),
                        ("user", "сколько стоит kaspersky endpoint security на 20 мест"), ("assistant", "Цена такая.")])
    assert codes(sid) == ["repeat"] and rated(sid)["score"] == 4
    short = dialog(turns=[("user", "да"), ("assistant", "Хорошо."), ("user", "да"), ("assistant", "Ясно.")])
    assert codes(short) == []


def test_human_asked_after_agent_answer():
    sid = dialog(turns=[("user", "Цена Windows 11?"), ("assistant", "Вот цена."), ("user", "Позовите менеджера, пожалуйста"),
                        ("assistant", "Передаю.")])
    assert codes(sid) == ["human"] and rated(sid)["score"] == 3
    first = dialog(turns=[("user", "Позовите менеджера"), ("assistant", "Передаю.")])
    assert codes(first) == []  # просьба в первой реплике: агент еще не отвечал
    button = dialog(turns=[("user", "Цена?"), ("assistant", "Вот цена.")], handoff=("клиент позвал менеджера кнопкой", "—"))
    assert codes(button) == ["human"]
    not_human = dialog(turns=[("user", "Цена?"), ("assistant", "Вот.")] + [("user", "Хорошо, передайте менеджеру"), ("assistant", "Готово.")])
    assert codes(not_human) == []


def test_last_agent_question_unanswered():
    open_q = dialog(turns=[("user", "Нужен антивирус"), ("assistant", "На сколько рабочих мест?")])
    assert codes(open_q) == ["unanswered"] and rated(open_q)["score"] == 4
    answered = dialog(turns=[("user", "Нужен антивирус"), ("assistant", "На сколько мест?"), ("user", "На 10"), ("assistant", "Вот цена.")])
    assert codes(answered) == []
    by_manager = dialog(turns=[("user", "Нужен антивирус"), ("assistant", "Подскажите срок?", {"from": "manager"})])
    assert codes(by_manager) == []


def test_score_floor_and_dialog_without_client_turns():
    sid = dialog(turns=[("user", "Позовите менеджера пожалуйста"), ("assistant", "Что именно нужно?"), ("user", "Позовите менеджера пожалуйста сейчас"),
                        ("assistant", "Сейчас?")],
                 steps=[(1, "error", "x", {}, {}), (1, "guard", "price", {}, {})] + [tool(1, "get_product") for _ in range(5)])
    assert rated(sid)["score"] == 1
    empty = store.get_session(None, "eval-empty")["id"]
    assert rated(empty) is None


def test_sessions_list_has_score_for_every_dialog_and_problem_filter_fields():
    bad = dialog("eval-bad", turns=[("user", "Цена?"), ("assistant", "Цена 100 руб.")], steps=[(1, "guard", "price", {}, {"unknown": ["1 руб."]})])
    good = dialog("eval-good", turns=[("user", "Цена?"), ("assistant", "Цена 100 руб.")])
    rows = {r["id"]: r for r in client.get("/api/admin/sessions", headers=KEY).json()["sessions"]}
    assert (rows[bad]["score"], rows[good]["score"]) == (3, 5)
    assert [p["label"] for p in rows[bad]["problems"]] == ["сработала проверка цен"] and rows[good]["problems"] == []
    stats = client.get("/api/admin/stats", headers=KEY).json()["problems"]
    assert stats["rated"] == 2 and stats["problematic"] == 1 and stats["bad_score"] == insights.BAD_SCORE
    assert [w["id"] for w in stats["worst"]] == [bad]
    assert {r["code"]: r["count"] for r in stats["by_rule"]}["price"] == 1
    opened = client.get(f"/api/admin/session/{bad}", headers=KEY).json()
    assert opened["rating"]["score"] == 3


def test_problem_dialog_goes_to_the_check_set_in_one_request():
    sid = dialog("eval-bad", turns=[("user", "Цена?"), ("assistant", "Цена 100 руб.")], steps=[(1, "guard", "price", {}, {"unknown": ["1 руб."]})])
    worst = client.get("/api/admin/stats", headers=KEY).json()["problems"]["worst"][0]
    assert worst["in_set"] is False
    note = f"Проблемный диалог, оценка {worst['score']} из 5: " + "; ".join(p["label"] for p in worst["problems"])
    r = client.post(f"/api/admin/session/{sid}/case", headers=KEY, json={"handoff": None, "quote": False, "note": note})
    assert r.status_code == 200 and r.json()["case_id"] == "s-" + sid[:8]
    assert cases.load()[0]["note"] == note
    assert client.get("/api/admin/stats", headers=KEY).json()["problems"]["worst"][0]["in_set"] is True
    assert next(s for s in client.get("/api/admin/sessions", headers=KEY).json()["sessions"] if s["id"] == sid)["in_set"] is True


def test_problems_without_key_cover_only_demo_dialogs():
    dialog("eval-demo", turns=[("user", "Цена?"), ("assistant", "Какой срок?")])
    dialog("visitor-9", turns=[("user", "Цена?"), ("assistant", "Какой срок?")])
    assert client.get("/api/admin/stats", headers=NOKEY).json()["problems"]["rated"] == 1
    assert client.get("/api/admin/stats", headers=KEY).json()["problems"]["rated"] == 2
    assert len(client.get("/api/admin/sessions", headers=NOKEY).json()["sessions"]) == 1


# --- 4. было -> стало ---

def test_before_after_numbers_come_from_economy_and_carry_their_source():
    eco = economy.calc(10, 4, 100.0)  # 6 из 10 без менеджера
    rows = insights.before_after(eco, 0.6, 6, 10)
    cat, first, share, hours = rows
    assert cat["before"]["value"] == "около 30 мин" and cat["before"]["who"] == "оценка заказчика"
    assert cat["after"] == {"value": f"{insights.ANSWER_MEDIAN_SEC:.1f} с".replace(".", ","), "note": cat["after"]["note"], "who": "замер стенда"} and "живых ответов" in cat["after"]["note"]
    assert (first["before"]["value"], first["before"]["who"]) == ("2 мин", "замер заказчика")
    assert (first["after"]["value"], first["after"]["who"]) == ("около 3 с", "замер стенда")
    assert (share["after"]["value"], share["before"]["value"]) == ("60%", "0%") and "6 из 10" in share["after"]["note"]
    assert hours["before"]["value"] == "250 ч"  # 500 обращений по 30 минут: то же, что в расчете экономики
    saved = eco["half_year"]["saved_hours"]
    assert f"снято {int(saved)} ч" in hours["after"]["note"]
    assert all(r["before"]["who"] and r["after"]["who"] for r in rows)


def test_before_after_without_dialogs_says_no_data():
    rows = insights.before_after(economy.calc(0, 0, 0.0), None, 0, 0)
    assert rows[2]["after"]["value"] == "нет данных" and rows[3]["after"]["value"] == "нет данных"
    assert rows[0]["after"]["value"] == f"{insights.ANSWER_MEDIAN_SEC:.1f} с".replace(".", ",")


def test_before_after_in_stats_matches_economy_block():
    dialog("eval-1", turns=[("user", "Цена?"), ("assistant", "Вот.")])
    s = client.get("/api/admin/stats", headers=KEY).json()
    assert s["before_after"] == insights.before_after(s["economy"], s["requests_without_human_share"],
                                                      s["closed_dialogs"], s["requests"])


# --- 5. свежесть цен и расписание обновления ---

def at(day, hour, minute=0, second=0):
    return datetime.datetime(2026, 11, day, hour, minute, second, tzinfo=server.MSK)


@pytest.mark.parametrize("every, now, expected", [
    (3, at(26, 0), at(26, 3)),
    (3, at(26, 2, 59, 59), at(26, 3)),
    (3, at(26, 3, 0, 1), at(26, 6)),
    (3, at(26, 22, 30), at(27, 0)),
    (24, at(26, 5), at(27, 0)),
    (5, at(26, 19, 59), at(26, 20)),
    (5, at(26, 20, 1), at(27, 0)),  # после последней точки суток идет полночь, а не 25 часов
    (1, at(26, 13, 20), at(26, 14)),
])
def test_next_run_grid_from_moscow_midnight(monkeypatch, every, now, expected):
    monkeypatch.setattr(server.config, "FEED_UPDATE_EVERY_H", every)
    assert server.next_run(now) == expected
    utc = now.astimezone(datetime.timezone.utc)
    assert server.next_run(utc) == expected  # время в другом поясе не сдвигает сетку


def test_default_update_period_is_three_hours_and_old_setting_is_gone():
    assert config.FEED_UPDATE_EVERY_H == 3 and not hasattr(config, "FEED_UPDATE_HOUR")


def test_update_loop_sleeps_to_each_slot_and_runs_work(monkeypatch):
    clock = [at(26, 1, 30)]
    slept, work = [], []

    class Stop(BaseException):
        pass

    def sleep(sec):
        slept.append(sec)
        clock[0] += datetime.timedelta(seconds=sec)  # время идет вместе со сном
        if len(slept) > 4:
            raise Stop

    monkeypatch.setattr(server.config, "FEED_UPDATE_EVERY_H", 3)
    monkeypatch.setattr(server, "_now", lambda: clock[0])
    monkeypatch.setattr(server.time, "sleep", sleep)
    monkeypatch.setattr(server, "_scheduled_work", lambda: work.append(clock[0]))
    with pytest.raises(Stop):
        server._update_loop()
    assert slept[0] == 90 * 60 + 1  # до 03:00 с секундой запаса
    assert all(sec == 3 * 3600 for sec in slept[1:])  # дальше каждые три часа, без двойного запуска точки
    assert [t.hour for t in work] == [3, 6, 9, 12] and all(t.minute == 0 and t.second == 1 for t in work)


def test_scheduled_work_purges_and_updates_catalog(monkeypatch):
    done = []
    monkeypatch.setattr(server.config, "FEED_URL", "https://feed.example/f.yml.gz")
    monkeypatch.setattr(server.store, "purge", lambda days: done.append("purge"))
    monkeypatch.setattr(server, "update_catalog", lambda: done.append("catalog"))
    server._scheduled_work()
    assert done == ["purge", "catalog"]


def test_prices_stamp_is_last_successful_update_or_catalog_build(small_catalog_db):
    built = update.prices_at()  # обновлений не было: время сборки базы из meta
    assert built == time.mktime(time.strptime(
        sqlite3.connect(small_catalog_db).execute("SELECT value FROM meta WHERE key='built_at'").fetchone()[0], "%Y-%m-%d %H:%M:%S"))
    update.STATE.write_text(json.dumps({"ok": True, "finished": 1795686496.5}), encoding="utf-8")
    assert update.prices_at() == 1795686496.5
    update.STATE.write_text(json.dumps({"ok": False, "finished": 1795690000.0, "error": "сеть"}), encoding="utf-8")
    assert update.prices_at() == built  # последнее обновление не прошло: цены остались от прежней сборки


def test_prices_stamp_without_catalog_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CATALOG_DB", tmp_path / "missing.sqlite")
    assert update.prices_at() is None


def test_catalog_screen_block_reports_update_state(monkeypatch):
    monkeypatch.setattr(server.config, "FEED_URL", "https://feed.example/f.yml.gz")
    monkeypatch.setattr(server.config, "FEED_UPDATE_EVERY_H", 3)
    update.STATE.write_text(json.dumps({"ok": True, "finished": 1795686496.0, "price_changed": 21132, "added": 850, "removed": 600}),
                            encoding="utf-8")
    monkeypatch.setattr(server, "_now", lambda: at(26, 13, 40))
    c = client.get("/api/admin/catalog").json()
    assert c["update_every_h"] == 3 and c["next_update"] == at(26, 15).timestamp() and "update_hour_msk" not in c
    assert c["prices_at"] == 1795686496.0 and c["last_update"]["price_changed"] == 21132
    monkeypatch.setattr(server.config, "FEED_URL", "")
    assert client.get("/api/admin/catalog").json()["next_update"] is None  # без фида обновления по расписанию нет


def test_chat_shows_catalog_price_time_under_priced_cards():
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert "Цены каталога на ${p.day}.${p.month} ${p.hour}:${p.minute} МСК" in js
    assert "Europe/Moscow" in js and 'hourCycle: "h23"' in js
    assert 'add("fresh", pricesLine())' in js and "c.price || c.price_min" in js and "state.pricesAt = h.prices_at" in js


def test_health_carries_the_price_time_for_the_chat(monkeypatch):
    monkeypatch.setattr(server.kb, "stats", lambda: {"chunks": 0})
    monkeypatch.setattr(server.llm, "model_online", lambda: True)
    update.STATE.write_text(json.dumps({"ok": True, "finished": 1795686496.0}), encoding="utf-8")
    assert client.get("/api/health").json()["prices_at"] == 1795686496.0
