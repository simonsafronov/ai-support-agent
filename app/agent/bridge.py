"""Ответчик для режима разработки LLM_PROVIDER=bridge: показывает очередной запрос к модели и принимает ответ.

  python -m app.agent.bridge next DIR             дождаться запроса без ответа и напечатать его
  python -m app.agent.bridge answer DIR < ответ   записать ответ на текущий запрос и напечатать следующий

Ответ это JSON в одном из двух видов:
  {"text": "ответ клиенту"}
  {"tool_call": {"name": "search_products", "args": {"query": "Kaspersky Endpoint Security"}}}

Чтобы исполнителю не читать одно и то же, системный промпт и список инструментов печатаются целиком один
раз, дальше только изменившаяся часть; внутри одного хода печатаются только новые сообщения.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

WAIT = 90  # сколько секунд один вызов ждет следующего запроса


def _state(d: Path) -> dict:
    f = d / "cursor.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"system": "", "tools": "", "messages": []}


def pending(d: Path) -> Path | None:
    """Самый ранний запрос, на который еще нет ответа."""
    for req in sorted(d.glob("req-*.json")):
        if not (d / req.name.replace("req-", "res-")).exists():
            return req
    return None


def _line(m: dict) -> str:
    if m["role"] == "assistant" and m.get("tool_call"):
        return f"assistant -> вызов {m['tool_call']['name']} {json.dumps(m['tool_call']['args'], ensure_ascii=False)}"
    if m["role"] == "tool":
        return f"tool {m['name']}: {m['content']}"
    return f"{m['role']}: {m['content']}"


def render(req: Path, state: dict) -> str:
    """Текст запроса для исполнителя; state запоминает уже показанное."""
    body = json.loads(req.read_text(encoding="utf-8"))
    system, messages = body["messages"][0]["content"], body["messages"][1:]
    tools = json.dumps(body["tools"], ensure_ascii=False)
    out = [f"=== ЗАПРОС {req.stem.split('-')[1]} ==="]

    old = state["system"]
    same = 0
    while same < min(len(old), len(system)) and old[same] == system[same]:
        same += 1
    if system == old:
        out.append("[СИСТЕМНЫЙ ПРОМПТ] без изменений")
    elif same > 2000:
        cut = system.rfind("\n", 0, same) + 1
        out.append(f"[СИСТЕМНЫЙ ПРОМПТ] первые {cut} знаков те же, дальше теперь так:\n{system[cut:]}")
    else:
        out.append("[СИСТЕМНЫЙ ПРОМПТ]\n" + system)

    if tools == state["tools"]:
        out.append("[ИНСТРУМЕНТЫ] те же: " + ", ".join(t["name"] for t in body["tools"]))
    else:
        out.append("[ИНСТРУМЕНТЫ] вызывать можно один за ответ\n" + tools)

    shown = state["messages"]
    if shown and messages[:len(shown)] == shown:
        out.append("[НОВЫЕ СООБЩЕНИЯ ТОГО ЖЕ ХОДА]")
        new = messages[len(shown):]
    else:
        out.append("[НОВЫЙ ХОД. СООБЩЕНИЯ ДИАЛОГА]")
        new = messages
    out += [_line(m) for m in new]
    state.update(system=system, tools=tools, messages=messages)
    return "\n".join(out)


def check(answer: dict, req: Path) -> str:
    """Пустая строка, если ответ годится; иначе что в нем не так."""
    names = {t["name"] for t in json.loads(req.read_text(encoding="utf-8"))["tools"]}
    call = answer.get("tool_call")
    if call:
        if call.get("name") not in names:
            return f"нет инструмента {call.get('name')!r}; есть: {', '.join(sorted(names))}"
        if not isinstance(call.get("args", {}), dict):
            return "args должен быть объектом"
        return ""
    if not (answer.get("text") or "").strip():
        return 'нужен {"text": "..."} или {"tool_call": {"name": "...", "args": {...}}}'
    return ""


def show_next(d: Path, wait: float = WAIT) -> str:
    t0 = time.time()
    while True:
        req = pending(d)
        if req:
            state = _state(d)
            text = render(req, state)
            (d / "cursor.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            return text
        if (d / "DONE").exists():
            return "ВСЕ ЗАПРОСЫ ОБРАБОТАНЫ"
        if time.time() - t0 > wait:
            return "ЗАПРОСА ПОКА НЕТ: вызовите next еще раз"
        time.sleep(0.3)


def answer(d: Path, raw: str, wait: float = WAIT) -> str:
    req = pending(d)
    if not req:
        return "НЕТ ЗАПРОСА БЕЗ ОТВЕТА\n" + show_next(d, wait)
    try:
        ans = json.loads(raw)
    except ValueError as e:
        return f"ОТВЕТ НЕ ПРИНЯТ: это не JSON ({e}). Повторите answer."
    err = check(ans, req)
    if err:
        return f"ОТВЕТ НЕ ПРИНЯТ: {err}. Повторите answer."
    res = d / req.name.replace("req-", "res-")
    tmp = res.with_suffix(".tmp")
    tmp.write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")
    tmp.rename(res)
    return show_next(d, wait)


def main():
    cmd, d = sys.argv[1], Path(sys.argv[2])
    print(show_next(d) if cmd == "next" else answer(d, sys.stdin.read()))


if __name__ == "__main__":
    main()
