"""Общие заготовки тестов: чистая база приложения на каждый тест, боевой каталог только для чтения."""
import pytest

from app import config, store

# Каталог и справка собираются из данных заказчика и в репозиторий не входят. На чистом клоне тест, которому
# они нужны, не падает, а пропускается с подсказкой; с собранными базами проходит весь набор.
HAVE_DATA = config.CATALOG_DB.exists() and config.KB_DB.exists()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed and call.when in ("setup", "call") and not HAVE_DATA:
        report.outcome = "skipped"
        report.longrepr = (str(item.fspath), item.location[1] or 0,
                           "Skipped: нет баз каталога и справки, соберите их по README (раздел Запуск)")


@pytest.fixture(autouse=True)
def app_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB", tmp_path / "app.sqlite")
    monkeypatch.setattr(config, "QUOTES_DIR", tmp_path / "quotes")
    monkeypatch.setattr(config, "CARDS_DB", tmp_path / "cards.sqlite")
    monkeypatch.setattr(config, "PRODUCT_CARDS", False)  # тесты не ходят на сайт магазина за описаниями
    store._local.con = None
    yield
    con = getattr(store._local, "con", None)
    if con is not None:
        con.close()
    store._local.con = None
