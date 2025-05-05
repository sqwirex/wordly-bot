FROM python:3.10-slim

WORKDIR /app

# Копируем .env из корня (теперь он лежит в site-for-mpu/.env)
COPY .env .

# Устанавливаем зависимости
COPY telegram-wordly-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY telegram-wordly-bot/ /app

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]