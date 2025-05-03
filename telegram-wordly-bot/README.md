# Все действия выполняются в консоли находясь в папке с проектом
## Шаг первый
Создать файл .env с BOT_TOKEN="токен бота"
Создать файл user_activity.json
Скачать докер, если не скачен

## Шаг второй: забилдить контейнер
docker build -t wordly-bot .

## Шаг третий: запустить конт в фоне и передать ему файл с токеном, сделать его постоянно запускаемым и передать json со статистикой
docker run -d \
  --name wordly-bot \
  --env-file .env \
  -v ~/telegram-wordly-bot/user_activity.json:/app/user_activity.json \
  wordly-bot
## Шаг четвертый: проверить логи на наличие ошибок
docker logs -f wordle-bot

## В images будет 2 образа, python и wordly-bot, в запущенных/остановленных контах будет wordly-bot
## Если вносятся изменения в код и тд, делаем шаг 2, потом удаляем конт:
docker rm -f wordly-bot
## и делаем шаг 3 и 4

## Чтобы посмотреть информацию о пользователе, которая внутри контейнера, вбиваем:
docker exec wordly-bot cat /app/user_activity.json
