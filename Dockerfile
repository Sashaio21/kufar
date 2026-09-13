FROM python:3.13-slim

# Не создаём .pyc и не буферизуем вывод, чтобы логи сразу были видны в docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Файлы состояния (уже виденные объявления + текущий интервал) -
# держим их в отдельном volume, чтобы данные не терялись при пересоздании контейнера
VOLUME ["/app/data"]
ENV SEEN_IDS_FILE=/app/data/seen_ads.json
ENV STATE_FILE=/app/data/monitor_state.json

# main.py сам проверяет объявления по расписанию (управляется командами
# Telegram-бота /interval, /status, /check). HTTP-сервер поднимается только
# ради /health - для докера/внешнего мониторинга "жив ли процесс".
EXPOSE 8080

CMD ["python", "main.py"]
