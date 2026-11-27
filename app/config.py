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
# кейсы, которые оператор добавил в проверочный набор со стенда: в data, чтобы пережить пересборку контейнера
CASES_ADDED = Path(env("CASES_ADDED", str(ROOT / "data/cases_added.jsonl")))

# Модель: gigachat | openai (любой OpenAI-совместимый сервис, в том числе on-prem через vLLM) |
# bridge (разработка без ключа: запросы к модели идут файлами через BRIDGE_DIR, см. app/agent/bridge.py)
LLM_PROVIDER = env("LLM_PROVIDER", "gigachat")
LLM_MODEL = env("LLM_MODEL", "GigaChat-2-Max")
LLM_LABEL = env("LLM_LABEL", "") or LLM_MODEL  # имя модели для экрана: чат и экран оператора показывают его
LLM_BASE_URL = env("LLM_BASE_URL", "")
LLM_API_KEY = env("LLM_API_KEY", "")
GIGACHAT_AUTH_KEY = env("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
BRIDGE_DIR = env("BRIDGE_DIR", "")
BRIDGE_TIMEOUT = float(env("BRIDGE_TIMEOUT", "1800"))  # сколько секунд ждать ответа исполнителя
# Мост на стенде: исполнитель дежурит и отмечается в каталоге моста. Отметка старше этого числа секунд значит,
# что его нет: запрос не ждет, клиент сразу получает ответ о передаче менеджеру. 0 = отметку не проверять
BRIDGE_ALIVE_SEC = float(env("BRIDGE_ALIVE_SEC", "0"))

# Цена модели в рублях за 1000 токенов (вход, выход): из нее считается стоимость запроса
PRICE_IN_PER_1K = float(env("PRICE_IN_PER_1K", "0.65"))
PRICE_OUT_PER_1K = float(env("PRICE_OUT_PER_1K", "0.65"))

# Нагрузка и расход на публичном адресе
MAX_PARALLEL_TURNS = int(env("MAX_PARALLEL_TURNS", "4"))  # ходов одновременно; GigaChat для физлиц держит 1 поток
TURN_DEADLINE = float(env("TURN_DEADLINE", "60"))         # секунд на один ход клиента, 0 = без предела
CHAT_RATE_PER_MIN = int(env("CHAT_RATE_PER_MIN", "10"))   # сообщений в минуту с одного адреса, 0 = без предела
DAILY_BUDGET_RUB = float(env("DAILY_BUDGET_RUB", "500"))  # потолок расхода на модель за сутки, 0 = без потолка
RETENTION_DAYS = int(env("RETENTION_DAYS", "90"))         # сколько дней хранятся диалоги с контактами клиентов
PREVIOUS_VISIT_DAYS = int(env("PREVIOUS_VISIT_DAYS", "30"))  # прошлый визит старше этого агент не вспоминает

# Фид каталога: сервер каждые FEED_UPDATE_EVERY_H часов скачивает его и пересобирает базу (app/catalog/update.py);
# пусто = не обновлять. Заказчик: фид магазина обновляется несколько раз в сутки
FEED_URL = env("FEED_URL", "https://store.softline.ru/feeds/Softline.yml.gz")
FEED_UPDATE_EVERY_H = max(1, int(env("FEED_UPDATE_EVERY_H", "3")))  # точки сетки от полуночи по Москве: 0, 3, 6, ...

# Ключ оператора. С ним экран оператора (/admin?token=...) показывает контакты клиентов и дает отвечать,
# выгружать журнал и обновлять каталог. Без ключа экран открыт только на чтение, контакты в нем скрыты.
ADMIN_TOKEN = env("ADMIN_TOKEN", "")

# Экономика на экране оператора. Первые три числа это допущения: их меняют под свои данные прямо на экране.
MANAGER_MIN_PER_REQUEST = float(env("MANAGER_MIN_PER_REQUEST", "30"))  # минут менеджера на обращение: оценка заказчика на брифе
MANAGER_RUB_PER_HOUR = float(env("MANAGER_RUB_PER_HOUR", "600"))        # стоимость часа менеджера для компании
CARD_SAVES_SHARE = float(env("CARD_SAVES_SHARE", "0.5"))  # какую долю времени снимает готовая карточка обращения
REQUESTS_PER_HALF_YEAR = int(env("REQUESTS_PER_HALF_YEAR", "500"))      # объем обращений: цифра заказчика
# Подписант КП: имя, должность и подпись под чертой. По умолчанию нейтральные, настоящие из шаблона заказчика в .env
KP_SIGNER_NAME = env("KP_SIGNER_NAME", "Менеджер Softline Store")
KP_SIGNER_TITLE = env("KP_SIGNER_TITLE", "Отдел продаж")
KP_SIGNER_SHORT = env("KP_SIGNER_SHORT", "Менеджер")
AGENT_DISCOUNT_MAX = min(5.0, max(0.0, float(env("AGENT_DISCOUNT_MAX", "5"))))  # скидка, которую агент дает сам, проценты: ответ
# заказчика на брифе ("агент может предоставить скидку 5%, все остальное уходит на оператора"); 0 = не дает
# Проверка "цена у своей позиции": цена одной позиции каталога, названная у другой, вырезается как чужая сумма.
# 0 выключает ее, если на живых диалогах она начнет резать верные ответы; остальные проверки цен остаются
PRICE_BINDING = env("PRICE_BINDING", "1") == "1"
SLA_MINUTES = env("SLA_MINUTES", "2,5,15")  # срок реакции менеджера на обращение: срочное, обычное, прочее.
# Историческое среднее у заказчика — 2 минуты от запроса клиента до первого видимого сообщения менеджера, включая уточнение. Это не срок SLA.
WORKDAYS_PER_MONTH = int(env("WORKDAYS_PER_MONTH", "21"))  # для расчета расхода на модель в день
STAND_NOTE = env("STAND_NOTE", "")  # пояснение на экране оператора: что за диалоги лежат на этом стенде
MODEL_BUDGET_HALF_YEAR_RUB = float(env("MODEL_BUDGET_HALF_YEAR_RUB", "7000"))  # расход на модель за полгода, который
# заказчик назвал приемлемым (5-7 тыс. руб.): верхняя граница; 0 = не показывать

# Сколько своих прокси стоит перед сервером (стенд за одним Caddy). Адрес клиента для лимита частоты берется
# из X-Forwarded-For с конца: начало списка задает сам клиент. 0 = прокси нет, берется адрес соединения
PROXY_HOPS = int(env("PROXY_HOPS", "1"))

# Обращения менеджеру в Telegram (app/notify.py): ключ бота и номер чата менеджеров. Пусто = только экран оператора
TG_TOKEN = env("TG_TOKEN", "")
TG_CHAT = env("TG_CHAT", "")
PUBLIC_URL = env("PUBLIC_URL", "")  # адрес стенда: кнопка "Экран оператора" под карточкой обращения в Telegram

# Чат сайта в Webim (app/webim.py, docs/webim.md): агент отвечает посетителю в окне Webim как бот, External Bot API 2.0.
# WEBIM_TOKEN: токен бота из кабинета Webim, пусто = адаптер выключен. WEBIM_SECRET: окончание адреса бота, который
# указывается в кабинете: https://<адрес стенда>/api/webim/<секрет>. WEBIM_API_URL: адрес аккаунта Webim.
# WEBIM_DEP_KEY: ключ отдела, куда агент переводит чат при передаче менеджеру; пусто = общая очередь операторов.
# Черновик КП Webim скачивает по ссылке от PUBLIC_URL: без него КП в Webim не уходит
WEBIM_TOKEN = env("WEBIM_TOKEN", "")
WEBIM_SECRET = env("WEBIM_SECRET", "")
WEBIM_API_URL = env("WEBIM_API_URL", "https://storesoftlineru.webim.ru")
WEBIM_DEP_KEY = env("WEBIM_DEP_KEY", "")

# Описание продукта с его страницы в магазине (app/catalog/cards.py): агент получает его вместе с вариантами лицензий
CARDS_DB = Path(env("CARDS_DB", str(ROOT / "data/db/cards.sqlite")))
PRODUCT_CARDS = env("PRODUCT_CARDS", "1") == "1"  # 0 = описаний не запрашивать, агент работает только по фиду
ABOUT_LIMIT = int(env("ABOUT_LIMIT", "1200"))     # сколько знаков описания отдавать модели
