"""Эмулятор Webim: адаптер app/webim.py в работе без живого аккаунта Webim.

Поднимает поддельный API бота Webim (send_message, redirect_chat, close_chat по адресам /api/bot/v2/...), шлет
события External Bot API 2.0 в запущенный экземпляр и печатает по строкам: событие -> ответ агента или перевод
чата. Поддельный Webim проверяет токен и форму вызовов по документации, а черновик КП скачивает по присланной
ссылке, как настоящий.

Экземпляр запускается с адресом эмулятора вместо Webim (токен и секрет любые, одинаковые в обеих командах):
  WEBIM_TOKEN=demo WEBIM_SECRET=s3cret WEBIM_API_URL=http://127.0.0.1:8099 WEBIM_DEP_KEY=sales \\
      uvicorn app.web.server:app --port 8053
  python -m eval.webim_emulator --base http://127.0.0.1:8053 --token demo --secret s3cret --port 8099

Код выхода 1, если какой-то шаг кончился не так, как ожидалось.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

METHODS = ("send_message", "redirect_chat", "close_chat")
BUTTON_ID = re.compile(r"[A-Za-z0-9_-]{1,24}")  # ограничение Webim на id кнопки


def problem(method: str, p: dict) -> str:
    """Код ошибки, которым Webim ответил бы на такой вызов, или пустая строка. Правила из документации API бота."""
    if not isinstance(p.get("chat_id"), int):
        return "incorrect-request"
    if method == "redirect_chat":
        return "incorrect-request" if "operator_id" in p and "dep_key" in p else ""
    if method != "send_message":
        return ""
    m = p.get("message")
    if not isinstance(m, dict) or m.get("kind") not in ("operator", "file_operator", "keyboard"):
        return "incorrect-request"
    if m["kind"] == "operator" and not str(m.get("text") or "").strip():
        return "incorrect-request"
    if m["kind"] == "file_operator" and not all((m.get("data") or {}).get(k) for k in ("url", "name", "media_type")):
        return "incorrect-request"
    if m["kind"] == "keyboard":
        rows = m.get("buttons")
        ok = isinstance(rows, list) and rows and all(isinstance(r, list) and r for r in rows) and all(
            BUTTON_ID.fullmatch(str(b.get("id"))) and str(b.get("text") or "").strip() for r in rows for b in r)
        return "" if ok else "incorrect-buttons"
    return ""


class FakeWebim:
    """Поддельный API бота Webim: проверяет токен и форму вызова, записывает вызовы, отвечает как Webim.

    reply(method, payload) задает ответ на правильный вызов: по умолчанию успех. fetch_files: скачивать файлы
    из send_message по ссылке, как это делает Webim.
    """

    def __init__(self, token: str, port: int = 0, fetch_files: bool = False):
        self.token, self.fetch_files = token, fetch_files
        self.calls: list[dict] = []
        self.reply = lambda method, payload: {"result": "ok"}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                method = self.path.removeprefix("/api/bot/v2/")
                if method not in METHODS:
                    return self._answer(400, {"error": "method-not-found"})
                if self.headers.get("Authorization") != f"Token {fake.token}":
                    return self._answer(403, {"error": "unauthorized"})
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                except ValueError:
                    return self._answer(400, {"error": "incorrect-request"})
                error = problem(method, payload) if isinstance(payload, dict) else "incorrect-request"
                if error:
                    return self._answer(400, {"error": error})
                fake.record(method, payload)
                self._answer(200, fake.reply(method, payload))

            def _answer(self, code: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def record(self, method: str, payload: dict):
        call = {"method": method, "payload": payload, "id": uuid.uuid4().hex}
        data = (payload.get("message") or {}).get("data") or {}
        if self.fetch_files and data.get("url"):
            try:
                r = httpx.get(data["url"], timeout=30)
                kind, size = r.headers.get("content-type", ""), len(r.content) // 1024
                call["file"] = f"скачан: {r.status_code} {kind}, {size} КБ"
            except httpx.HTTPError as e:
                call["file"] = f"не скачан: {e}"
        with self._lock:
            self.calls.append(call)

    def of_chat(self, chat_id: int) -> list[dict]:
        with self._lock:
            return [c for c in self.calls if c["payload"].get("chat_id") == chat_id]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def visitor(text: str) -> dict:
    return {"id": uuid.uuid4().hex, "kind": "visitor", "text": text}


def new_chat(chat_id: int, *messages: dict) -> dict:
    """Чат назначен боту: с сообщениями, которые посетитель успел написать. channel null это виджет на сайте."""
    return {"event": "new_chat", "chat": {"id": chat_id}, "visitor": {"id": uuid.uuid4().hex, "channel": None},
            "messages": list(messages)}


def new_message(chat_id: int, message: dict) -> dict:
    return {"event": "new_message", "chat_id": chat_id, "message": message}


def pressed(button: dict, keyboard_id: str) -> dict:
    """Посетитель нажал кнопку клавиатуры, которую прислал бот."""
    return {"id": uuid.uuid4().hex, "kind": "keyboard_response",
            "data": {"button": button, "request": {"messageId": keyboard_id}}}


def show(call: dict) -> str:
    """Вызов бота одной строкой для консоли."""
    p = call["payload"]
    if call["method"] == "redirect_chat":
        return "redirect_chat: " + (f"в отдел {p['dep_key']}" if p.get("dep_key") else "в общую очередь операторов")
    if call["method"] != "send_message":
        return call["method"]
    m = p["message"]
    if m["kind"] == "keyboard":
        return "кнопки: " + " | ".join(b["text"] for row in m["buttons"] for b in row)
    if m["kind"] == "file_operator":
        d = m["data"]
        return f"файл {d['name']} ({d['media_type']})" + (f", {call['file']}" if call.get("file") else "")
    text = " / ".join(line for line in m["text"].splitlines() if line.strip())
    return "ответ агента: " + (text if len(text) <= 400 else text[:400] + "...")


def scenario(fake: FakeWebim, first: int):
    """Шаги показа: (подпись, номер чата, событие, чего ждать). Ждать: "ответ", "ничего", "перевод", "файл"."""
    price = new_chat(first, visitor("Сколько стоит Kaspersky Endpoint Security на 10 ПК?"))
    yield "новый чат", first, price, "ответ"
    yield "повтор доставки того же события", first, price, "ничего"
    yield "новое сообщение", first, new_message(first, visitor("Сделайте скидку 15%, тогда возьмем")), "перевод"
    quote = "Подготовьте КП на Kaspersky Endpoint Security Стандартный на 10 ПК на год, компания ООО Ромашка"
    yield "новый чат", first + 1, new_chat(first + 1, visitor(quote)), "файл"
    yield "новый чат", first + 2, new_chat(first + 2, visitor("Нужен антивирус для офиса")), "ответ"
    keyboards = [c for c in fake.of_chat(first + 2) if c["payload"].get("message", {}).get("kind") == "keyboard"]
    manager = {"id": "manager", "text": "Позвать менеджера"}
    yield ("кнопка", first + 2, new_message(first + 2, pressed(manager, keyboards[-1]["id"] if keyboards else "")),
           "перевод")


def met(expect: str, calls: list[dict]) -> bool:
    kinds = [c["payload"].get("message", {}).get("kind", c["method"]) for c in calls]
    return {"ничего": not calls, "ответ": "operator" in kinds, "перевод": "redirect_chat" in kinds,
            "файл": "file_operator" in kinds}[expect]


def said(event: dict) -> str:
    if event["event"] == "new_chat":
        return " / ".join(m["text"] for m in event["messages"])
    m = event["message"]
    return m.get("text") or "нажата кнопка " + m["data"]["button"]["text"]


def run(base: str, secret: str, fake: FakeWebim, first: int, wait: float, quiet: float) -> int:
    """Проигрывает шаги показа и печатает их. Возвращает число шагов, которые кончились не так, как ожидалось."""
    url, bad = f"{base.rstrip('/')}/api/webim/{secret}", 0
    for n, (title, chat_id, event, expect) in enumerate(scenario(fake, first), 1):
        before = len(fake.of_chat(chat_id))
        started = time.monotonic()
        r = httpx.post(url, json=event, headers={"X-Bot-API-Dialect": "Webim Standard", "X-Bot-API-Version": "2.0"},
                       timeout=30)
        print(f"[{n}] {event['event']}, чат {chat_id}, {title}: {said(event)}")
        print(f"    бот подтвердил прием: {r.status_code} {r.text} за {(time.monotonic() - started) * 1000:.0f} мс")
        until, last = time.monotonic() + (quiet * 2 if expect == "ничего" else wait), None
        while time.monotonic() < until:  # ждем первый вызов, потом тишину: ход агента кончился
            count = len(fake.of_chat(chat_id))
            if count > before and last is None:
                last = (count, time.monotonic())
            elif last and count != last[0]:
                last = (count, time.monotonic())
            elif last and time.monotonic() - last[1] >= quiet:
                break
            time.sleep(0.1)
        calls = fake.of_chat(chat_id)[before:]
        for c in calls:
            print("    -> " + show(c))
        if not calls:
            print("    -> вызовов нет" + (": второй ход не запускался" if expect == "ничего" else ""))
        ok = met(expect, calls)
        bad += not ok
        print(f"    [{'ок' if ok else 'не так'}: ждали {expect}, через {time.monotonic() - started:.1f} с]")
    return bad


def main():
    ap = argparse.ArgumentParser(description="Эмулятор Webim для адаптера app/webim.py")
    ap.add_argument("--base", required=True, help="адрес запущенного экземпляра, например http://127.0.0.1:8053")
    ap.add_argument("--secret", required=True, help="WEBIM_SECRET экземпляра")
    ap.add_argument("--token", default="demo", help="WEBIM_TOKEN экземпляра")
    ap.add_argument("--port", type=int, default=8099, help="порт поддельного Webim: WEBIM_API_URL экземпляра")
    ap.add_argument("--first-chat", type=int, default=int(time.time()) % 10_000_000 * 10,
                    help="номер первого чата; по умолчанию новый на каждый запуск")
    ap.add_argument("--wait", type=float, default=180, help="секунд ждать ответа агента на событие")
    ap.add_argument("--quiet", type=float, default=2, help="секунд без вызовов: ход агента кончился")
    args = ap.parse_args()
    fake = FakeWebim(args.token, args.port, fetch_files=True)
    print(f"Поддельный Webim: {fake.url}, события идут на {args.base.rstrip('/')}/api/webim/<секрет>")
    try:
        bad = run(args.base, args.secret, fake, args.first_chat, args.wait, args.quiet)
    finally:
        fake.close()
    print("Итог: все шаги как ожидалось" if not bad else f"Итог: не так шагов: {bad}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
