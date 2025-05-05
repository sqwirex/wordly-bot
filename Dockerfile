FROM python:3.10-slim
WORKDIR /app/telegram-wordly-bot

COPY telegram-wordly-bot/.env .        
COPY telegram-wordly-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY telegram-wordly-bot/ /app/telegram-wordly-bot

CMD ["python", "bot.py"]