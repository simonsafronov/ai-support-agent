FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY certs certs
COPY eval eval
COPY data/ru_vendors.txt data/ru_vendors.txt
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# Базы каталога и знаний лежат в томе data/db: их собирают команды из README (раздел "Запуск")
CMD ["uvicorn", "app.web.server:app", "--host", "0.0.0.0", "--port", "8000"]
