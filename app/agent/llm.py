"""Единый интерфейс к языковой модели.

Сообщения внутри приложения хранятся в одном формате:
  {"role": "system" | "user" | "assistant" | "tool", "content": str,
   "tool_call": {"name": str, "args": dict} (у assistant), "name": str (у tool)}
Провайдер переводит их в свой формат и обратно. За один ход модель делает не больше одного вызова
инструмента: так работает GigaChat, и так проще проверять каждый шаг.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app import config


@dataclass
class LLMResult:
    text: str = ""
    tool_call: dict | None = None  # {"name": ..., "args": {...}}
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    seconds: float = 0.0
    extra: dict = field(default_factory=dict)  # служебные поля провайдера, которые надо вернуть в историю


class LLMError(RuntimeError):
    pass


def _post(client: httpx.Client, url: str, **kw) -> dict:
    last = None
    for attempt in range(3):
        try:
            r = client.post(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = LLMError(f"{r.status_code}: {r.text[:300]}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{r.status_code}: {r.text[:500]}")
            return r.json()
        except httpx.HTTPError as e:
            last = LLMError(f"сеть: {e}")
            time.sleep(1.5 * (attempt + 1))
    raise last


class GigaChat:
    """GigaChat API: ключ авторизации меняется на токен на 30 минут, функции в формате functions/function_call."""

    AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"

    def __init__(self, auth_key: str, model: str, scope: str):
        if not auth_key:
            raise LLMError("не задан GIGACHAT_AUTH_KEY")
        self.auth_key, self.model, self.scope = auth_key, model, scope
        ca = config.ROOT / "certs" / "russian_trusted_root_ca.pem"
        self.client = httpx.Client(verify=str(ca), timeout=httpx.Timeout(90, connect=15))
        self._token, self._exp, self._lock = "", 0.0, threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            if time.time() < self._exp - 60:
                return self._token
            data = _post(
                self.client, self.AUTH_URL,
                headers={"Authorization": f"Basic {self.auth_key}", "RqUID": str(uuid.uuid4()),
                         "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={"scope": self.scope},
            )
            self._token, self._exp = data["access_token"], data["expires_at"] / 1000
            return self._token

    @staticmethod
    def _to_wire(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "tool":
                out.append({"role": "function", "name": m["name"], "content": m["content"]})
            elif m["role"] == "assistant" and m.get("tool_call"):
                w = {"role": "assistant", "content": m.get("content") or "",
                     "function_call": {"name": m["tool_call"]["name"], "arguments": m["tool_call"]["args"]}}
                if m.get("extra", {}).get("functions_state_id"):
                    w["functions_state_id"] = m["extra"]["functions_state_id"]
                out.append(w)
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages: list[dict], tools: list[dict], temperature: float = 0.1,
             max_tokens: int = 1200) -> LLMResult:
        body = {"model": self.model, "messages": self._to_wire(messages), "temperature": temperature,
                "max_tokens": max_tokens}
        if tools:
            body["functions"] = [{"name": t["name"], "description": t["description"], "parameters": t["parameters"],
                                  **({"few_shot_examples": t["examples"]} if t.get("examples") else {})}
                                 for t in tools]
            body["function_call"] = "auto"
        t0 = time.time()
        data = _post(self.client, f"{self.BASE_URL}/chat/completions",
                     headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"},
                     json=body)
        msg, usage = data["choices"][0]["message"], data.get("usage", {})
        res = LLMResult(text=(msg.get("content") or "").strip(), model=data.get("model", self.model),
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0), seconds=time.time() - t0)
        fc = msg.get("function_call")
        if fc:
            args = fc.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            res.tool_call = {"name": fc["name"], "args": args}
            if msg.get("functions_state_id"):
                res.extra["functions_state_id"] = msg["functions_state_id"]
        return res


class OpenAICompat:
    """Любой сервис с интерфейсом OpenAI chat/completions и tools: YandexGPT, vLLM в контуре заказчика и т.п."""

    def __init__(self, base_url: str, api_key: str, model: str):
        if not base_url:
            raise LLMError("не задан LLM_BASE_URL")
        self.base_url, self.model = base_url.rstrip("/"), model
        self.client = httpx.Client(timeout=httpx.Timeout(90, connect=15),
                                   headers={"Authorization": f"Bearer {api_key}"} if api_key else {})

    @staticmethod
    def _to_wire(messages: list[dict]) -> list[dict]:
        out, last_id = [], ""
        for i, m in enumerate(messages):
            if m["role"] == "assistant" and m.get("tool_call"):
                last_id = f"call_{i}"
                out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [{
                    "id": last_id, "type": "function",
                    "function": {"name": m["tool_call"]["name"],
                                 "arguments": json.dumps(m["tool_call"]["args"], ensure_ascii=False)}}]})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": last_id, "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages: list[dict], tools: list[dict], temperature: float = 0.1,
             max_tokens: int = 1200) -> LLMResult:
        body = {"model": self.model, "messages": self._to_wire(messages), "temperature": temperature,
                "max_tokens": max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
            body["parallel_tool_calls"] = False
        t0 = time.time()
        data = _post(self.client, f"{self.base_url}/chat/completions", json=body)
        msg, usage = data["choices"][0]["message"], data.get("usage", {})
        res = LLMResult(text=(msg.get("content") or "").strip(), model=data.get("model", self.model),
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0), seconds=time.time() - t0)
        calls = msg.get("tool_calls") or []
        if calls:
            f = calls[0]["function"]
            res.tool_call = {"name": f["name"], "args": json.loads(f.get("arguments") or "{}")}
        return res


class Scripted:
    """Модель-сценарий для тестов: отдает заранее заданные ответы по порядку."""

    def __init__(self, steps: list[LLMResult | dict | str]):
        self.steps, self.calls = list(steps), []

    def chat(self, messages: list[dict], tools: list[dict], **_) -> LLMResult:
        self.calls.append(messages)
        step = self.steps.pop(0) if self.steps else "Готово."
        if isinstance(step, LLMResult):
            return step
        if isinstance(step, dict):
            return LLMResult(tool_call=step, prompt_tokens=100, completion_tokens=20, model="scripted")
        return LLMResult(text=step, prompt_tokens=100, completion_tokens=20, model="scripted")


class Bridge:
    """Режим разработки без ключа модели: запрос кладется файлом в каталог, ответ ждем файлом оттуда же.

    Отвечает внешний исполнитель через `python -m app.agent.bridge` (человек или модель, у которой нет
    HTTP-интерфейса). Токены считаются оценкой по числу знаков; время ответа скорость модели не показывает.
    """

    CHARS_PER_TOKEN = 3.0

    def __init__(self, directory: str, model: str, timeout: float = 1800):
        if not directory:
            raise LLMError("не задан BRIDGE_DIR")
        self.dir, self.model, self.timeout = Path(directory), model, timeout
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = len(list(self.dir.glob("req-*.json")))

    @classmethod
    def tokens(cls, text: str) -> int:
        return int(len(text) / cls.CHARS_PER_TOKEN) + 1

    def chat(self, messages: list[dict], tools: list[dict], **_) -> LLMResult:
        self.n += 1
        req, res = self.dir / f"req-{self.n:05d}.json", self.dir / f"res-{self.n:05d}.json"
        body = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
        tmp = req.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.rename(req)
        t0 = time.time()
        while not res.exists():
            if time.time() - t0 > self.timeout:
                raise LLMError(f"мост: нет ответа на {req.name} за {self.timeout:.0f} с")
            time.sleep(0.2)
        ans = json.loads(res.read_text(encoding="utf-8"))
        # seconds остается 0: ждали сессию разработчика, скорость модели это не показывает
        out = LLMResult(text=(ans.get("text") or "").strip(), model=self.model, seconds=0.0,
                        prompt_tokens=self.tokens(body),
                        completion_tokens=self.tokens(json.dumps(ans, ensure_ascii=False)))
        if ans.get("tool_call"):
            out.tool_call = {"name": ans["tool_call"]["name"], "args": ans["tool_call"].get("args") or {}}
        return out


_llm = None


def get_llm():
    """Модель по настройкам окружения, одна на процесс."""
    global _llm
    if _llm is None:
        if config.LLM_PROVIDER == "gigachat":
            _llm = GigaChat(config.GIGACHAT_AUTH_KEY, config.LLM_MODEL, config.GIGACHAT_SCOPE)
        elif config.LLM_PROVIDER == "openai":
            _llm = OpenAICompat(config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL)
        elif config.LLM_PROVIDER == "bridge":
            _llm = Bridge(config.BRIDGE_DIR, config.LLM_MODEL, config.BRIDGE_TIMEOUT)
        else:
            raise LLMError(f"неизвестный LLM_PROVIDER: {config.LLM_PROVIDER}")
    return _llm
