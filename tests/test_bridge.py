"""Мост разработки: запрос к модели уходит файлом, ответ приходит файлом."""
import json
import threading

from app.agent import bridge
from app.agent.llm import Bridge

TOOLS = [{"name": "search_products", "description": "поиск", "parameters": {"type": "object", "properties": {}}}]
SYSTEM = "Вы консультант. " * 200 + "\n\nКОНТЕКСТ\nСегодня 26.11.2026."


def ask(llm, messages, box):
    box.append(llm.chat(messages, TOOLS))


def run(tmp_path, messages):
    llm, box = Bridge(str(tmp_path), "test-model"), []
    t = threading.Thread(target=ask, args=(llm, messages, box))
    t.start()
    return t, box


def test_tool_call_round_trip(tmp_path):
    t, box = run(tmp_path, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "нужен антивирус"}])
    shown = bridge.show_next(tmp_path, wait=5)
    assert "нужен антивирус" in shown and "search_products" in shown and "Вы консультант" in shown
    call = {"tool_call": {"name": "search_products", "args": {"query": "антивирус"}}}
    out = bridge.answer(tmp_path, json.dumps(call), wait=0.5)
    t.join(5)
    assert box[0].tool_call == {"name": "search_products", "args": {"query": "антивирус"}}
    assert box[0].model == "test-model" and box[0].prompt_tokens > 1000
    assert "ЗАПРОСА ПОКА НЕТ" in out or "ЗАПРОС" in out


def test_second_request_shows_only_new_messages(tmp_path):
    first = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "нужен антивирус"}]
    t, _ = run(tmp_path, first)
    bridge.show_next(tmp_path, wait=5)
    bridge.answer(tmp_path, '{"tool_call": {"name": "search_products", "args": {}}}', wait=0.5)
    t.join(5)
    more = first + [{"role": "assistant", "content": "", "tool_call": {"name": "search_products", "args": {}}},
                    {"role": "tool", "name": "search_products", "content": '{"products": []}'}]
    llm, box = Bridge(str(tmp_path), "test-model"), []
    t = threading.Thread(target=ask, args=(llm, more, box))
    t.start()
    shown = bridge.show_next(tmp_path, wait=5)
    assert "НОВЫЕ СООБЩЕНИЯ ТОГО ЖЕ ХОДА" in shown and "без изменений" in shown
    assert "нужен антивирус" not in shown and '{"products": []}' in shown
    bridge.answer(tmp_path, '{"text": "В каталоге не нашел."}', wait=0.5)
    t.join(5)
    assert box[0].text == "В каталоге не нашел." and box[0].tool_call is None


def test_bad_answer_is_rejected_and_request_stays(tmp_path):
    t, box = run(tmp_path, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "привет"}])
    bridge.show_next(tmp_path, wait=5)
    assert "НЕ ПРИНЯТ" in bridge.answer(tmp_path, "не json", wait=0.5)
    assert "НЕ ПРИНЯТ" in bridge.answer(tmp_path, '{"tool_call": {"name": "delete_all", "args": {}}}', wait=0.5)
    assert "НЕ ПРИНЯТ" in bridge.answer(tmp_path, '{"text": " "}', wait=0.5)
    assert bridge.pending(tmp_path) is not None
    bridge.answer(tmp_path, '{"text": "Здравствуйте."}', wait=0.5)
    t.join(5)
    assert box[0].text == "Здравствуйте."


def test_done_marker_ends_the_session(tmp_path):
    (tmp_path / "DONE").touch()
    assert bridge.show_next(tmp_path, wait=1) == "ВСЕ ЗАПРОСЫ ОБРАБОТАНЫ"
