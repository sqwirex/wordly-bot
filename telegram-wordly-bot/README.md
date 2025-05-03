# Шаг первый
Создать файл .env с BOT_TOKEN="токен бота"

# Шаг второй: забилдить контейнер
docker build -t wordly-bot .

# Шаг третий: запустить конт в фоне и передать ему файл с токеном
docker run -d \
  --name wordly-bot \
  --env-file .env \
  --restart unless-stopped \
  wordly-bot

# Шаг четвертый: проверить логи на наличие ошибок
docker logs -f wordle-bot

# В images будет 2 образа, python и wordly-bot, в запущенных/остановленных контах будет wordly-bot
# Если вносятся изменения в код и тд, делаем шаг 2, потом удаляем конт:
docker rm -f wordly-bot
# и делаем шаг 3 и 4
