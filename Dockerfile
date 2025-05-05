# Используем лёгкий официальный образ Python
FROM python:3.10-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Копируем .env (он создаётся на VPS/CI до сборки)
COPY .env .

# Устанавливаем зависимости бота
COPY telegram-wordly-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем сам код бота
COPY telegram-wordly-bot/ /app

# Чтобы логи сразу шли на STDOUT
ENV PYTHONUNBUFFERED=1

# Точка входа
CMD ["python", "bot.py"]