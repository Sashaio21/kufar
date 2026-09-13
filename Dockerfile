FROM python:3.13-slim

# Не создаём .pyc и не буферизуем вывод, чтобы логи сразу были видны в docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Файл, в котором скрипт хранит уже виденные объявления -
# держим его в отдельном volume, чтобы данные не терялись при пересоздании контейнера
VOLUME ["/app/data"]
ENV SEEN_IDS_FILE=/app/data/seen_ads.json

CMD ["python", "main.py"]
