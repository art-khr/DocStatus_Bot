FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACCESS_PATH=/app/data/access.json

WORKDIR /app

# Бот использует только стандартную библиотеку Python — зависимости не нужны.
COPY bot.py .

# Каталог состояния: сюда бот пишет access.json (монтируется с хоста).
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
