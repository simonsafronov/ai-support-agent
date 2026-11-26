FROM python:3.13-slim
WORKDIR /srv
# requirements.lock это точные версии всего дерева зависимостей, на которых проходят тесты
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app app
COPY certs certs
COPY eval eval
COPY data/ru_vendors.txt data/ru_vendors.txt
# Сервис работает не от root. Пишет он только в data (базы, КП) и eval (кейсы со стенда).
RUN useradd --uid 1000 --no-create-home app && mkdir -p data/db data/quotes && chown -R app:app data eval
USER app
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
# Базы каталога и знаний лежат в томе data: их собирают команды из README (раздел "Запуск").
# Каталог тома на хосте должен быть доступен на запись пользователю с uid 1000.
CMD ["uvicorn", "app.web.server:app", "--host", "0.0.0.0", "--port", "8000"]
