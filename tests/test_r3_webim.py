"""Адаптер Webim (app/webim.py) против поддельного API Webim из eval/webim_emulator.py, модель-сценарий вместо модели.

Поддельный Webim настоящий HTTP-сервер: адаптер ходит в него тем же вызовом, что в Webim, с токеном бота, а сервер
проверяет токен и форму вызова по документации External Bot API 2.0 и записывает вызовы.
"""
import os
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, store, webim
from app.agent import loop
from app.agent.llm import LLMError, Scripted
from app.catalog import search as catalog
from app.web import server
from eval.webim_emulator import FakeWebim, new_chat, new_message, pressed, visitor

pytestmark = pytest.mark.small_catalog

TOKEN, SECRET, DEP = "bot-token-1", "hook-secret-1", "sales"
hook = FastAPI()
hook.include_router(webim.router)
client = TestClient(hook)
site = TestClient(server.app)


@pytest.fixture(scope="module")
def offer(small_catalog_db):
    """Вариант Kaspersky с ценой из синтетического каталога."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CATALOG_DB", str(small_catalog_db))
        product = catalog.search_products("Kaspersky Endpoint Security", only_available=True, limit=3)["products"][0]
        offers = catalog.get_product(product["product_id"], limit=5)["offers"]
    return {**next(o for o in offers if o["price"]), "product_id": product["product_id"]}


@pytest.fixture
def fake(monkeypatch):
    f = FakeWebim(TOKEN)
    for key, value in (("WEBIM_TOKEN", TOKEN), ("WEBIM_SECRET", SECRET), ("WEBIM_API_URL", f.url),
                       ("WEBIM_DEP_KEY", DEP), ("PUBLIC_URL", "https://bot.example.ru"), ("ADMIN_TOKEN", "secret")):
        monkeypatch.setattr(config, key, value)
    webim._seen.clear()
    yield f
    f.close()


@pytest.fixture
def jobs(monkeypatch):
    """Фоновые ходы адаптера: те же потоки, только тест может дождаться их конца."""
    threads = []

    def start(fn, *args):
        t = threading.Thread(target=fn, args=args)
        t.start()
        threads.append(t)

    monkeypatch.setattr(webim, "_background", start)
    return threads


def settle(threads):
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)


def model(monkeypatch, llm):
    monkeypatch.setattr(server.loop, "get_llm", lambda: llm)
    return llm


def asked(llm) -> list[str]:
    """Реплики клиента, которые модель получила в первом вызове хода."""
    return [m["content"] for m in llm.calls[0] if m["role"] == "user"]


def post(event, secret=SECRET):
    return client.post(f"/api/webim/{secret}", json=event)


def sent(fake, chat_id):
    """Вызовы бота по чату: (метод, сообщение или тело перевода)."""
    return [(c["method"], c["payload"].get("message") or {k: v for k, v in c["payload"].items() if k != "chat_id"})
            for c in fake.of_chat(chat_id)]


def test_price_question_gets_the_catalog_price_card_and_buttons(fake, jobs, monkeypatch, offer):
    price = webim.rub(offer["price"])
    llm = model(monkeypatch, Scripted([
        {"name": "get_product", "args": {"product_id": str(offer["product_id"])}},
        f"Подходит {offer['name']} [[offer:{offer['offer_id']}]], цена {price}.\n"
        "[[choices: Оформить КП | Другой срок]]",
    ]))
    r = post(new_chat(9001, visitor("Сколько стоит Kaspersky на 10 ПК")))
    assert (r.status_code, r.json()) == (200, {"result": "ok"})
    settle(jobs)
    (m1, text), (m2, keyboard) = sent(fake, 9001)
    assert (m1, text["kind"], m2) == ("send_message", "operator", "send_message")
    assert f"цена {price}" in text["text"] and "[[" not in text["text"]
    assert f"{offer['name']}: {price}" in text["text"]  # карточка варианта строкой под ответом, цена из каталога
    assert keyboard == {"kind": "keyboard", "buttons": [[{"id": "choice-1", "text": "Оформить КП"}],
                                                        [{"id": "choice-2", "text": "Другой срок"}],
                                                        [webim.MANAGER]]}
    sid = webim.session_id(9001)
    assert store.find_session(sid)["client_id"] == sid and sid.startswith("webim-9001-")
    assert [m["role"] for m in store.messages(sid)] == ["user", "assistant"]
    assert asked(llm) == ["Сколько стоит Kaspersky на 10 ПК"]


def test_discount_above_the_limit_hands_the_chat_to_the_sales_department(fake, jobs, monkeypatch):
    reply = "Скидку больше 5% согласует менеджер: передал ему запрос, он подключится в этом чате."
    model(monkeypatch, Scripted([
        {"name": "handoff_to_manager", "args": {"reason": "скидка больше 5%", "summary": "Скидка 15% на Kaspersky"}},
        reply,
    ]))
    post(new_message(9002, visitor("Нужна скидка 15%")))
    settle(jobs)
    assert sent(fake, 9002) == [("send_message", {"kind": "operator", "text": reply}),
                                ("redirect_chat", {"dep_key": DEP})]
    assert store.open_handoff(webim.session_id(9002))["reason"] == "скидка больше 5%"


def test_without_department_the_handoff_goes_to_the_common_queue(fake, jobs, monkeypatch):
    monkeypatch.setattr(config, "WEBIM_DEP_KEY", "")
    model(monkeypatch, Scripted([{"name": "handoff_to_manager", "args": {"reason": "жалоба", "summary": "Жалоба"}},
                                 "Передал менеджеру, он ответит в этом чате."]))
    post(new_message(9003, visitor("Хочу пожаловаться")))
    settle(jobs)
    assert sent(fake, 9003)[-1] == ("redirect_chat", {})


class Broken:
    def chat(self, *args, **kwargs):
        raise LLMError("модель недоступна")


def crash(*args, **kwargs):
    raise RuntimeError("сбой кода хода")


@pytest.mark.parametrize("where", ["model", "code"])
def test_failed_turn_sends_the_fallback_and_puts_the_chat_into_the_common_queue(fake, jobs, monkeypatch, where):
    """Зачем: при сбое модели или кода чат уходит к людям в общую очередь, а не в отдел и не остается у бота."""
    model(monkeypatch, Broken())
    if where == "code":
        monkeypatch.setattr(server.loop, "run_turn", crash)
    post(new_chat(9004, visitor("Сколько стоит Kaspersky на 10 ПК")))
    settle(jobs)
    assert sent(fake, 9004) == [("send_message", {"kind": "operator", "text": loop.FALLBACK}), ("redirect_chat", {})]
    assert store.open_handoff(webim.session_id(9004)) is not None  # обращение на экране оператора тоже есть


def test_repeated_delivery_does_not_start_a_second_turn(fake, jobs, monkeypatch):
    """Зачем: Webim повторяет доставку до пяти раз, если не дождался ответа; клиент не получает два ответа."""
    llm = model(monkeypatch, Scripted(["Здравствуйте, подскажу.", "Второй ответ."]))
    first = new_chat(9005, visitor("Добрый день"))
    again = new_message(9005, visitor("Есть Astra Linux?"))
    for event in (first, first, again, again):
        assert post(event).json() == {"result": "ok"}
    settle(jobs)
    assert len(jobs) == 2 and len(llm.calls) == 2
    assert [m for m, _ in sent(fake, 9005)] == ["send_message", "send_message"]
    assert [m["content"] for m in store.messages(webim.session_id(9005)) if m["role"] == "user"] == \
        ["Добрый день", "Есть Astra Linux?"]


def test_messages_written_before_the_bot_got_the_chat_make_one_turn(fake, jobs, monkeypatch):
    llm = model(monkeypatch, Scripted(["Подскажу."]))
    post(new_chat(9006, visitor("Здравствуйте"), visitor("Нужен Kaspersky на 10 ПК")))
    settle(jobs)
    assert asked(llm) == ["Здравствуйте\nНужен Kaspersky на 10 ПК"] and len(llm.calls) == 1


def test_quote_goes_as_a_pdf_file_by_a_signed_link(fake, jobs, monkeypatch, offer):
    model(monkeypatch, Scripted([
        {"name": "make_quote", "args": {"items": [{"offer_id": offer["offer_id"], "qty": offer["qty_min"] or 1}]}},
        "Черновик КП готов, файл ниже.",
    ]))
    post(new_chat(9007, visitor(f"КП на {offer['name']}")))
    settle(jobs)
    (_, text), (_, file) = sent(fake, 9007)
    assert text["text"] == "Черновик КП готов, файл ниже."
    number = store.last_quote(webim.session_id(9007))["number"]
    name = f"{number}.pdf"
    assert file == {"kind": "file_operator", "data": {
        "url": f"https://bot.example.ru/quotes/{quote(name)}?k={server.quote_key(name)}", "name": name,
        "media_type": "application/pdf"}}
    link = urlsplit(file["data"]["url"])
    pdf = site.get(f"{link.path}?{link.query}")  # по этой ссылке Webim скачает файл без ключа оператора
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")


def test_quote_link_goes_as_text_when_the_account_does_not_allow_pdf(fake, jobs, monkeypatch, offer):
    fake.reply = lambda method, p: ({"error": "insecure-file-type"} if p.get("message", {}).get("kind") ==
                                    "file_operator" else {"result": "ok"})
    items = [{"offer_id": offer["offer_id"], "qty": offer["qty_min"] or 1}]
    model(monkeypatch, Scripted([{"name": "make_quote", "args": {"items": items}}, "Черновик КП готов."]))
    post(new_chat(9008, visitor("Нужно КП")))
    settle(jobs)
    number = store.last_quote(webim.session_id(9008))["number"]
    (_, last) = sent(fake, 9008)[-1]
    assert last["text"].startswith(f"Черновик {number}: https://bot.example.ru/quotes/")


def test_manager_button_redirects_without_the_model(fake, jobs, monkeypatch):
    llm = model(monkeypatch, Scripted([]))
    post(new_message(9009, pressed(webim.MANAGER, "kb1")))
    settle(jobs)
    (m1, text), (m2, target) = sent(fake, 9009)
    assert text["text"].startswith(server.CALLED) and (m2, target) == ("redirect_chat", {"dep_key": DEP})
    assert llm.calls == []
    assert store.open_handoff(webim.session_id(9009))["reason"] == "клиент позвал менеджера кнопкой"


def test_choice_button_is_the_client_reply(fake, jobs, monkeypatch):
    llm = model(monkeypatch, Scripted(["Понял, на два года."]))
    post(new_message(9010, pressed({"id": "choice-2", "text": "Другой срок"}, "kb2")))
    settle(jobs)
    assert asked(llm) == ["Другой срок"]


def test_department_that_does_not_take_the_chat_falls_back_to_the_common_queue(fake, jobs, monkeypatch):
    fake.reply = lambda method, p: {"error": "target-is-offline"} if p.get("dep_key") else {"result": "ok"}
    post(new_message(9011, pressed(webim.MANAGER, "kb3")))
    settle(jobs)
    assert [x for x in sent(fake, 9011) if x[0] == "redirect_chat"] == [("redirect_chat", {"dep_key": DEP}),
                                                                       ("redirect_chat", {})]


def test_answer_that_webim_did_not_take_puts_the_chat_into_the_queue(fake, jobs, monkeypatch):
    fake.reply = lambda method, p: {"error": "message-not-created"} if method == "send_message" else {"result": "ok"}
    model(monkeypatch, Scripted(["Здравствуйте."]))
    post(new_message(9012, visitor("Добрый день")))
    settle(jobs)
    assert sent(fake, 9012)[-1] == ("redirect_chat", {})


def test_event_is_acknowledged_at_once_while_the_turn_goes_on(fake, jobs, monkeypatch):
    """Зачем: Webim ждет только подтверждения приема; ход агента длиной в десятки секунд идет после ответа."""
    go = threading.Event()

    class Slow(Scripted):
        def chat(self, *args, **kwargs):
            go.wait(10)
            return super().chat(*args, **kwargs)

    model(monkeypatch, Slow(["Готово."]))
    started = time.monotonic()
    r = post(new_message(9013, visitor("Добрый день")))
    assert r.json() == {"result": "ok"} and time.monotonic() - started < 1 and fake.calls == []
    go.set()
    settle(jobs)
    assert sent(fake, 9013) == [("send_message", {"kind": "operator", "text": "Готово."})]


def test_visitor_file_gets_one_text_reply_when_ready(fake, jobs):
    upload = {"id": "f1", "kind": "file_visitor", "data": {"state": "upload", "progress": 40}}
    ready = {"id": "f1", "kind": "file_visitor", "data": {"state": "ready", "name": "карточка.pdf",
                                                          "media_type": "application/pdf", "url": "/x"}}
    post(new_message(9014, upload))
    for _ in range(2):
        post({"event": "message_updated", "chat_id": 9014, "message": ready})
    settle(jobs)
    assert sent(fake, 9014) == [("send_message", {"kind": "operator", "text": webim.FILE_REPLY})]


def test_wrong_or_unset_secret_is_refused(fake, jobs, monkeypatch):
    event = new_message(9015, visitor("Добрый день"))
    assert post(event, secret="guess").status_code == 403
    monkeypatch.setattr(config, "WEBIM_SECRET", "")
    assert post(event, secret="guess").status_code == 403
    assert jobs == []


def test_broken_event_gets_an_error_and_unknown_event_is_acknowledged(fake, jobs):
    assert client.post(f"/api/webim/{SECRET}", content=b"not json").status_code == 400
    assert post({"event": "new_message", "message": {"kind": "visitor", "text": "без номера чата"}}).status_code == 400
    assert post({"event": "chat_closed", "chat_id": 1}).json() == {"result": "ok"} and jobs == []


def test_webim_dialog_cannot_be_opened_from_the_site_chat_by_the_chat_number(fake, jobs, monkeypatch):
    """Зачем: номера чатов Webim идут подряд; по номеру чужой диалог не читается и не попадает в память визита."""
    model(monkeypatch, Scripted(["Здравствуйте."]))
    post(new_message(9016, visitor("Мы ООО Ромашка")))
    settle(jobs)
    guess = "webim-9016"
    assert site.get(f"/api/session/{guess}", params={"client_id": guess}).status_code == 404
    assert store.previous_session(guess, "") is None
    mine = webim.session_id(9016)
    monkeypatch.setattr(config, "WEBIM_TOKEN", "other-bot")
    assert webim.session_id(9016) != mine  # подпись это ключ бота, а не что-то известное по номеру


def _server_answer(env: dict) -> str:
    code = ("from fastapi.testclient import TestClient\nfrom app.web import server\n"
            "r = TestClient(server.app).post('/api/webim/s1', json={'event': 'chat_closed'})\n"
            "print(r.status_code, r.text)")
    return subprocess.run([sys.executable, "-c", code], cwd=config.ROOT, env=env, capture_output=True, text=True,
                          timeout=120).stdout.strip()


def test_server_mounts_the_adapter_only_with_a_bot_token(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEBIM_")}
    env.update(APP_DB=str(tmp_path / "app.sqlite"), QUOTES_DIR=str(tmp_path / "quotes"))
    assert _server_answer({**env, "WEBIM_TOKEN": "t", "WEBIM_SECRET": "s1"}) == '200 {"result":"ok"}'
    assert _server_answer(env).startswith("404")


def test_review_mode_sends_the_review_note_then_the_checked_answer(fake, jobs, monkeypatch):
    """Режим "ответ проверяет менеджер": посетитель Webim сначала получает срок проверки, ответ агента уходит в окно
    Webim после "Отправить", текст менеджера после "Править"."""
    monkeypatch.setitem(server.DELIVERY, "webim-", webim.deliver)
    store.set_setting(server.REVIEW, "1")
    try:
        reply = "Kaspersky на 10 ПК подберу по сроку: на год или на два?"
        model(monkeypatch, Scripted([reply, "Второй ответ агента."]))
        post(new_message(9101, visitor("Сколько стоит Kaspersky на 10 ПК")))
        settle(jobs)
        (method, note), = sent(fake, 9101)
        assert method == "send_message" and "проверяет менеджер" in note["text"] and reply not in note["text"]
        sid = webim.session_id(9101)
        draft = store.drafts("d.session_id=? AND d.status=?", (sid, "new"))[-1]
        assert server.review_draft(draft["id"], "send")["ok"]
        assert sent(fake, 9101)[-1] == ("send_message", {"kind": "operator", "text": reply})
        post(new_message(9101, visitor("А на два года?")))
        settle(jobs)
        draft = store.drafts("d.session_id=? AND d.status=?", (sid, "new"))[-1]
        assert server.review_draft(draft["id"], "edit", "На два года выгоднее, пришлю расчет.")["ok"]
        assert sent(fake, 9101)[-1] == ("send_message", {"kind": "operator", "text": "На два года выгоднее, пришлю расчет."})
    finally:
        store.set_setting(server.REVIEW, "0")
