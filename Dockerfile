# 1) Берём официальный образ Python
FROM python:3.10-slim

# 2) Работаем в /app
WORKDIR /app

# 3) Ставим зависимости
#    Предполагаем, что telegram-wordly-bot/requirements.txt лежит в корне site-for-mpu
COPY telegram-wordly-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) Копируем весь код бота
COPY telegram-wordly-bot/ /app

# 5) Чтобы логи сразу шли в консоль
ENV PYTHONUNBUFFERED=1

# 6) Точка входа
CMD ["python", "bot.py"]