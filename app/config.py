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

# Нагрузка и расход на публичном адресе
MAX_PARALLEL_TURNS = int(env("MAX_PARALLEL_TURNS", "4"))  # ходов одновременно; GigaChat для физлиц держит 1 поток
TURN_DEADLINE = float(env("TURN_DEADLINE", "60"))         # секунд на один ход клиента, 0 = без предела
CHAT_RATE_PER_MIN = int(env("CHAT_RATE_PER_MIN", "10"))   # сообщений в минуту с одного адреса, 0 = без предела
DAILY_BUDGET_RUB = float(env("DAILY_BUDGET_RUB", "0"))    # потолок расхода на модель за сутки, 0 = без потолка
RETENTION_DAYS = int(env("RETENTION_DAYS", "90"))         # сколько дней хранятся диалоги с контактами клиентов
PREVIOUS_VISIT_DAYS = int(env("PREVIOUS_VISIT_DAYS", "30"))  # прошлый визит старше этого агент не вспоминает

# Фид каталога: сервер раз в сутки скачивает его и пересобирает базу (app/catalog/update.py); пусто = не обновлять
FEED_URL = env("FEED_URL", "https://store.softline.ru/feeds/Softline.yml.gz")
FEED_UPDATE_HOUR = int(env("FEED_UPDATE_HOUR", "4"))  # час по Москве

# Ключ оператора. С ним экран оператора (/admin?token=...) показывает контакты клиентов и дает отвечать,
# выгружать журнал и обновлять каталог. Без ключа экран открыт только на чтение, контакты в нем скрыты.
ADMIN_TOKEN = env("ADMIN_TOKEN", "")

# Экономика на экране оператора. Первые три числа это допущения: их меняют под свои данные прямо на экране.
MANAGER_MIN_PER_REQUEST = float(env("MANAGER_MIN_PER_REQUEST", "10"))  # минут менеджера на одно обращение
MANAGER_RUB_PER_HOUR = float(env("MANAGER_RUB_PER_HOUR", "600"))        # стоимость часа менеджера для компании
CARD_SAVES_SHARE = float(env("CARD_SAVES_SHARE", "0.5"))  # какую долю времени снимает готовая карточка обращения
REQUESTS_PER_HALF_YEAR = int(env("REQUESTS_PER_HALF_YEAR", "500"))      # объем обращений: цифра заказчика
MODEL_BUDGET_HALF_YEAR_RUB = float(env("MODEL_BUDGET_HALF_YEAR_RUB", "7000"))  # расход на модель за полгода, который
# заказчик назвал приемлемым (5-7 тыс. руб.): верхняя граница; 0 = не показывать
