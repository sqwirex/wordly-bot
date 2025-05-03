import os
import logging
import random
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from wordfreq import top_n_list
from dotenv import load_dotenv

load_dotenv()  # для локального .env

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ASK_LENGTH, GUESSING, PLAY_AGAIN = range(3)
WORDLIST = top_n_list("ru", 50000)
GREEN, YELLOW, RED = "🟩", "🟨", "🟥"

def make_feedback(secret: str, guess: str) -> str:
    feedback = [None] * len(guess)
    secret_chars = list(secret)
    # зелёные
    for i, ch in enumerate(guess):
        if secret[i] == ch:
            feedback[i] = GREEN
            secret_chars[i] = None
    # жёлтые/красные
    for i, ch in enumerate(guess):
        if feedback[i] is None:
            if ch in secret_chars:
                feedback[i] = YELLOW
                secret_chars[secret_chars.index(ch)] = None
            else:
                feedback[i] = RED
    return "".join(feedback)

# ==== Обработчики команд ====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я Wordly Bot — угадай слово за 6 попыток.\n\n"
        "/play — начать новую игру\n"
        "/reset — сбросить текущую игру\n\n"
        "Только не забывай: я ещё учусь и не знаю некоторых слов!\n"
        "Не расстраивайся, если я ругаюсь на твоё слово — мне есть чему учиться :)\n\n"
        "Кстати, иногда я могу «выключаться», потому что живу в грязном локальном контейнере, а не в уютном сервере :(\n"
        "Поэтому, если видишь, что я не отвечаю, вернись через какое-то время и нажми любую команду, чтобы проверить моё состояние.\n\n"
        "К сожалению, после таких перезапусков я теряю память и забываю, что мы играли в игру — создателю лень делать БД с сессиями игроков :(\n"
        "Поэтому после перезапуска придётся угадывать новое слово (х_х)."
    )

async def ask_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сколько букв в слове? (4–11)")
    return ASK_LENGTH

async def receive_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not 4 <= int(text) <= 11:
        await update.message.reply_text("Нужно число от 4 до 11.")
        return ASK_LENGTH

    length = int(text)
    candidates = [w for w in WORDLIST if len(w) == length]
    if not candidates:
        await update.message.reply_text("Не нашёл слов такой длины. Попробуй ещё:")
        return ASK_LENGTH

    secret = random.choice(candidates)
    context.user_data.update({
        "secret": secret,
        "length": length,
        "attempts": 0,
    })
    await update.message.reply_text(
        f"Загадал слово из {length} букв. У тебя 6 попыток. Введи первую догадку:"
    )
    return GUESSING

async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guess = update.message.text.strip().lower()
    length = context.user_data["length"]
    if len(guess) != length or guess not in WORDLIST:
        await update.message.reply_text(f"Введите существующее слово из {length} букв.")
        return GUESSING

    context.user_data["attempts"] += 1
    attempts = context.user_data["attempts"]
    secret = context.user_data["secret"]

    feedback = make_feedback(secret, guess)
    await update.message.reply_text(feedback)

    if guess == secret:
        await update.message.reply_text(
            f"🎉 Поздравляю! Угадал за {attempts} попыток.\n"
            "Сыграем ещё? (да/нет)"
        )
        return PLAY_AGAIN

    if attempts >= 6:
        await update.message.reply_text(
            f"💔 Попытки закончились. Было слово «{secret}».\n"
            "Сыграем ещё? (да/нет)"
        )
        return PLAY_AGAIN

    return GUESSING

async def play_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip().lower()
    if answer in ("да", "yes", "д"):
        return await ask_length(update, context)

    await update.message.reply_text("Окей, жду /play для новой игры.")
    return ConversationHandler.END

# ==== Новый: сброс прогресса ====

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Прогресс сброшен. Жду /play для новой игры.")
    return ConversationHandler.END

# ==== Игнорирование /start и /play во время игры ====

IGN_MSG = (
    "Команды /start и /play не работают во время игры. "
    "Если хочешь начать заново, нажми /reset."
)

async def ignore_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(IGN_MSG)
    return ASK_LENGTH

async def ignore_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(IGN_MSG)
    return GUESSING

async def ignore_playagain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(IGN_MSG)
    return PLAY_AGAIN

# ==== Точка входа ====

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN не установлен в окружении")
        return

    app = ApplicationBuilder().token(token).build()

    # ConversationHandler с новыми командами
    conv = ConversationHandler(
        entry_points=[CommandHandler("play", ask_length)],
        states={
            ASK_LENGTH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_length),
                CommandHandler("start", ignore_ask),
                CommandHandler("play", ignore_ask),
                CommandHandler("reset", reset),
            ],
            GUESSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess),
                CommandHandler("start", ignore_guess),
                CommandHandler("play", ignore_guess),
                CommandHandler("reset", reset),
            ],
            PLAY_AGAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, play_again),
                CommandHandler("start", ignore_playagain),
                CommandHandler("play", ignore_playagain),
                CommandHandler("reset", reset),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("reset", reset),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))  # на случай, когда не в игре
    app.add_handler(conv)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
