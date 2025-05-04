import os
import json
import random
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pymorphy2
from dotenv import load_dotenv
from wordfreq import iter_wordlist, zipf_frequency
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ——— Конфигурация и логирование ———
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

USER_FILE = Path("user_activity.json")
VOCAB_FILE = Path("vocabulary.json")

# ——— Хранилище состояния ———
def load_store() -> dict:
    """Загружает или инициализирует структуру store с 'users' и 'global'."""
    template = {
        "users": {},
        "global": {
            "total_games": 0,
            "total_wins": 0,
            "total_losses": 0,
            "win_rate": 0.0
        }
    }
    if not USER_FILE.exists():
        return template
    raw = USER_FILE.read_text("utf-8").strip()
    if not raw:
        return template
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return template
    if not isinstance(data, dict):
        return template

    users = data.get("users")
    glob = data.get("global")
    if not isinstance(users, dict) or not isinstance(glob, dict):
        return template

    for key, val in template["global"].items():
        glob.setdefault(key, val)

    return {"users": users, "global": glob}

def save_store(store: dict) -> None:
    """Сохраняет store в USER_FILE."""
    USER_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ——— Обновление профиля пользователя ———
def update_user_activity(user) -> None:
    """
    Сохраняет или обновляет профиль пользователя в store['users']:
    first_name, last_name, username, premium, last_seen, stats.
    """
    store = load_store()
    uid = str(user.id)
    u = store["users"].setdefault(uid, {
        "first_name": user.first_name,
        "id": user.id,
        "is_bot": user.is_bot,
        "language_code": user.language_code,
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })
    # Обновляем профиль
    u["first_name"] = user.first_name
    u["last_name"] = user.last_name
    u["username"] = user.username
    u["is_premium"] = getattr(user, "is_premium", False)
    u["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()
    save_store(store)

# ——— Генерация словаря ———
morph = pymorphy2.MorphAnalyzer(lang="ru")
_v = json.loads(VOCAB_FILE.read_text("utf-8"))
BLACK_LIST = set(_v.get("black_list", []))
WHITE_LIST = set(_v.get("white_list", []))
ZIPF_THRESHOLD = 2.5

_base = {
    w for w in iter_wordlist("ru", wordlist="large")
    if (
        w.isalpha() and
        4 <= len(w) <= 11 and
        w not in BLACK_LIST and
        zipf_frequency(w, "ru") >= ZIPF_THRESHOLD
    )
    for p in [morph.parse(w)[0]]
    if p.tag.POS == "NOUN" and p.normal_form == w
}
WORDLIST = sorted(_base | {w for w in WHITE_LIST if 4 <= len(w) <= 11})

GREEN, YELLOW, RED, UNK = "🟩", "🟨", "🟥", "⬜"

def make_feedback(secret: str, guess: str) -> str:
    fb = [None] * len(guess)
    secret_chars = list(secret)
    # зелёные
    for i, ch in enumerate(guess):
        if secret[i] == ch:
            fb[i] = GREEN
            secret_chars[i] = None
    # жёлтые/красные
    for i, ch in enumerate(guess):
        if fb[i] is None:
            if ch in secret_chars:
                fb[i] = YELLOW
                secret_chars[secret_chars.index(ch)] = None
            else:
                fb[i] = RED
    return "".join(fb)

def compute_letter_status(secret: str, guesses: list[str]) -> dict[str, str]:
    status = {}
    for guess in guesses:
        # зелёные
        for i, ch in enumerate(guess):
            if secret[i] == ch:
                status[ch] = "green"
        # жёлтые/красные
        secret_chars = list(secret)
        for ch, col in status.items():
            if col == "green":
                idx = [i for i,c in enumerate(guess) if c==ch and secret[i]==ch]
                if idx: secret_chars[idx[0]] = None
        for i, ch in enumerate(guess):
            if ch not in status:
                if ch in secret_chars:
                    status[ch] = "yellow"
                    secret_chars[secret_chars.index(ch)] = None
                else:
                    status[ch] = "red"
    return status

# ——— Состояния разговора ———
ASK_LENGTH, GUESSING = range(2)

# ——— Хендлеры ———
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        cg = u["current_game"]
        context.user_data.update({
            "secret": cg["secret"],
            "length": len(cg["secret"]),
            "attempts": cg["attempts"],
            "guesses": cg["guesses"],
        })
        await update.message.reply_text(
            f"У тебя есть незавершённая игра: слово из {len(cg['secret'])} букв, "
            f"ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING

    await update.message.reply_text(
        "Привет! Я Wordly Bot — угадай слово за 6 попыток.\n\n"
        "/play — начать новую игру\n"
        "/my_letters — статус букв во время игры\n"
        "/reset — сбросить игру\n\n"
        "Не забывай: Ё ≠ Е. Удачи!"
    )

async def send_activity_periodic(context: ContextTypes.DEFAULT_TYPE):
    """Шлёт содержимое user_activity.json админу каждые 3 ч и при старте."""
    activity_path = USER_FILE
    if not activity_path.exists():
        return
    content = activity_path.read_text("utf-8")
    if len(content) <= 4000:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"<pre>{content}</pre>",
            parse_mode="HTML"
        )
    else:
        with activity_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=InputFile(f, filename="user_activity.json"),
                caption="user_activity.json"
            )

async def ask_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    await update.message.reply_text("Сколько букв в слове? (4–11)")
    return ASK_LENGTH

async def my_letters_during_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сначала введи число от 4 до 11.")
    return ASK_LENGTH

async def receive_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    text = update.message.text.strip()
    if not text.isdigit() or not 4 <= int(text) <= 11:
        await update.message.reply_text("Нужно число от 4 до 11.")
        return ASK_LENGTH

    length = int(text)
    candidates = [w for w in WORDLIST if len(w)==length]
    if not candidates:
        await update.message.reply_text("Слова такой длины не нашёл.")
        return ASK_LENGTH

    secret = random.choice(candidates)
    store = load_store()
    u = store["users"].setdefault(str(update.effective_user.id), {
        "first_name": update.effective_user.first_name,
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })
    u["current_game"] = {"secret": secret, "attempts": 0, "guesses": []}
    save_store(store)

    context.user_data.update({
        "secret": secret,
        "length": length,
        "attempts": 0,
        "guesses": []
    })
    await update.message.reply_text(
        f"Я загадал слово из {length} букв. У тебя 6 попыток."
    )
    return GUESSING

async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    uid = str(update.effective_user.id)
    store = load_store()
    user = store["users"][uid]
    cg = user.get("current_game")
    if not cg:
        await update.message.reply_text("Нет активной игры — /play")
        return ConversationHandler.END

    guess = update.message.text.strip().lower()
    secret = cg["secret"]
    if len(guess)!=len(secret) or guess not in WORDLIST:
        await update.message.reply_text(f"Нужно слово из {len(secret)} букв.")
        return GUESSING

    cg["guesses"].append(guess)
    cg["attempts"] += 1
    await update.message.reply_text(make_feedback(secret, guess))

    won = (guess==secret)
    over = cg["attempts"]>=6 or won

    if over:
        user["stats"]["games_played"] += 1
        if won:
            user["stats"]["wins"] += 1
        else:
            user["stats"]["losses"] += 1

        g = store["global"]
        g["total_games"] += 1
        if won:
            g["total_wins"] += 1
        else:
            g["total_losses"] += 1
        g["win_rate"] = g["total_wins"]/g["total_games"]

        msg = (
            "🎉 Угадал!" if won else f"💔 Было слово «{secret}»."
        )
        await update.message.reply_text(
            f"{msg}\nПопыток: {cg['attempts']}. /play для новой игры."
        )

        del user["current_game"]
        save_store(store)
        return ConversationHandler.END

    save_store(store)
    return GUESSING

async def my_letters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    store = load_store()
    user = store["users"].get(uid, {})
    cg = user.get("current_game")
    if not cg:
        await update.message.reply_text("Нет активной игры — /play")
        return

    guesses = cg["guesses"]
    if not guesses:
        await update.message.reply_text(UNK + " абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
        return

    status = compute_letter_status(cg["secret"], guesses)
    alphabet = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
    lines = []
    lines.append(GREEN  + " " + " ".join(ch for ch in alphabet if status.get(ch)=="green"))
    lines.append(YELLOW + " " + " ".join(ch for ch in alphabet if status.get(ch)=="yellow"))
    lines.append(RED    + " " + " ".join(ch for ch in alphabet if status.get(ch)=="red"))
    lines.append(UNK    + " " + " ".join(ch for ch in alphabet if ch not in status))
    await update.message.reply_text("\n".join(lines))

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    context.user_data.clear()
    await update.message.reply_text("Игра сброшена. /play для новой.")
    return ConversationHandler.END

async def ignore_during(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нельзя сейчас. Сначала /reset, потом /play."
    )
    return

# ——— Точка входа ———
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # планировщик
    jq = app.job_queue
    jq.run_repeating(send_activity_periodic, interval=3*3600, first=0)

    conv = ConversationHandler(
        entry_points=[CommandHandler("play", ask_length)],
        states={
            ASK_LENGTH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_length),
                CommandHandler("my_letters", my_letters_during_length),
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
