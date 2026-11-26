"""Обновление каталога с фида магазина без остановки чата.

Новая база собирается рядом с рабочей, проверяется и подменяет ее одним переименованием: поиск
открывает новый файл на следующем же запросе (app/catalog/search.py следит за подменой). Фид пришел
не целиком или сборка упала: рабочая база остается прежней, причина пишется в журнал обновлений.

Запуск: python -m app.catalog.update    Сервер стенда запускает то же раз в сутки (FEED_UPDATE_HOUR).
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from app import config
from app.catalog import ingest

MIN_SHARE = 0.7  # в новом фиде меньше 70% прежних предложений: фид отдан не целиком, подмена отменяется
STATE = config.CATALOG_DB.parent / "catalog_update.json"


def download(url: str, dest: Path) -> None:
    """Скачивает фид потоком и распаковывает gzip на лету."""
    with httpx.stream("GET", url, timeout=httpx.Timeout(30, read=300), follow_redirects=True) as r:
        r.raise_for_status()
        if url.endswith(".gz"):
            with gzip.GzipFile(fileobj=_Reader(r.iter_raw())) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        else:
            with dest.open("wb") as out:
                for chunk in r.iter_bytes():
                    out.write(chunk)


class _Reader:
    """Файловый объект поверх итератора кусков: gzip читает из него столько, сколько просит."""

    def __init__(self, chunks):
        self._chunks, self._buf = chunks, b""

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self._buf) < n:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buf += chunk
        out, self._buf = (self._buf, b"") if n < 0 else (self._buf[:n], self._buf[n:])
        return out


def _offers(db: Path) -> int:
    with sqlite3.connect(db) as con:
        return con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]


def _diff(old: Path, new: Path) -> dict:
    """Что изменилось в каталоге: сколько предложений появилось, ушло и сменило цену."""
    con = sqlite3.connect(new)
    try:
        con.execute("ATTACH DATABASE ? AS old", (str(old),))
        one = lambda sql: con.execute(sql).fetchone()[0]
        return {"added": one("SELECT COUNT(*) FROM offers n WHERE NOT EXISTS (SELECT 1 FROM old.offers o WHERE o.id=n.id)"),
                "removed": one("SELECT COUNT(*) FROM old.offers o WHERE NOT EXISTS (SELECT 1 FROM offers n WHERE n.id=o.id)"),
                "price_changed": one("SELECT COUNT(*) FROM offers n JOIN old.offers o ON o.id=n.id "
                                     "WHERE COALESCE(n.price,-1) <> COALESCE(o.price,-1)")}
    finally:
        con.close()


def run(url: str | None = None, db: Path | None = None) -> dict:
    url, db = url or config.FEED_URL, Path(db or config.CATALOG_DB)
    feed, new_db = db.with_name("feed.new.xml"), db.with_name(db.stem + ".new.sqlite")
    state = {"started": time.time(), "url": url}
    try:
        download(url, feed)
        counts = ingest.build(feed, new_db, verbose=False)
        after = _offers(new_db)
        if db.exists():
            before = _offers(db)
            if after < before * MIN_SHARE:
                raise ValueError(f"в новом фиде {after} предложений против {before} в рабочей базе: подмена отменена")
            state.update(_diff(db, new_db), offers_before=before)
        os.replace(new_db, db)
        state.update(ok=True, offers=after, products=counts.get("products"), feed_date=counts.get("feed_date"))
    except Exception as e:  # рабочая база не тронута: чат продолжает отвечать по вчерашнему каталогу
        state.update(ok=False, error=f"{type(e).__name__}: {e}")
        new_db.unlink(missing_ok=True)
    finally:
        feed.unlink(missing_ok=True)
    state["finished"] = time.time()
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def last() -> dict:
    """Итог последнего обновления для экрана оператора."""
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


if __name__ == "__main__":
    res = run()
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(0 if res["ok"] else 1)
