import os
import logging
import random
import json

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+
from io import BytesIO
from collections import Counter
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
    BotCommandScopeChat,
    InputFile
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

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
# файл для предложений пользователей
SUGGESTIONS_FILE = Path("suggestions.json")
# админ айди
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

async def set_commands(app):
    
    await app.bot.set_my_commands(
        [
            BotCommand("start",         "Показать приветствие"),
            BotCommand("play",          "Начать новую игру"),
            BotCommand("hint",    "Подсказка"),
            BotCommand("reset",         "Сбросить игру"),
            BotCommand("my_stats",      "Ваша статистика"),
            BotCommand("global_stats",  "Глобальная статистика"),
            BotCommand("feedback", "Жалоба на слово"),
            BotCommand("dict_file",  "Посмотреть словарь"),
            BotCommand("dump_activity", "Скачать user_activity.json"),
            BotCommand("suggestions_view", "Посмотреть фидбек юзеров"),
            BotCommand("suggestions_remove", "Удалить что-то из фидбека"),
            BotCommand("broadcast", "Отправить сообщение всем юзерам"),
            BotCommand("broadcast_cancel", "Отменить отправку")
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
    - stats (если ещё нет): games_played, wins, losses, win rate
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



def render_wordle_image(guesses: list[str], secret: str) -> BytesIO:
    """
    Рисует доску Wordle с цветами (🟩, 🟨, ⬜),
    автоматически подгоняя размеры квадратиков под max_width_px.
    """
    # Параметры
    max_width_px   = 600      # максимально допустимая ширина всего изображения
    padding        = 8        # px между квадратиками и по краям
    default_sq     = 80       # желаемый размер квадратика
    rows, cols     = len(guesses), len(secret)

    # 1) Вычисляем размер квадратика так, чтобы доска влезала в max_width_px
    total_pad = (cols + 1) * padding
    square = default_sq
    if cols * default_sq + total_pad > max_width_px:
        square = max(
            20,  # нижняя граница размера квадратика
            (max_width_px - total_pad) // cols
        )

    width  = cols * square + total_pad
    height = rows * square + (rows + 1) * padding

    # Шрифт — масштабируем пропорционально square
    font_path = "DejaVuSans-Bold.ttf"
    font_size = int(square * 0.6)
    font      = ImageFont.truetype(font_path, font_size)

    # Создаем полотно
    img  = Image.new("RGB", (width, height), color=(30,30,30))
    draw = ImageDraw.Draw(img)

    for r, guess in enumerate(guesses):
        fb = make_feedback(secret, guess)  # 🟩🟨⬜…
        for c, ch in enumerate(guess):
            x0 = padding + c * (square + padding)
            y0 = padding + r * (square + padding)
            x1, y1 = x0 + square, y0 + square

            # фон по фидбеку
            if   fb[c] == "🟩": bg = (106,170,100)
            elif fb[c] == "🟨": bg = (201,180, 88)
            else:               bg = (255,255,255)

            # цвет текста: на белом — чёрный, иначе — белый
            text_color = (0,0,0) if bg == (255,255,255) else (255,255,255)

            draw.rectangle([x0,y0,x1,y1], fill=bg, outline=(0,0,0), width=2)

            # получаем размер буквы
            bbox = draw.textbbox((0,0), ch.upper(), font=font)
            w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]

            tx = x0 + (square - w) / 2
            ty = y0 + (square - h) / 2
            draw.text((tx, ty), ch.upper(), font=font, fill=text_color)

    # 2) На всякий случай — если итоговая ширина всё ещё больше max_width_px,
    #    делаем финальное масштабирование:
    if img.width > max_width_px:
        ratio = max_width_px / img.width
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# --- Константы и словарь ---
ASK_LENGTH, GUESSING, FEEDBACK_CHOOSE, FEEDBACK_WORD, REMOVE_INPUT, BROADCAST= range(6)

VOCAB_FILE = Path("vocabulary.json")
with VOCAB_FILE.open("r", encoding="utf-8") as f:
    vocabulary = json.load(f)
BLACK_LIST = set(vocabulary.get("black_list", []))
WHITE_LIST = set(vocabulary.get("white_list", []))
BASE_FILE = Path("base_words.json")
with BASE_FILE.open("r", encoding="utf-8") as f:
    BASE_WORDS = set(json.load(f))
# Объединяем с белым списком, чтобы эти слова гарантированно присутствовали
WORDLIST = sorted(
    w for w in (BASE_WORDS | WHITE_LIST)
    if (
        w.isalpha()
        and 4 <= len(w) <= 11
        and w not in BLACK_LIST
    )
)

GREEN, YELLOW, RED = "🟩", "🟨", "⬜"

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


# --- Обработчики команд ---

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
        with activity_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=InputFile(f, filename="user_activity.json"),
                caption="📁 user_activity.json (слишком большой для текста)"
            )


async def send_unfinished_games(context: ContextTypes.DEFAULT_TYPE):
    """
    Раз в 1 секунду после старта отправляем всем пользователям с current_game
    напоминание продолжить игру.
    """
    store = load_store()
    for uid, udata in store["users"].items():
        if "current_game" in udata:
            cg = udata["current_game"]
            length = len(cg["secret"])
            attempts = cg["attempts"]
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=(
                        f"Я вернулся из спячки!\n"
                        f"⏳ У вас есть незавершённая игра:\n"
                        f"{length}-буквенное слово, вы на попытке {attempts}.\n"
                        "Нажмите /play или /start, чтобы продолжить!"
                    )
                )
            except Exception as e:
                logger.warning(f"Не смогли напомнить {uid}: {e}")


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # если сейчас в игре или в фидбеке — молчим
    if context.user_data.get("game_active") or context.user_data.get("in_feedback") or context.user_data.get("in_remove"):
        return
    if context.user_data.pop("just_done", False):
        return
    await update.message.reply_text(
        "Я не обрабатываю слова просто так😕\n"
        "Чтобы начать игру, введи /play."
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
            f"Продолжаем игру: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING

    
    await update.message.reply_text(
        "Привет! Я Wordle Bot — угадай слово за 6 попыток.\n"
        "https://github.com/sqwirex/wordle-bot - ссылка на репозиторий с кодом бота\n\n"
        "/play — начать или продолжить игру\n"
        "/hint — дает слово в подсказку, если вы затрудняетесь ответить " \
        "(случайное слово в котором совпадают некоторые буквы с загаданным)\n"
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


async def ask_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = ASK_LENGTH
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
            f"Продолжаем игру: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING
    
    await update.message.reply_text("Сколько букв в слове? (4–11)")
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
    context.user_data["state"] = GUESSING

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

    # Обновляем время последнего визита
    user_entry["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()

    # Проверяем, есть ли активная игра
    if "current_game" not in user_entry:
        await update.message.reply_text("Нет активной игры, начни /play")
        return ConversationHandler.END

    cg     = user_entry["current_game"]
    guess  = update.message.text.strip().lower()
    secret = cg["secret"]
    length = len(secret)

    # Валидация
    if len(guess) != length or guess not in WORDLIST:
        await update.message.reply_text(f"Введите существующее слово из {length} букв.")
        return GUESSING

    # Сохраняем ход
    cg["guesses"].append(guess)
    cg["attempts"] += 1
    save_store(store)

    # Рендерим доску в картинку
    img_buf = render_wordle_image(cg["guesses"], cg["secret"])
    await update.message.reply_photo(
        photo=InputFile(img_buf, filename="wordle.png"),
        caption=f"Попытка {cg['attempts']} из 6"
    )

    # —— Победа ——
    if guess == secret:
        stats = user_entry["stats"]
        stats["games_played"] += 1
        stats["wins"] += 1
        stats["win_rate"] = stats["wins"] / stats["games_played"]

        g = store["global"]
        g["total_games"] += 1
        g["total_wins"] += 1
        g["win_rate"] = g["total_wins"] / g["total_games"]

        # Обновляем топ-игрока
        top_uid, top_data = max(
            store["users"].items(),
            key=lambda kv: kv[1]["stats"]["wins"]
        )
        store["global"]["top_player"] = {
            "user_id":  top_uid,
            "username": top_data.get("username") or top_data.get("first_name", ""),
            "wins":     top_data["stats"]["wins"]
        }

        await update.message.reply_text(
            f"🎉 Поздравляю! Угадал за {cg['attempts']} "
            f"{'попытка' if cg['attempts']==1 else 'попытки' if 2<=cg['attempts']<=4 else 'попыток'}.\n"
            "Чтобы сыграть вновь, введи /play."
        )

        del user_entry["current_game"]
        context.user_data.pop("game_active", None)
        context.user_data["just_done"] = True
        save_store(store)
        return ConversationHandler.END

    # —— Поражение ——
    if cg["attempts"] >= 6:
        stats = user_entry["stats"]
        stats["games_played"] += 1
        stats["losses"] += 1
        stats["win_rate"] = stats["wins"] / stats["games_played"]

        g = store["global"]
        g["total_games"] += 1
        g["total_losses"] += 1
        g["win_rate"] = g["total_wins"] / g["total_games"]

        await update.message.reply_text(
            f"💔 Попытки закончились. Было слово «{secret}».\n"
            "Чтобы начать новую игру, введи /play."
        )

        del user_entry["current_game"]
        context.user_data.pop("game_active", None)
        context.user_data["just_done"] = True
        save_store(store)
        return ConversationHandler.END

    # Игра продолжается
    return GUESSING


async def ignore_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды /start и /play не работают во время игры — сначала /reset.")
    return ASK_LENGTH


async def ignore_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды /start и /play не работают во время игры — сначала /reset.")
    return GUESSING


async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    store = load_store()
    user_entry = store["users"].setdefault(user_id, {
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })

    # Проверяем, есть ли активная игра
    if "current_game" not in user_entry:
        await update.message.reply_text("Эту команду можно использовать только во время игры.")
        return ConversationHandler.END

    cg = user_entry["current_game"]

    # Если подсказка уже взята — не даём ещё одну
    if cg.get("hint_used", False):
        await update.message.reply_text("Подсказка уже использована в этой игре.")
        return GUESSING

    secret = cg["secret"]
    length = len(secret)

    # Сколько букв нужно подсказать
    hint_counts = {4:1, 5:2, 6:2, 7:3, 8:3, 9:4, 10:4, 11:5}
    num_letters = hint_counts.get(length, 1)

    # Считаем буквы в secret
    secret_counter = Counter(secret)

    # Выбираем кандидатов: разная позиция, но >= num_letters общих символов
    candidates = []
    for w in WORDLIST:
        if len(w) != length or w == secret:
            continue
        w_counter = Counter(w)
        # пересечение счётчиков по минимуму
        common = sum(min(secret_counter[ch], w_counter[ch]) for ch in w_counter)
        if common == num_letters:
            candidates.append(w)

    if not candidates:
        await update.message.reply_text("К сожалению, подходящих подсказок нет.")
        return GUESSING

    hint_word = random.choice(candidates)

    # Отмечаем в JSON, что подсказка взята
    cg["hint_used"] = True
    save_store(store)

    await update.message.reply_text(f"🔍 Подсказка: {hint_word}")
    return GUESSING


async def hint_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщение, если /hint вызвали не во время игры."""
    await update.message.reply_text("Эту команду можно использовать только во время игры.")
    # если сейчас выбираем длину — останемся в ASK_LENGTH, иначе в GUESSING
    return context.user_data.get("state", ASK_LENGTH)


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


async def only_outside_game(update, context):
    await update.message.reply_text("Эту команду можно использовать только вне игры.")
    # вернём то состояние, в котором сейчас юзер:
    return context.user_data.get("state", ConversationHandler.END)


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
    context.user_data["in_feedback"] = True
    return FEEDBACK_CHOOSE


async def feedback_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        context.user_data.pop("in_feedback", None)
        context.user_data["just_done"] = True
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
    word = update.message.text.strip().lower()
    target = context.user_data["fb_target"]

    if SUGGESTIONS_FILE.exists() and SUGGESTIONS_FILE.stat().st_size >= 1_000_000:
        await update.message.reply_text(
            "Прости, сейчас нельзя добавить новое слово — файл предложений уже слишком большой."
        )
        # Сбрасываем режим фидбека
        context.user_data.pop("in_feedback", None)
        context.user_data["just_done"] = True
        return ConversationHandler.END
    
    # подтянем свежие предложения
    suggestions = load_suggestions()

    if target == "black":
        if word not in WORDLIST:
            resp = "Нельзя: такого слова нет в основном словаре."
        elif word in vocabulary.get("black_list", []) or word in suggestions["black"]:
            resp = "Нельзя: слово уже в чёрном списке."
        else:
            suggestions["black"].append(word)
            save_suggestions(suggestions)
            resp = "Спасибо, добавил в предложения для чёрного списка."
    else:  # white
        # Сначала проверяем длину
        if not (4 <= len(word) <= 11):
            resp = "Нельзя: длина слова должна быть от 4 до 11 символов."
        # Потом — что слово уже есть в основном словаре
        elif word in WORDLIST:
            resp = "Нельзя: такое слово уже есть в основном словаре."
        # Потом — что его уже предлагали
        elif word in vocabulary.get("white_list", []) or word in suggestions["white"]:
            resp = "Нельзя: слово уже в белом списке."
        else:
            # Всё ок — добавляем
            suggestions["white"].append(word)
            save_suggestions(suggestions)
            resp = "Спасибо, добавил в предложения для белого списка."

    await update.message.reply_text(resp)
    context.user_data.pop("in_feedback", None)
    context.user_data["just_done"] = True
    return ConversationHandler.END


async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def block_during_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # любой посторонний ввод заглушаем
    await update.message.reply_text(
        "Сейчас идёт ввод для фидбека, нельзя использовать команды."
    )
    # возвращаемся в текущее состояние
    return context.user_data.get("feedback_state", FEEDBACK_CHOOSE)


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


async def dict_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Только админу
    if update.effective_user.id != ADMIN_ID:
        return

    # Собираем весь WORDLIST в единую строку
    data = "\n".join(WORDLIST)
    count = len(WORDLIST)

    # Упаковываем в BytesIO, задаём имя файла
    bio = BytesIO(data.encode("utf-8"))
    bio.name = "wordlist.txt"

    # Отправляем как документ
    await update.message.reply_document(
        document=bio,
        filename="wordlist.txt",
        caption=f"📚 В словаре {count} слов"
    )


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


async def suggestions_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    if update.effective_user.id != ADMIN_ID:
        return
    sugg = load_suggestions()
    black = sugg.get("black", [])
    white = sugg.get("white", [])
    text = (
        "Предложения для чёрного списка:\n"
        + (", ".join(f'"{w}"' for w in black) if black else "— пусто")
        + "\n\nПредложения для белого списка:\n"
        + (", ".join(f'"{w}"' for w in white) if white else "— пусто")
    )
    await update.message.reply_text(text)


async def suggestions_remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Только админ
    if update.effective_user.id != ADMIN_ID:
        return

    # Блокируем во время игры
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u or context.user_data.get("game_active"):
        await update.message.reply_text("Эту команду можно использовать только вне игры.")
        return ConversationHandler.END

    # Если всё ок — запускаем диалог удаления
    await update.message.reply_text(
        "Введи, что удалить (формат):\n"
        "black: слово1, слово2\n"
        "white: слово3, слово4\n\n"
        "Или /cancel для отмены."
    )
    return REMOVE_INPUT


async def suggestions_remove_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    
    context.user_data["in_remove"] = True
    text = update.message.text.strip()
    sugg = load_suggestions()
    removed = {"black": [], "white": []}

    # парсим построчно
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, vals = line.split(":", 1)
        key = key.strip().lower()
        if key not in ("black", "white"):
            continue
        # извлекаем слова через запятую
        words = [w.strip().lower() for w in vals.split(",") if w.strip()]
        for w in words:
            if w in sugg[key]:
                sugg[key].remove(w)
                removed[key].append(w)

    save_suggestions(sugg)

    # формируем ответ
    parts = []
    if removed["black"]:
        parts.append(f'Из чёрного удалено: {", ".join(removed["black"])}')
    if removed["white"]:
        parts.append(f'Из белого удалено: {", ".join(removed["white"])}')
    if not parts:
        parts = ["Ничего не удалено."]
    await update.message.reply_text("\n".join(parts))
    context.user_data.pop("in_remove", None)
    context.user_data["just_done"] = True
    return ConversationHandler.END


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    context.user_data["in_broadcast"] = True
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Введите текст рассылки для всех пользователей:")
    return BROADCAST


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    store = load_store()      # берём тех, кого мы когда-то записали
    failed = []
    for uid in store["users"].keys():
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
        except Exception:
            failed.append(uid)
    msg = "✅ Рассылка успешно отправлена!"
    if failed:
        msg += f"\nНе удалось доставить сообщения пользователям: {', '.join(failed)}"
    await update.message.reply_text(msg)
    context.user_data.pop("in_broadcast", None)
    context.user_data["just_done"] = True
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Рассылка отменена.")
    context.user_data.pop("in_broadcast", None)
    return ConversationHandler.END


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
	
    # отправляем один раз при загрузке
    app.job_queue.run_once(send_activity_periodic, when=0)
    app.job_queue.run_once(send_unfinished_games, when=1)

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
    allow_reentry=True
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
                CommandHandler("hint", hint_not_allowed),
                CommandHandler("reset", reset),
                CommandHandler("my_stats", only_outside_game),
                CommandHandler("global_stats", only_outside_game),
            ],
            GUESSING: [
                CommandHandler("feedback", feedback_not_allowed_guess),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess),
                CommandHandler("start", ignore_guess),
		        CommandHandler("play", ignore_guess),
                CommandHandler("hint", hint),
                CommandHandler("reset", reset),
                CommandHandler("my_stats", only_outside_game),
                CommandHandler("global_stats", only_outside_game),
            ],
        },
        fallbacks=[
            CommandHandler("reset", reset),
       ],
    )
    app.add_handler(conv)

    # 1) просмотр
    app.add_handler(CommandHandler("suggestions_view", suggestions_view))

    # 2) удаление через ConversationHandler
    remove_conv = ConversationHandler(
        entry_points=[CommandHandler("suggestions_remove", suggestions_remove_start)],
        states={
            REMOVE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, suggestions_remove_process),
            ],
        },
        fallbacks=[CommandHandler("cancel", feedback_cancel)],
        allow_reentry=True,
    )
    app.add_handler(remove_conv)

    broadcast_conv = ConversationHandler(
    entry_points=[CommandHandler("broadcast", broadcast_start)],
    states={
        BROADCAST: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send),
        ],
    },
    fallbacks=[CommandHandler("broadcast_cancel", broadcast_cancel)],
    allow_reentry=True,
    )
    app.add_handler(broadcast_conv)

    app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text),
    group=99
    )

    # Глобальные
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hint", hint_not_allowed))
    app.add_handler(CommandHandler("reset", reset_global))
    app.add_handler(CommandHandler("my_stats", my_stats))
    app.add_handler(CommandHandler("global_stats", global_stats))
    app.add_handler(CommandHandler("dict_file", dict_file))
    app.add_handler(CommandHandler("dump_activity", dump_activity))

    store = load_store()

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
