FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACCESS_PATH=/app/data/access.json

WORKDIR /app

# Единственная зависимость — драйвер PostgreSQL (нужен только при APP_ENV=production).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Каталог состояния: при APP_ENV=development сюда пишется access.json (монтируется с хоста).
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
