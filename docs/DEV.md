# Разработка

- Python 3.13, окружение `.venv` (fastapi, uvicorn, httpx, snowballstemmer, reportlab, pypdf, pytest).
- Данные заказчика лежат в `data/raw/` (в репозиторий не кладем): `feed.xml` (каталог YML), `buy_part_*_masked.txt` (переписки), `kp_template.pdf`.
- Базы строятся в `data/db/`: `catalog.sqlite` (каталог), `kb.sqlite` (база знаний).
- Модули: `app/catalog` (загрузка и поиск по каталогу), `app/kb` (база знаний), `app/agent` (модель, инструменты, цикл агента), `app/quote` (КП), `app/web` (чат и экран оператора), `eval` (проверочные диалоги).
- Запуск тестов: `.venv/bin/pytest -q`.
