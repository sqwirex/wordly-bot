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
from wordfreq import iter_wordlist
from dotenv import load_dotenv

# Загрузка .env
load_dotenv()

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния Conversation
ASK_LENGTH, GUESSING = range(2)

# Словарь «small» для экономии памяти
WORDLIST = list(iter_wordlist("ru", wordlist="small"))

# Эмоджи статусов
GREEN, YELLOW, RED, UNK = "🟩", "🟨", "🟥", "⬜"


def make_feedback(secret: str, guess: str) -> str:
    fb = [None] * len(guess)
    secret_chars = list(secret)
    # 1) зелёные
    for i, ch in enumerate(guess):
        if secret[i] == ch:
            fb[i] = GREEN
            secret_chars[i] = None
    # 2) жёлтые/красные
    for i, ch in enumerate(guess):
        if fb[i] is None:
            if ch in secret_chars:
                fb[i] = YELLOW
                secret_chars[secret_chars.index(ch)] = None
            else:
                fb[i] = RED
    return "".join(fb)


def compute_letter_status(secret: str, guesses: list[str]) -> dict[str, str]:
    status: dict[str, str] = {}
    for guess in guesses:
        # зелёные
        for i, ch in enumerate(guess):
            if secret[i] == ch:
                status[ch] = "green"
        # копия списка для жёлтых
        secret_chars = list(secret)
        for i, ch in enumerate(guess):
            if status.get(ch) == "green":
                secret_chars[i] = None
        # жёлтые/красные
        for i, ch in enumerate(guess):
            if status.get(ch) == "green":
                continue
            if ch in secret_chars:
                if status.get(ch) != "green":
                    status[ch] = "yellow"
                secret_chars[secret_chars.index(ch)] = None
            else:
                if status.get(ch) not in ("green", "yellow"):
                    status[ch] = "red"
    return status


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я Wordly Bot — угадай слово за 6 попыток.\n\n"
        "/play — начать новую игру\n"
        "/my_letters — показать информацию о буквах во время игры\n"
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
    context.user_data["secret"] = secret
    context.user_data["length"] = length
    context.user_data["attempts"] = 0
    context.user_data["guesses"] = []

    await update.message.reply_text(
        f"Я загадал слово из {length} букв. У тебя 6 попыток. Введи первую догадку:"
    )
    return GUESSING


async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guess = update.message.text.strip().lower()
    secret = context.user_data["secret"]
    length = context.user_data["length"]

    if len(guess) != length or guess not in WORDLIST:
        await update.message.reply_text(f"Введите существующее слово из {length} букв.")
        return GUESSING

    context.user_data["guesses"].append(guess)
    context.user_data["attempts"] += 1
    attempts = context.user_data["attempts"]

    fb = make_feedback(secret, guess)
    await update.message.reply_text(fb)

    # победа
    if guess == secret:
        context.user_data.clear()
        form = "попытка" if attempts % 10 == 1 and attempts % 100 != 11 else (
               "попытки" if 2 <= attempts % 10 <= 4 and not 12 <= attempts % 100 <= 14
               else "попыток")
        await update.message.reply_text(
            f"🎉 Поздравляю! Угадал за {attempts} {form}.\n"
            "Чтобы сыграть вновь, введи команду /play."
        )
        return ConversationHandler.END

    # поражение
    if attempts >= 6:
        context.user_data.clear()
        await update.message.reply_text(
            f"💔 Попытки закончились. Было слово «{secret}».\n"
            "Чтобы начать новую игру, введи команду /play."
        )
        return ConversationHandler.END

    return GUESSING


async def my_letters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data

    # вне игры
    if "secret" not in data:
        await update.message.reply_text(
            "Сейчас эта команда не имеет смысла — начни игру: /play"
        )
        return

    guesses = data.get("guesses", [])
    alphabet = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")

    # если нет попыток — показываем все буквы белым
    if not guesses:
        await update.message.reply_text(UNK + " " + " ".join(alphabet))
        return

    status = compute_letter_status(data["secret"], guesses)

    greens  = [ch for ch in alphabet if status.get(ch) == "green"]
    yellows = [ch for ch in alphabet if status.get(ch) == "yellow"]
    reds    = [ch for ch in alphabet if status.get(ch) == "red"]
    unused  = [ch for ch in alphabet if ch not in status]

    lines = []
    if greens:  lines.append(GREEN  + " " + " ".join(greens))
    if yellows: lines.append(YELLOW + " " + " ".join(yellows))
    if reds:    lines.append(RED    + " " + " ".join(reds))
    if unused:  lines.append(UNK    + " " + " ".join(unused))

    await update.message.reply_text("\n".join(lines))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Прогресс сброшен. Жду /play для новой игры.")
    return ConversationHandler.END


async def reset_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сейчас нечего сбрасывать — начните игру: /play")


IGN_MSG = "Команды /start и /play не работают во время игры — сначала /reset."


async def ignore_during(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(IGN_MSG)
    return ASK_LENGTH  # остаёмся в текущем состоянии


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN не установлен")
        return

    app = ApplicationBuilder().token(token).build()

    # ConversationHandler для /play
    conv = ConversationHandler(
        entry_points=[CommandHandler("play", ask_length)],
        states={
            ASK_LENGTH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_length),
                CommandHandler("start", ignore_during),
                CommandHandler("play", ignore_during),
                CommandHandler("reset", reset),
            ],
            GUESSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess),
                CommandHandler("my_letters", my_letters),
                CommandHandler("start", ignore_during),
                CommandHandler("play", ignore_during),
                CommandHandler("reset", reset),
            ],
        },
        fallbacks=[CommandHandler("reset", reset)],
    )
    app.add_handler(conv)

    # Глобальные команды
    app.add_handler(CommandHandler("my_letters", my_letters))
    app.add_handler(CommandHandler("reset", reset_global))
    app.add_handler(CommandHandler("start", start))

    # Запуск polling
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
