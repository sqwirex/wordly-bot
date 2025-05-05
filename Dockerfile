FROM python:3.10-slim

WORKDIR /app

# Копируем .env из корня контекста
COPY .env .

# Зависимости бота
COPY telegram-wordly-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY telegram-wordly-bot/ /app

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
