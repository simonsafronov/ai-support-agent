"""Список позиций одним сообщением: разбор строк, сверка с каталогом кодом и сквозной ход агента."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store
from app.agent import handoff_card, loop, spec, tools
from app.agent.llm import Scripted
from app.catalog import search as catalog
from app.web import server

# Список для показа: товары каталога заказчика. Indeed PAM в каталоге нет (ни в названиях, ни у вендоров)
DEMO = """1. KL4867RAQFS - 50 шт
2. KL4863RATFS - 30 шт
3. Astra Linux SE 1.8 сервер, 2 лицензии
4. NC260P_CNL_12M_ACC_PROLONG nanoCAD 26 продление на 1 год - 3 шт
5. X5-STD-CF-RDNL-A МойОфис Стандартный, обновление - 10 шт
6. 9EM-00628 Windows Server Standard 2019 - 2 шт
7. Indeed PAM, 2 лицензии"""

# Обычные сообщения из нескольких строк: приветствие и подпись, реквизиты, вопросы списком, ключи, даты, потребности
ORDINARY = [
    "Добрый день!\nНужен Kaspersky Endpoint Security на 50 ПК.\nСрок 1 год.\nСпасибо, Иван",
    "ООО Ромашка\nИНН 7707083893\nКПП 770701001\nр/с 40702810900000000001\nБИК 044525225\nтел. +7 916 123-45-67",
    "1. Есть ли у вас Astra Linux?\n2. Сколько стоит?\n3. Можно ли оплатить картой?",
    "1. Цена на KES на 50 узлов\n2. Срок поставки\n3. Можно ли по безналу",
    "У нас:\n- 50 пользователей\n- 3 сервера\n- 10 ноутбуков\nЧто посоветуете?",
    "Ключи активации:\nABCDE-12345-FGHIJ-67890-KLMNO\nQWERT-12345-ASDFG-67890-ZXCVB\n12ABC-34DEF-56GHI-78JKL",
    "Заказ №12345 от 25.11.2025\nСчет 6789 оплачен 26.11.2025г.\nДокументы до 31.12.2025г. нужны",
    "Здравствуйте.\nУ нас 2 лицензии Windows 10 Pro и 5 лицензий Office 2019.\nХотим обновить до Windows 11.\nЧто посоветуете?",
    "Нужно 50 лицензий.\nНа 2 года.\nДля 3 филиалов.",
    "Добрый день, нам нужно 3 лицензии Astra Linux, 2 сервера и 10 рабочих мест.\nБюджет 300 тысяч.\nСрок до конца года.",
]


def _client_turns() -> list[str]:
    path = Path(__file__).parents[1] / "eval" / "cases.jsonl"
    return [t for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            for t in json.loads(line)["turns"]]


def test_parse_numbered_lines_with_units_and_articles():
    rows = spec.parse("Прошу КП на:\n1. KL4867RATFS - 50 шт\n2) Astra Linux SE 1.8 сервер, 2 лицензии\n"
                      "3. Артикул: X5-STD-CF-RDNL-A, 10 лиц.\n- nanoCAD 26 продление\n• МойОфис Стандартный x 5\nСпасибо")
    assert [(r["query"], r["article"], r["qty"]) for r in rows] == [
        ("KL4867RATFS", "KL4867RATFS", 50), ("Astra Linux SE 1.8 сервер", "", 2),
        ("X5-STD-CF-RDNL-A", "X5-STD-CF-RDNL-A", 10), ("nanoCAD 26 продление", "", None),
        ("МойОфис Стандартный", "", 5)]
    assert rows[0]["line"] == "1. KL4867RATFS - 50 шт"


def test_parse_excel_cells_skips_header_prices_and_total():
    rows = spec.parse("№\tНаименование\tАртикул\tКол-во\tЦена\n"
                      "1\tKaspersky Endpoint Security\tKL4867RAQFS\t50\t3940,20\t197010,00\n"
                      "2\tAstra Linux SE 1.8\t\t2\tшт\n"
                      "3\tМойОфис Стандартный\tX5-STD-CF-RDNL-A\t10 шт\n"
                      "Итого\t\t\t\t300000")
    assert [(r["query"], r["article"], r["qty"]) for r in rows] == [
        ("Kaspersky Endpoint Security KL4867RAQFS", "KL4867RAQFS", 50), ("Astra Linux SE 1.8", "", 2),
        ("МойОфис Стандартный X5-STD-CF-RDNL-A", "X5-STD-CF-RDNL-A", 10)]
    assert rows[0]["line"] == "1 | Kaspersky Endpoint Security | KL4867RAQFS | 50 | 3940,20 | 197010,00"


def test_parse_bare_vendor_articles_without_quantity():
    rows = spec.parse("KL4867RAQFS\nkl4863rapfs\nNC260P_CNL_12M_ACC_PROLONG")
    assert [(r["article"], r["qty"]) for r in rows] == [
        ("KL4867RAQFS", None), ("kl4863rapfs", None), ("NC260P_CNL_12M_ACC_PROLONG", None)]


def test_two_positions_are_not_a_list():
    assert spec.parse("KL4867RAQFS - 50 шт\nAstra Linux SE 1.8, 2 лицензии\nСпасибо") == []


def test_no_list_in_ordinary_client_messages():
    """Ни одна реплика клиентов из набора проверки и ни одно обычное сообщение из нескольких строк не список."""
    turns = _client_turns() + ORDINARY
    assert len(turns) >= 150 + len(ORDINARY)
    assert [t for t in turns if spec.parse(t)] == []


def test_model_cannot_call_the_step():
    assert spec.TOOL not in {t["name"] for t in tools.specs()}
    assert "error" in tools.call(tools.ToolContext("s"), spec.TOOL, {"items": []})


@pytest.mark.small_catalog
def test_ordinary_message_runs_no_step():
    res = loop.run_turn(None, "Добрый день!\nНужен антивирус на 50 ПК.\nСпасибо", llm=Scripted(["Подберу."]))
    assert [t["name"] for t in res["trace"] if t["kind"] == "tool"] == []
    assert not [e for e in res["events"] if e["type"] == "spec"]


@pytest.mark.small_catalog
def test_rows_over_limit_go_to_manager_unmatched():
    rows = spec.parse("\n".join(f"{n}. ZZ{n:04d}QQ - {n} шт" for n in range(1, spec.MAX_ROWS + 3)))
    lines = spec.match(rows)["lines"]
    assert [ln["status"] for ln in lines[spec.MAX_ROWS:]] == [spec.SKIPPED, spec.SKIPPED]
    assert {ln["status"] for ln in lines[:spec.MAX_ROWS]} == {spec.ABSENT}


@pytest.mark.needs_data
def test_statuses_on_customer_catalog():
    out = spec.match(spec.parse(DEMO))
    lines = out["lines"]
    assert [ln["status"] for ln in lines] == [spec.EXACT, spec.CHECK, spec.CHECK, spec.EXACT, spec.EXACT,
                                              spec.ON_REQUEST, spec.ABSENT]
    for ln in lines:
        if ln.get("offer_id"):  # название, артикул и цена строки это предложение каталога как есть
            o = catalog.get_offer(ln["offer_id"])
            assert (ln["name"], ln["vendor_code"], ln["price"]) == (o["name"], o["vendor_code"], o["price"])
    exact = [ln for ln in lines if ln["status"] == spec.EXACT]
    assert [(ln["vendor_code"], ln["qty"], ln["sum"]) for ln in exact] == [
        ("KL4867RAQFS", 50, round(lines[0]["price"] * 50, 2)), ("NC260P_CNL_12M_ACC_PROLONG", 3, 73800),
        ("X5-STD-CF-RDNL-A", 10, 65300)]
    assert out["total"] == round(sum(ln["sum"] for ln in exact), 2)
    assert "sum" not in lines[1] and "sum" not in lines[2]  # сумма только у точных строк
    # артикул для 250-499 узлов, а нужно 30: кандидат того же названия под 30 и подсказка
    assert lines[1]["vendor_code"] == "KL4863RAPFS" and "KL4863RATFS" in lines[1]["hint"]
    assert lines[2]["hint"].startswith("найдено по названию") and "Astra Linux" in lines[2]["name"]
    assert lines[5]["price"] is None and lines[6].get("offer_id") is None
    assert out["counts"] == {spec.EXACT: 3, spec.CHECK: 2, spec.ON_REQUEST: 1, spec.ABSENT: 1}


@pytest.mark.needs_data
def test_quantity_defaults_to_one_with_mark():
    lines = spec.match(spec.parse("NC260P_CNL_12M_ACC_PROLONG\nX5-STD-CF-RDNL-A - 2 шт\n9EM-00628 - 1 шт"))["lines"]
    assert lines[0]["qty"] == 1 and lines[0]["qty_assumed"] and lines[0]["sum"] == lines[0]["price"]
    assert "qty_assumed" not in lines[1]


class TableReader(Scripted):
    """Модель-сценарий, которая первым шагом читает таблицу из ответа match_spec и зовет make_quote по строкам точно."""

    def chat(self, messages, tools_, **kw):
        if not self.calls:
            table = json.loads(messages[-1]["content"])
            self.steps.insert(0, {"name": "make_quote", "args": {"items": [
                {"offer_id": ln["offer_id"], "qty": ln["qty"]} for ln in table["lines"] if ln["status"] == "точно"]}})
        return super().chat(messages, tools_, **kw)


@pytest.mark.needs_data
def test_list_turn_quotes_exact_rows_and_hands_off_the_rest(monkeypatch):
    lines = spec.match(spec.parse(DEMO))["lines"]
    first, total = lines[0], round(sum(ln.get("sum", 0) for ln in lines), 2)
    rub = lambda v: f"{v:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")
    llm = TableReader([
        {"name": "handoff_to_manager", "args": {"reason": "позиции списка без цены и не из каталога",
                                                "summary": "Список из 7 строк, КП на найденные точно."}},
        f"Сверил список: 3 строки найдены точно на {rub(total)} руб., по ним готов черновик КП. "
        f"{first['name']} [[offer:{first['offer_id']}]] стоит {rub(first['price'])} руб. за лицензию. "
        "Две строки проверьте по таблице, остальные передал менеджеру.",
    ])
    events = []
    res = loop.run_turn(None, DEMO, client_id="spec-1", llm=llm, on_event=events.append)
    sid = res["session_id"]

    # шаг кода стоит до первого вызова модели: в журнале, в следе и в истории сообщений как пара вызов + ответ
    assert events[0]["name"] == spec.TOOL and len(events[0]["args"]["items"]) == 7
    assert [t["name"] for t in res["trace"]] == [spec.TOOL, "make_quote", "handoff_to_manager"]
    step = [s for s in store.steps(sid) if s["name"] == spec.TOOL][0]
    assert step["kind"] == "tool" and step["output"]["lines"] == lines
    call, answer = llm.calls[0][2], llm.calls[0][3]
    assert call["role"] == "assistant" and call["tool_call"]["name"] == spec.TOOL
    assert answer["role"] == "tool" and answer["name"] == spec.TOOL and json.loads(answer["content"])["total"] == total

    # в КП ровно строки "точно", цены из каталога
    quote = json.loads(store.last_quote(sid)["items_json"])
    exact = [ln for ln in lines if ln["status"] == spec.EXACT]
    assert [(q["offer_id"], q["qty"], q["price"]) for q in quote] == [
        (ln["offer_id"], ln["qty"], catalog.get_offer(ln["offer_id"])["price"]) for ln in exact]

    # суммы и цены из таблицы проверка цен знает: ответ ушел как есть, карточка варианта из таблицы строится
    assert not [t for t in res["trace"] if t["kind"] == "guard"]
    assert rub(total) in res["reply"] and [c["id"] for c in res["cards"]] == [first["offer_id"]]

    # таблица уходит в чат событием и восстанавливается вместе с историей
    assert [e["type"] for e in res["events"]] == ["spec", "quote", "handoff"]
    assert res["events"][0] == {"type": "spec", "rows": lines, "total": total}
    monkeypatch.setattr(server.config, "CHAT_RATE_PER_MIN", 0)
    history = TestClient(server.app).get(f"/api/session/{sid}?client_id=spec-1").json()["messages"]
    assert history[-1]["events"][0] == {"type": "spec", "rows": lines, "total": total}
    assert history[-1]["trace"][0]["name"] == spec.TOOL

    # карточка менеджеру: строки без КП по статусам с количествами
    needs = handoff_card.build(dict(store.db().execute("SELECT * FROM handoffs").fetchone()))["needs"]
    assert needs[spec.ABSENT] == "Indeed PAM, 2 шт."
    assert needs[spec.ON_REQUEST] == "9EM-00628 Windows Server Standard 2019, 2 шт."
    assert needs[spec.CHECK] == (f"KL4863RATFS ({lines[1]['vendor_code']}), 30 шт.; "
                                 f"Astra Linux SE 1.8 сервер ({lines[2]['vendor_code']}), 2 шт.")


@pytest.mark.needs_data
def test_price_check_knows_table_sums():
    """Сумма найденного и цена кандидата строки "проверьте" есть только в таблице: проверка цен их знает."""
    lines = spec.match(spec.parse(DEMO))["lines"]
    total, check = round(sum(ln.get("sum", 0) for ln in lines), 2), lines[1]
    rub = lambda v: f"{v:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")
    reply = (f"Точно найдены три строки, итого {rub(total)} руб. Для 30 лицензий подходит артикул "
             f"{check['vendor_code']} [[offer:{check['offer_id']}]], цена {rub(check['price'])} руб. за лицензию.")
    res = loop.run_turn(None, DEMO, llm=Scripted([reply]))
    assert not [t for t in res["trace"] if t["kind"] == "guard"]
    assert rub(total) in res["reply"] and rub(check["price"]) in res["reply"]
    assert [c["id"] for c in res["cards"]] == [check["offer_id"]]  # вариант показан таблицей: карточка строится


@pytest.mark.needs_data
def test_card_drops_checked_row_once_it_is_in_quote():
    """Клиент подтвердил вариант строки "проверьте", агент внес его в КП: менеджеру эта строка уже не нужна."""
    lines = spec.match(spec.parse(DEMO))["lines"]
    items = [{"offer_id": ln["offer_id"], "qty": ln["qty"]} for ln in lines if ln["status"] == spec.EXACT]
    items.append({"offer_id": lines[1]["offer_id"], "qty": 30})
    res = loop.run_turn(None, DEMO, llm=Scripted([
        {"name": "make_quote", "args": {"items": items}},
        {"name": "handoff_to_manager", "args": {"reason": "остальные позиции списка", "summary": "Список, КП готов"}},
        "КП готов, остальное у менеджера."]))
    hid = store.open_handoff(res["session_id"])["id"]
    needs = handoff_card.build(dict(store.db().execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()))["needs"]
    assert needs[spec.CHECK].startswith("Astra Linux SE 1.8 сервер") and "KL4863" not in needs[spec.CHECK]


def test_get_product_takes_the_quantity_under_the_name_the_catalog_used_in_clarify():
    """Каталог в clarify предлагает сузить по nodes или users: такой фильтр работает как quantity, а не теряется."""
    from app.agent import tools as agent_tools
    ctx = agent_tools.ToolContext(session_id="t-alias")
    seen = {}

    def handler(ctx, product_id, quantity=None, **filters):
        seen.update(product_id=product_id, quantity=quantity, **filters)
        return {"ok": True}

    tool = {**agent_tools.BY_NAME["get_product"], "handler": handler}
    original = agent_tools.BY_NAME["get_product"]
    agent_tools.BY_NAME["get_product"] = tool
    try:
        assert agent_tools.call(ctx, "get_product", {"product_id": "3953", "nodes": 14}) == {"ok": True}
        assert seen["quantity"] == 14
        seen.clear()
        agent_tools.call(ctx, "get_product", {"product_id": "3953", "users": 25, "quantity": 30})
        assert seen["quantity"] == 30  # явное quantity главнее
        got = agent_tools.call(ctx, "get_product", {"product_id": "3953", "color": "red"})
        assert got["ignored_args"] == ["color"]  # неизвестный фильтр модель видит, а не принимает за примененный
    finally:
        agent_tools.BY_NAME["get_product"] = original
