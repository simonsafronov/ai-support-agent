"""Общие заготовки тестов: чистая база приложения на каждый тест, боевой каталог только для чтения."""
import pytest

from app import config, store

# Каталог и справка собираются из данных заказчика и в репозиторий не входят. На чистом клоне тест, которому
# они нужны, не падает, а пропускается с подсказкой; с собранными базами проходит весь набор.
HAVE_DATA = config.CATALOG_DB.exists() and config.KB_DB.exists()


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_data: тесту нужны собранные базы каталога и справки заказчика")


def pytest_collection_modifyitems(items):
    """Тест с пометкой needs_data на чистом клоне пропускается. Любой другой упавший тест падает: раньше здесь
    стоял перехват, который без баз превращал в пропуск любое падение, и настоящая ошибка была не видна."""
    if HAVE_DATA:
        return
    skip = pytest.mark.skip(reason="нет баз каталога и справки, соберите их по README (раздел Запуск)")
    for item in items:
        if item.get_closest_marker("needs_data"):
            item.add_marker(skip)


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
