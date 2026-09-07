FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Бот использует только стандартную библиотеку Python — зависимости не нужны.
COPY bot.py .

RUN adduser -D -H bot && mkdir -p /app/logs && chown -R bot:bot /app
USER bot

CMD ["python", "bot.py"]
