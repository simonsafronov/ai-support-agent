"""Лимит хода и клиент модели: ожидание и повторы запроса укладываются в остаток хода. Без сети: подставной httpx."""
import httpx
import pytest

from app.agent import llm as llm_mod, loop
from app.agent.llm import LLMError, OpenAICompat, Scripted

pytestmark = pytest.mark.small_catalog

ANSWER = {"choices": [{"message": {"content": "Готово."}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class Clock:
    """Часы теста: время идет, только когда идет запрос (seconds на ответ) или пауза перед повтором."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(llm_mod.time, "monotonic", c.monotonic)
    monkeypatch.setattr(llm_mod.time, "sleep", c.sleep)
    return c


def model(clock, status: int = 200, seconds: float = 0.0) -> tuple[OpenAICompat, list]:
    """Клиент OpenAI-совместимого API с подставным транспортом; seen: таймауты каждого запроса."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        wait = min(seconds, request.extensions["timeout"]["read"])  # как настоящий httpx: ждет не дольше таймаута
        clock.now += wait
        if wait < seconds:
            raise httpx.ReadTimeout("нет ответа", request=request)
        return httpx.Response(status, json=ANSWER if status == 200 else {"error": "busy"})

    client = OpenAICompat("http://model.test/v1", "", "m")
    client.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(90, connect=15))
    return client, seen


def test_request_waits_no_longer_than_the_rest_of_the_turn(clock):
    client, seen = model(clock)
    assert client.chat([{"role": "user", "content": "x"}], [], time_left=20).text == "Готово."
    assert seen[-1]["read"] == 20 and seen[-1]["connect"] == 15
    client.chat([{"role": "user", "content": "x"}], [], time_left=1.5)  # остаток меньше 5 с: ждем 5 с
    assert seen[-1]["read"] == 5 and seen[-1]["connect"] == 5
    client.chat([{"role": "user", "content": "x"}], [], time_left=500)  # больше обычного предела не ждем
    assert seen[-1]["read"] == 90


def test_without_turn_limit_the_client_behaves_as_before(clock):
    client, seen = model(clock, status=503, seconds=30)
    with pytest.raises(LLMError, match="503"):
        client.chat([{"role": "user", "content": "x"}], [])
    assert len(seen) == 3 and all(t["read"] == 90 for t in seen)  # три попытки с обычным таймаутом


def test_retries_stop_when_the_turn_time_is_over(clock):
    """Зачем: лимит хода 60 с проверялся только перед вызовом модели, а запрос ждал до 90 с и повторялся."""
    client, seen = model(clock, status=503, seconds=30)
    with pytest.raises(LLMError, match="сеть"):
        client.chat([{"role": "user", "content": "x"}], [], time_left=40)
    assert [t["read"] for t in seen] == [40, 8.5]  # второй запрос ждет только остаток: 40 - 30 - пауза 1.5
    assert clock.now == 40  # третьей попытки нет: срок хода вышел


def test_network_error_retry_also_respects_the_turn_time(clock):
    calls = []

    def handler(request):
        calls.append(request)
        clock.now += 10
        raise httpx.ReadTimeout("медленно", request=request)

    client = OpenAICompat("http://model.test/v1", "", "m")
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="сеть"):
        client.chat([{"role": "user", "content": "x"}], [], time_left=5)
    assert len(calls) == 1


class Recording(Scripted):
    def __init__(self, steps):
        super().__init__(steps)
        self.time_left = []

    def chat(self, messages, tools, **kw):
        self.time_left.append(kw.get("time_left"))
        return super().chat(messages, tools, **kw)


def test_loop_passes_the_rest_of_the_turn_to_the_model(monkeypatch):
    monkeypatch.setattr(loop.config, "TURN_DEADLINE", 60.0)
    model_ = Recording([{"name": "kb_search", "args": {"query": "оплата"}}, "Готово."])
    loop.run_turn(None, "Как оплатить?", llm=model_)
    first, second = model_.time_left
    assert 0 < second <= first <= 60
    monkeypatch.setattr(loop.config, "TURN_DEADLINE", 0)  # без предела хода: как раньше
    model_ = Recording(["Готово."])
    loop.run_turn(None, "Здравствуйте", llm=model_)
    assert model_.time_left == [None]
