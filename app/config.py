"""Настройки из окружения и файла .env в корне проекта."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


CATALOG_DB = Path(env("CATALOG_DB", str(ROOT / "data/db/catalog.sqlite")))
KB_DB = Path(env("KB_DB", str(ROOT / "data/db/kb.sqlite")))
APP_DB = Path(env("APP_DB", str(ROOT / "data/db/app.sqlite")))
QUOTES_DIR = Path(env("QUOTES_DIR", str(ROOT / "data/quotes")))

# Модель: gigachat | openai (любой OpenAI-совместимый сервис, в том числе on-prem через vLLM) |
# bridge (разработка без ключа: запросы к модели идут файлами через BRIDGE_DIR, см. app/agent/bridge.py)
LLM_PROVIDER = env("LLM_PROVIDER", "gigachat")
LLM_MODEL = env("LLM_MODEL", "GigaChat-2-Max")
LLM_BASE_URL = env("LLM_BASE_URL", "")
LLM_API_KEY = env("LLM_API_KEY", "")
GIGACHAT_AUTH_KEY = env("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
BRIDGE_DIR = env("BRIDGE_DIR", "")
BRIDGE_TIMEOUT = float(env("BRIDGE_TIMEOUT", "1800"))  # сколько секунд ждать ответа исполнителя

# Цена модели в рублях за 1000 токенов (вход, выход): из нее считается стоимость запроса
PRICE_IN_PER_1K = float(env("PRICE_IN_PER_1K", "0.65"))
PRICE_OUT_PER_1K = float(env("PRICE_OUT_PER_1K", "0.65"))

# Фид каталога: сервер раз в сутки скачивает его и пересобирает базу (app/catalog/update.py); пусто = не обновлять
FEED_URL = env("FEED_URL", "https://store.softline.ru/feeds/Softline.yml.gz")
FEED_UPDATE_HOUR = int(env("FEED_UPDATE_HOUR", "4"))  # час по Москве

ADMIN_TOKEN = env("ADMIN_TOKEN", "")  # задан: экран оператора открывается по адресу /admin?token=...
