import os
import logging
import random
import pymorphy2
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+
from telegram import InputFile

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from wordfreq import iter_wordlist, zipf_frequency
from dotenv import load_dotenv

# Загрузка .env
load_dotenv()

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Файл для активности пользователей
USER_FILE = Path("user_activity.json")
VOCAB_FILE = Path("vocabulary.json")

def load_store() -> dict:
    """
    Просто загружает user_activity.json в двухсекционной структуре.
    Если файл не существует, пуст или битый — возвращает шаблон.
    Не делает никакой миграции старых ключей.
    """
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

    # Убедимся, что структура корректна
    if not isinstance(data, dict):
        return template
    users = data.get("users")
    global_ = data.get("global")
    if not isinstance(users, dict) or not isinstance(global_, dict):
        return template

    # Типы полей в global
    for key in ("total_games", "total_wins", "total_losses", "win_rate"):
        if key not in global_:
            global_[key] = template["global"][key]

    return {"users": users, "global": global_}


def save_store(store: dict) -> None:
    """
    Записывает store в user_activity.json.
    Ожидается формат:
    {
      "users": { ... },
      "global": { ... }
    }
    """
    USER_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    
def load_user_activity() -> dict:
    """
    Загружает user_activity.json.
    Если файл пустой или НЕ является корректным JSON,
    возвращает пустой словарь.
    """
    if not USER_FILE.exists():
        return {}
    text = USER_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # можно залогировать, если нужно:
        logger.warning(f"{USER_FILE} содержит некорректный JSON, сбрасываем")
        return {}

def save_user_activity(data: dict) -> None:
    USER_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def update_user_activity(user: telegram.User) -> None:
    """
    Обновляет запись пользователя в store['users']:
    сохраняет поля from_dict + last_seen_msk.
    """
    store = load_store()
    uid = str(user.id)

    # Берём существующую запись или новый шаблон
    u = store["users"].setdefault(uid, {
        "first_name": user.first_name,
        "id": user.id,
        "is_bot": user.is_bot,
        "language_code": user.language_code,
        # stats гарантированно есть в load_store()
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })

    # Обновляем профильные поля
    u["username"]    = user.username
    u["last_name"]   = user.last_name
    u["is_premium"]  = getattr(user, "is_premium", False)
    u["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()

    save_store(store)


# --- Константы и словарь ---
ASK_LENGTH, GUESSING = range(2)

# инициализация морфоанализатора
morph = pymorphy2.MorphAnalyzer(lang="ru")

# частотный порог (регулируйте по вкусу)
ZIPF_THRESHOLD = 2.5

_v = json.loads(VOCAB_FILE.read_text(encoding="utf-8"))

BLACK_LIST = set(_v.get("black_list", []))
WHITE_LIST = set(_v.get("white_list", []))

_base = {
    w
    for w in iter_wordlist("ru", wordlist="large")
    if (
        w.isalpha()
        and 4 <= len(w) <= 11
        and w not in BLACK_LIST
        and zipf_frequency(w, "ru") >= ZIPF_THRESHOLD
    )
    for p in [morph.parse(w)[0]]
    if p.tag.POS == "NOUN" and p.normal_form == w
}

# Объединяем с белым списком, чтобы эти слова гарантированно присутствовали
WORDLIST = sorted(_base | {w for w in WHITE_LIST if 4 <= len(w) <= 11})

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
        # копия для жёлтых
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


# --- Обработчики команд ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        cg = u["current_game"]
        # заполняем context.user_data из cg:
        context.user_data.update({
            "secret": cg["secret"],
            "length": len(cg["secret"]),
            "attempts": cg["attempts"],
            "guesses": cg["guesses"],
        })
        await update.message.reply_text(
            f"У тебя есть незавершённая игра: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING

    
    await update.message.reply_text(
        "Привет! Я Wordly Bot — угадай слово за 6 попыток.\n\n"
        "/play — начать новую игру\n"
        "/my_letters — показать статус букв во время игры\n"
        "/reset — сбросить текущую игру\n\n"
        "Только не забывай: я ещё учусь и не знаю некоторых слов!\n"
        "Не расстраивайся, если я ругаюсь на твоё слово — мне есть чему учиться :)\n\n"
        "Кстати, иногда я могу «выключаться», потому что живу в контейнере!\n"
        "Если я не отвечаю — попробуй позже и нажми любую команду.\n\n"
        "После перезапуска я забываю прогресс, так что придётся начать заново (х_х).\n\n"
	"И еще, не забывай, буква Ё ≠ Е. Удачи!"
    )

async def send_activity_periodic(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодически (и сразу при старте) шлёт user_activity.json администратору.
    Если файл слишком большой, шлёт его как документ.
    """
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    activity_path = USER_FILE
    if not activity_path.exists():
        return

    content = activity_path.read_text(encoding="utf-8")
    # Ограничение Telegram — примерно 4096 символов
    MAX_LEN = 4000

    if len(content) <= MAX_LEN:
        # Можно втиснуть в одно сообщение
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📋 Текущий user_activity.json:\n<pre>{content}</pre>",
            parse_mode="HTML"
        )
    else:
        # Слишком длинное — отправляем как файл
        from telegram import InputFile
        with activity_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=InputFile(f, filename="user_activity.json"),
                caption="📁 user_activity.json (слишком большой для текста)"
            )

async def ask_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        cg = u["current_game"]
        # заполняем context.user_data из cg:
        context.user_data.update({
            "secret": cg["secret"],
            "length": len(cg["secret"]),
            "attempts": cg["attempts"],
            "guesses": cg["guesses"],
        })
        await update.message.reply_text(
            f"У тебя есть незавершённая игра: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING
    
    await update.message.reply_text("Сколько букв в слове? (4–11)")
    return ASK_LENGTH
	
async def my_letters_during_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь нажал /my_letters до того, как выбрал длину
    await update.message.reply_text("Нужно ввести число от 4 до 11.")
    return ASK_LENGTH
	
async def receive_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
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
    
    store = load_store()
    u = store["users"].setdefault(str(update.effective_user.id), {"stats": {"games_played":0,"wins":0,"losses":0}})
    # Запись текущей игры
    u["current_game"] = {
        "secret": secret,
        "attempts": 0,
        "guesses": [],
    }
    save_store(store)

    context.user_data["secret"] = secret
    context.user_data["length"] = length
    context.user_data["attempts"] = 0
    context.user_data["guesses"] = []

    await update.message.reply_text(
        f"Я загадал слово из {length} букв. У тебя 6 попыток. Введи первую догадку:"
    )
    return GUESSING

async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    store = load_store()
    user_entry = store["users"].setdefault(user_id, {
        "first_name": update.effective_user.first_name,
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })

    # Обновляем last_seen
    user_entry["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()

    # Если по какой‑то причине current_game отсутствует — инициируем новую
    if "current_game" not in user_entry:
        await update.message.reply_text("Нет активной игры, начни /play")
        return ConversationHandler.END

    cg = user_entry["current_game"]
    guess = update.message.text.strip().lower()
    secret = cg["secret"]
    length = len(secret)

    # Валидация
    if len(guess) != length or guess not in WORDLIST:
        await update.message.reply_text(f"Введите существующее слово из {length} букв.")
        return GUESSING

    # Сохраняем догадку
    cg["guesses"].append(guess)
    cg["attempts"] += 1

    # Фидбек
    fb = make_feedback(secret, guess)
    await update.message.reply_text(fb)

    # Победа
    if guess == secret:
        # Обновляем пользовательскую статистику
        user_entry["stats"]["games_played"] += 1
        user_entry["stats"]["wins"] += 1

        # Обновляем глобальную статистику
        store["global"]["total_games"]   = store["global"].get("total_games", 0) + 1
        store["global"]["total_wins"]    = store["global"].get("total_wins", 0) + 1
        store["global"]["total_losses"]  = store["global"].get("total_losses", 0)
        store["global"]["win_rate"]      = store["global"]["total_wins"] / store["global"]["total_games"]

        await update.message.reply_text(
            f"🎉 Поздравляю! Угадал за {cg['attempts']} {'попытка' if cg['attempts']==1 else 'попытки' if 2<=cg['attempts']<=4 else 'попыток'}.\n"
            "Чтобы сыграть вновь, введи команду /play."
        )

        # Удаляем текущее состояние игры
        del user_entry["current_game"]
        save_store(store)
        return ConversationHandler.END

    # Поражение
    if cg["attempts"] >= 6:
        user_entry["stats"]["games_played"] += 1
        user_entry["stats"]["losses"] += 1

        store["global"]["total_games"]   = store["global"].get("total_games", 0) + 1
        store["global"]["total_wins"]    = store["global"].get("total_wins", 0)
        store["global"]["total_losses"]  = store["global"].get("total_losses", 0) + 1
        store["global"]["win_rate"]      = (
            store["global"]["total_wins"] / store["global"]["total_games"]
            if store["global"]["total_games"] else 0
        )

        await update.message.reply_text(
            f"💔 Попытки закончились. Было слово «{secret}».\n"
            "Чтобы начать новую игру, введи команду /play."
        )

        del user_entry["current_game"]
        save_store(store)
        return ConversationHandler.END

    # Игра продолжается — сохраняем прогресс и ждём следующей догадки
    save_store(store)
    return GUESSING


async def my_letters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        cg = u["current_game"]
        # заполняем context.user_data из cg:
        context.user_data.update({
            "secret": cg["secret"],
            "length": len(cg["secret"]),
            "attempts": cg["attempts"],
            "guesses": cg["guesses"],
        })
        await update.message.reply_text(
            f"У тебя есть незавершённая игра: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING
    
    data = context.user_data
    if "secret" not in data:
        await update.message.reply_text("Сейчас эта команда не имеет смысла — начни игру: /play")
        return
    guesses = data.get("guesses", [])
    alphabet = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")

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
    update_user_activity(update.effective_user)
    context.user_data.clear()
    await update.message.reply_text("Прогресс сброшен. Жду /play для новой игры.")
    return ConversationHandler.END

async def reset_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    await update.message.reply_text("Сейчас нечего сбрасывать — начните игру: /play")

IGN_MSG = "Команды /start и /play не работают во время игры — сначала /reset."

async def ignore_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(IGN_MSG)
    return ASK_LENGTH

async def ignore_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(IGN_MSG)
    return GUESSING


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN не установлен")
        return

    app = ApplicationBuilder().token(token).build()
	
    # Запускаем фоновую задачу: каждые 3 часа шлём user_activity.json админу
    job_queue = app.job_queue
    job_queue.run_repeating(
        send_activity_periodic,
        interval=3 * 60 * 60,  # 3 часа в секундах
        first=10      # первый запуск сразу
    )
	
    conv = ConversationHandler(
        entry_points=[CommandHandler("play", ask_length)],
        states={
            ASK_LENGTH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_length),
                CommandHandler("start", ignore_ask),
                CommandHandler("play", ignore_ask),
                CommandHandler("reset", reset),
		CommandHandler("my_letters", my_letters_during_length),
            ],
            GUESSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess),
                CommandHandler("my_letters", my_letters),
                CommandHandler("start", ignore_guess),
                CommandHandler("play", ignore_guess),
                CommandHandler("reset", reset),
            ],
        },
        fallbacks=[CommandHandler("reset", reset)],
    )
    app.add_handler(conv)

    # Глобальные
    app.add_handler(CommandHandler("my_letters", my_letters))
    app.add_handler(CommandHandler("reset", reset_global))
    app.add_handler(CommandHandler("start", start))

    store = load_store()
    # Для каждого пользователя, у которого был current_game, 
    # контекст загрузит его в context.user_data
    for uid, udata in store["users"].items():
        if "current_game" in udata:
            # мы запомним это в user_data при первом обращении:
            pass

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
