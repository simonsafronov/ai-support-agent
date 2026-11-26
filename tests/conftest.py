"""Общие заготовки тестов: чистая база приложения на каждый тест, боевой каталог только для чтения."""
import pytest

from app import config, store


@pytest.fixture(autouse=True)
def app_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB", tmp_path / "app.sqlite")
    monkeypatch.setattr(config, "QUOTES_DIR", tmp_path / "quotes")
    store._local.con = None
    yield
    con = getattr(store._local, "con", None)
    if con is not None:
        con.close()
    store._local.con = None
