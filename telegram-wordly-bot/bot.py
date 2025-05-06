import os
import logging
import random
import pymorphy2
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+
from telegram import InputFile

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from wordfreq import iter_wordlist, zipf_frequency
from dotenv import load_dotenv

from telegram import BotCommand, BotCommandScopeChat


# Загрузка .env
load_dotenv()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# Файл для активности пользователей
USER_FILE = Path("user_activity.json")
VOCAB_FILE = Path("vocabulary.json")
with VOCAB_FILE.open("r", encoding="utf-8") as f:
    vocabulary = json.load(f)

# файл для предложений пользователей
SUGGESTIONS_FILE = Path("suggestions.json")

async def set_commands(app):
    
    await app.bot.set_my_commands(
        [
            BotCommand("start",         "Показать приветствие"),
            BotCommand("play",          "Начать новую игру"),
            BotCommand("reset",         "Сбросить игру"),
            BotCommand("my_letters",    "Статус букв в игре"),
            BotCommand("my_stats",      "Ваша статистика"),
            BotCommand("global_stats",  "Глобальная статистика"),
            BotCommand("feedback", "Жалоба на слово"),
            BotCommand("dump_activity", "Скачать user_activity.json"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

def load_suggestions() -> dict:
    if not SUGGESTIONS_FILE.exists():
        return {"black": [], "white": []}
    raw = SUGGESTIONS_FILE.read_text("utf-8").strip()
    if not raw:
        return {"black": [], "white": []}
    try:
        data = json.loads(raw)
        return {
            "black": list(data.get("black", [])),
            "white": list(data.get("white", [])),
        }
    except json.JSONDecodeError:
        return {"black": [], "white": []}

def save_suggestions(sugg: dict):
    with SUGGESTIONS_FILE.open("w", encoding="utf-8") as f:
        json.dump(sugg, f, ensure_ascii=False, indent=2)

# загружаем один раз при старте
suggestions = load_suggestions()

def load_store() -> dict:
    """
    Загружает user_activity.json.
    Если файла нет или он пуст/битый — возвращает чистый шаблон:
    {
      "users": {},
      "global": { "total_games":0, "total_wins":0, "total_losses":0, "win_rate":0.0 }
    }
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

    # Проверим разделы
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    if not isinstance(data.get("global"), dict):
        data["global"] = template["global"].copy()

    # Подставим недостающие ключи в global
    for key, val in template["global"].items():
        data["global"].setdefault(key, val)

    return data

def save_store(store: dict) -> None:
    """
    Сохраняет переданный store в USER_FILE в JSON-формате с отступами.
    Ожидаем, что store имеет формат:
    {
      "users": { ... },
      "global": { ... }
    }
    """
    USER_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def update_user_activity(user) -> None:
    """
    Создаёт или обновляет запись user в store['users'], добавляя:
    - first_name, last_name, username
    - is_bot, is_premium, language_code
    - last_seen_msk (по московскому времени)
    - stats (если ещё нет): games_played, wins, losses
    """
    store = load_store()
    uid = str(user.id)
    users = store["users"]

    # Если пользователь впервые — создаём базовую запись
    if uid not in users:
        users[uid] = {
            "first_name": user.first_name,
            "stats": {
                "games_played": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0
            }
        }

    u = users[uid]
    # Обновляем поля профиля
    u["first_name"]    = user.first_name
    u["last_name"]     = user.last_name
    u["username"]      = user.username
    u["is_bot"]        = user.is_bot
    u["is_premium"]    = getattr(user, "is_premium", False)
    u["language_code"] = user.language_code
    u["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()

    save_store(store)

# --- Константы и словарь ---
ASK_LENGTH, GUESSING, FEEDBACK_CHOOSE, FEEDBACK_WORD = range(4)

# инициализация морфоанализатора
morph = pymorphy2.MorphAnalyzer(lang="ru")

# частотный порог (регулируйте по вкусу)
ZIPF_THRESHOLD = 2.5

BLACK_LIST = set(vocabulary.get("black_list", []))
WHITE_LIST = set(vocabulary.get("white_list", []))

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

async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) если пользователь в процессе игры/выбора длины
    if context.user_data.get("game_active"):
        return
    # 2) или в процессе фидбека
    if context.user_data.get("feedback_state") is not None:
        return

    # иначе — сообщение вне игры и не диалога фидбека
    await update.message.reply_text(
        "Я не обрабатываю слова просто так😕\n"
        "Чтобы начать игру, введи /play."
    )

async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # запретим во время игры
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        await update.message.reply_text(
            "Нельзя отправлять фидбек пока идёт игра. Сначала закончи играть или нажми /reset.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    
    if context.user_data.get("game_active"):
        await update.message.reply_text(
            "Нельзя отправлять фидбек пока идёт игра. Сначала закончи играть или нажми /reset.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    # предлагаем выбрать список
    keyboard = [
        ["Чёрный список", "Белый список"],
        ["Отмена"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Куда добавить слово?", reply_markup=markup)

    # запомним текущее состояние
    context.user_data["feedback_state"] = FEEDBACK_CHOOSE
    return FEEDBACK_CHOOSE


async def feedback_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("feedback_state", None)
    text = update.message.text.strip()
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if text not in ("Чёрный список", "Белый список"):
        await update.message.reply_text("Пожалуйста, нажимайте одну из кнопок.")
        return FEEDBACK_CHOOSE

    # куда кладём
    context.user_data["fb_target"] = "black" if text == "Чёрный список" else "white"
    # убираем клавиатуру и спрашиваем слово
    await update.message.reply_text(
        "Введите слово для предложения:", reply_markup=ReplyKeyboardRemove()
    )

    context.user_data["feedback_state"] = FEEDBACK_WORD
    return FEEDBACK_WORD


async def feedback_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("feedback_state", None)
    word = update.message.text.strip().lower()
    target = context.user_data["fb_target"]

    # подтянем свежие предложения
    global suggestions
    suggestions = load_suggestions()

    if target == "black":
        if word not in WORDLIST:
            resp = "Нельзя: такого слова нет в основном словаре."
        elif word in vocabulary.get("black_list", []) or word in suggestions["black"]:
            resp = "Нельзя: слово уже в чёрном списке или вы его уже предлагали."
        else:
            suggestions["black"].append(word)
            save_suggestions(suggestions)
            resp = "Спасибо, добавил в предложения для чёрного списка."
    else:  # white
        if word in WORDLIST:
            resp = "Нельзя: такое слово уже есть в основном словаре."
        elif word in vocabulary.get("white_list", []) or word in suggestions["white"]:
            resp = "Нельзя: слово уже в белом списке или вы его уже предлагали."
        else:
            suggestions["white"].append(word)
            save_suggestions(suggestions)
            resp = "Спасибо, добавил в предложения для белого списка."

    await update.message.reply_text(resp)
    return ConversationHandler.END


async def block_during_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # любой посторонний ввод заглушаем
    await update.message.reply_text(
        "Сейчас идёт ввод для фидбека, нельзя использовать команды."
    )
    # возвращаемся в текущее состояние
    return context.user_data.get("feedback_state", FEEDBACK_CHOOSE)


async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("feedback_state", None)
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def dump_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    path = USER_FILE  # это Path("user_activity.json")
    if not path.exists():
        return await update.message.reply_text("Файл user_activity.json не найден.")

    # прочитаем текст, и если короткий — отправим как сообщение
    content = path.read_text("utf-8")
    if len(content) < 3000:
        # отправляем в кодовом блоке
        return await update.message.reply_text(
            f"<pre>{content}</pre>", parse_mode="HTML"
        )

    # иначе — отправляем как документ
    with path.open("rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=path.name),
            caption="📁 user_activity.json"
        )

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
        "/play — начать или продолжить игру\n"
        "/my_letters — показать статус букв во время игры\n"
        "/reset — сбросить текущую игру\n"
        "/my_stats — посмотреть свою статистику\n"
        "/global_stats — посмотреть глобальную статистику за все время\n"
        "/feedback — если ты встретил слово, которое не должно быть в словаре или не существует, введи его в Черный список, " \
        "если же наоборот, ты вбил слово, а бот его не признает, но ты уверен что оно существует, отправляй его в Белый список. " \
        "Администратор бота рассмотрит твое предложение и добавит в ближайшем обновлении, если оно действительно подходит!\n\n"
        "Только не забывай: я ещё учусь и не знаю некоторых слов!\n"
        "Не расстраивайся, если я ругаюсь на твоё слово — мне есть чему учиться :)\n\n"
        "Кстати, иногда я могу «выключаться», потому что живу в контейнере!\n"
        "Если я не отвечаю — попробуй позже и нажми /play или /start, чтобы продолжить прервавшуюся игру.\n\n"
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
    context.user_data["game_active"] = True
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

async def feedback_not_allowed_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нельзя отправлять фидбек пока вы выбираете длину слова. "
        "Сначала укажите длину (4–11)."
    )
    return ASK_LENGTH

async def feedback_not_allowed_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нельзя отправлять фидбек во время игры. "
        "Сначала закончите игру или /reset."
    )
    return GUESSING

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
        wp = user_entry["stats"]["wins"] / user_entry["stats"]["games_played"]
        user_entry["stats"]["win_rate"] = round(wp, 2)

        # Обновляем глобальную статистику
        store["global"]["total_games"]   = store["global"].get("total_games", 0) + 1
        store["global"]["total_wins"]    = store["global"].get("total_wins", 0) + 1
        gr = store["global"]["total_wins"] / store["global"]["total_games"]
        store["global"]["win_rate"]      = round(gr, 2)

        top_uid, top_data = max(
            store["users"].items(),
            key=lambda kv: kv[1].get("stats", {}).get("wins", 0)
        )

        store["global"]["top_player"] = {
            "user_id":   top_uid,
            "username":  top_data.get("username") or top_data.get("first_name", ""),
            "wins":      top_data["stats"]["wins"]
        }
        
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
        wp = user_entry["stats"]["wins"] / user_entry["stats"]["games_played"]
        user_entry["stats"]["win_rate"] = round(wp, 2)

        store["global"]["total_games"]   = store["global"].get("total_games", 0) + 1
        store["global"]["total_losses"]  = store["global"].get("total_losses", 0) + 1
        if store["global"]["total_games"]:
            gr = store["global"]["total_wins"] / store["global"]["total_games"]
            store["global"]["win_rate"] = round(gr, 2)

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

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает личную статистику — только вне игры."""
    update_user_activity(update.effective_user)
    store = load_store()
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)
    if not user or "current_game" in user:
        await update.message.reply_text("Эту команду можно использовать только вне игры.")
        return
    s = user.get("stats", {})
    await update.message.reply_text(
        "```"
        f"🧑 Ваши результаты:\n\n"
        f"🎲 Всего игр: {s.get('games_played',0)}\n"
        f"🏆 Побед: {s.get('wins',0)}\n"
        f"💔 Поражений: {s.get('losses',0)}\n"
        f"📊 Процент: {s.get('win_rate',0.0)*100:.2f}%"
        "```",
        parse_mode="Markdown"
    )

async def global_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    """Показывает глобальную статистику — только вне игры."""
    store = load_store()
    g = store["global"]
    # если во время партии — запрет
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)
    if user and "current_game" in user:
        await update.message.reply_text("Эту команду можно использовать только вне игры.")
        return
    
    tp = g.get("top_player", {})
    if tp:
        top_line = f"Сильнейший: @{tp['username']} ({tp['wins']} побед)\n\n"
    else:
        top_line = ""
    
    await update.message.reply_text(
        "```"
        f"🌐 Глобальная статистика:\n\n"
        f"🎲 Всего игр: {g['total_games']}\n"
        f"🏆 Побед: {g['total_wins']}\n"
        f"💔 Поражений: {g['total_losses']}\n"
        f"📊 Процент: {g['win_rate']*100:.2f}%\n\n"
        f"{top_line}"
        "```",
        parse_mode="Markdown"
    )

async def my_letters_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    await update.message.reply_text("Эту команду можно использовать только во время игры.")
    # остаёмся в том же состоянии ASK_LENGTH
    return ASK_LENGTH


async def my_letters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обновляем профиль пользователя
    update_user_activity(update.effective_user)

    store = load_store()
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)

    # Если игры нет вовсе — запрещаем, возвращаемся в GUESSING, 
    # но обработчик my_letters_not_allowed в ASK_LENGTH
    if not user or "current_game" not in user:
        await update.message.reply_text("Эту команду можно использовать только во время игры.")
        return GUESSING

    cg = user["current_game"]
    guesses = cg.get("guesses", [])
    secret = cg["secret"]

    alphabet = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")

    # Если ни одной попытки ещё не было — все буквы неизвестны
    if not guesses:
        await update.message.reply_text(UNK + " " + " ".join(alphabet))
        return GUESSING

    status = compute_letter_status(secret, guesses)
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
    return GUESSING

async def stats_not_allowed_during(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    await update.message.reply_text("Эту команду можно использовать только вне игры.")
    # возвращаем текущее состояние разговора, которое лежит в context.user_data
    return context.user_data.get("state", context.user_data["state"])

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)

    store = load_store()
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)
    if user and "current_game" in user:
        del user["current_game"]
        save_store(store)

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

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(set_commands)
        .build()
    )
	
    # Запускаем фоновую задачу: каждые 3 часа шлём user_activity.json админу
    job_queue = app.job_queue
    job_queue.run_repeating(
        send_activity_periodic,
        interval=3 * 60 * 60,  # 3 часа в секундах
        first=10      # первый запуск сразу
    )

    feedback_conv = ConversationHandler(
    entry_points=[CommandHandler("feedback", feedback_start)],
    states={
        FEEDBACK_CHOOSE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_choose),
            MessageHandler(filters.ALL, block_during_feedback),
        ],
        FEEDBACK_WORD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_word),
            MessageHandler(filters.ALL, block_during_feedback),
        ],
    },
    fallbacks=[CommandHandler("cancel", feedback_cancel)],
    allow_reentry=True,
    )
    
    app.add_handler(feedback_conv)
    
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("play", ask_length),
            CommandHandler("start", start),
        ],
        states={
            ASK_LENGTH: [
                CommandHandler("feedback", feedback_not_allowed_ask),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_length),
                CommandHandler("start", ignore_ask),
                CommandHandler("play", ignore_ask),
                CommandHandler("reset", reset),
                CommandHandler("my_stats", stats_not_allowed_during),
                CommandHandler("global_stats", stats_not_allowed_during),
		        CommandHandler("my_letters", my_letters_during_length),
                CommandHandler("my_letters", my_letters_not_allowed),
                CommandHandler("feedback", feedback_not_allowed_ask),
            ],
            GUESSING: [
                CommandHandler("feedback", feedback_not_allowed_guess),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess),
                CommandHandler("my_letters", my_letters),
                CommandHandler("start", ignore_guess),
                CommandHandler("my_stats", stats_not_allowed_during),
                CommandHandler("global_stats", stats_not_allowed_during),
                CommandHandler("play", ignore_guess),
                CommandHandler("reset", reset),
            ],
        },
        fallbacks=[
            CommandHandler("reset", reset),
       ],
    )
    app.add_handler(conv)

    app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text),
    group=99
    )

    # Глобальные
    app.add_handler(CommandHandler("reset", reset_global))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my_letters", my_letters_not_allowed))
    app.add_handler(CommandHandler("my_stats", my_stats))
    app.add_handler(CommandHandler("global_stats", global_stats))
    app.add_handler(CommandHandler("dump_activity", dump_activity))

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
