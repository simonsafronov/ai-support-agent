"""Описание продукта с карточки магазина: подгружается по запросу и хранится в своей базе.

Заказчик на брифе разрешил брать все, что есть на карточке товара. В фиде описания продукта нет: в поле
description там параметры лицензии. На странице продукта есть вкладка "Описание"; ее текст агент получает
в ответе get_product и по нему объясняет, для чего продукт и чем он отличается от соседних.

Страница запрашивается один раз на продукт, не чаще одного запроса в DELAY секунд. На отказ сайта (403, 429,
проверка на робота) запросы останавливаются на PAUSE секунд: агент продолжает работать по данным фида.

    python -m app.catalog.cards warm 300    # заранее подгрузить описания 300 продуктов с наибольшим выбором
"""
from __future__ import annotations

import html
import re
import sqlite3
import sys
import threading
import time

import httpx

from app import config

DELAY, PAUSE, TIMEOUT = 2.0, 1800.0, 8.0
KEEP, RETRY_EMPTY = 6000, 86400.0  # сколько знаков описания хранить; через сколько секунд повторить пустую карточку
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_ARTICLE = re.compile(r'<li[^>]*data-tab="description"[^>]*>.*?<article[^>]*>(.*?)</article>', re.S)
_BLOCK = re.compile(r"</(p|li|h[1-6]|tr|div)>|<br\s*/?>", re.I)
_lock, _state = threading.Lock(), {"last": 0.0, "paused_until": 0.0}


def _db() -> sqlite3.Connection:
    config.CARDS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.CARDS_DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS cards(product_id TEXT PRIMARY KEY, url TEXT, about TEXT, fetched_at REAL)")
    return con


def extract(page: str) -> str:
    """Текст вкладки "Описание" со страницы продукта: абзацы и пункты списков через перевод строки."""
    m = _ARTICLE.search(page)
    if not m:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", m.group(1), flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", _BLOCK.sub("\n", text))).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)[:KEEP]


def _fetch(url: str) -> str | None:
    """Страница продукта или None, если сайт не ответил; на отказ сайта ставит паузу для всех запросов."""
    with _lock:  # запросы к сайту магазина идут по одному и не чаще DELAY
        if time.time() < _state["paused_until"]:
            return None
        time.sleep(max(0.0, _state["last"] + DELAY - time.time()))
        _state["last"] = time.time()
        try:
            r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "ru"})
        except httpx.HTTPError:
            return None
        if r.status_code in (403, 429) or "cf-chl" in r.text[:5000]:
            _state["paused_until"] = time.time() + PAUSE
            return None
        return r.text if r.status_code == 200 else None


def about(product_id: str, url: str | None, limit: int | None = None, fetch: bool = True) -> str:
    """Описание продукта, обрезанное по границе предложения; пустая строка, если его нет или сайт недоступен."""
    if not config.PRODUCT_CARDS or not url or not url.startswith("https://store.softline.ru/"):
        return ""
    limit = limit or config.ABOUT_LIMIT
    con = _db()
    try:
        row = con.execute("SELECT about, fetched_at FROM cards WHERE product_id=?", (str(product_id),)).fetchone()
        if row is None or (not row[0] and time.time() - row[1] > RETRY_EMPTY):
            if not fetch:
                return ""
            page = _fetch(url)
            if page is None:  # сайт не ответил: в базу не пишем, попробуем при следующем обращении
                return row[0] if row else ""
            row = (extract(page), time.time())
            con.execute("INSERT OR REPLACE INTO cards(product_id, url, about, fetched_at) VALUES(?,?,?,?)",
                        (str(product_id), url, row[0], row[1]))
            con.commit()
    finally:
        con.close()
    return cut(row[0], limit)


def cut(text: str, limit: int) -> str:
    """Текст не длиннее limit, оборванный на конце предложения или строки."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind(". "), head.rfind(".\n"), head.rfind("\n"))
    return head[:end + 1].rstrip() if end > limit // 2 else head.rstrip()


def warm(top: int) -> dict:
    """Заранее подгружает описания продуктов с наибольшим числом предложений в продаже."""
    cat = sqlite3.connect(f"file:{config.CATALOG_DB}?mode=ro", uri=True)
    rows = cat.execute("SELECT product_id, sample_url FROM products WHERE sample_url LIKE 'https://store.softline.ru/%' "
                       "ORDER BY n_available DESC, n_offers DESC LIMIT ?", (top,)).fetchall()
    cat.close()
    done = {"products": len(rows), "with_text": 0}
    for pid, url in rows:
        if time.time() < _state["paused_until"]:
            done["stopped"] = "сайт отказал, запросы остановлены"
            break
        done["with_text"] += bool(about(pid, url))
    return done


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "warm":
        print(warm(int(sys.argv[2])))
    else:
        print(__doc__)
