import os
import re
import logging
import threading
import asyncio
import random
from flask import Flask

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from google import genai
from google.genai import types
import psycopg2
from psycopg2 import pool

# ─────────────────────────────────────────────────────────────
# КОНФИГУРАЦИЯ (берётся из переменных окружения Render)
# ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

MODEL_NAME = "gemini-3.5-flash-lite"

# Приветственное сообщение (можно менять здесь)
WELCOME_MESSAGE = """Привет! Меня зовут Трезвый, я виртуальный помощник в команде онлайн-курса PROТрезвость по освобождению и выздоровлению от алкогольной и наркотической зависимости. Я искусственный интеллект, но работаю по методике курса и разделяю позицию его специалистов.

Я здесь, чтобы помочь тебе на пути к счастливой сознательной трезвости. Если у тебя есть вопросы по прохождению курса — смело задавай их мне. Если что-то беспокоит, не получается, ты в отчаянии, застрял на месте и не знаешь, как быть — я рядом.

С чем ты пришёл сегодня: хочешь разобраться в своём сценарии, спросить что-то по курсу, или просто поговорить?"""

# ─────────────────────────────────────────────────────────────
# ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# ЧТЕНИЕ ФАЙЛОВ LOREBOOK (локально из папки репозитория)
# ─────────────────────────────────────────────────────────────

def read_file(filename: str) -> str:
    """Читает файл из папки с ботом. Если файла нет — возвращает пустую строку."""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"Файл {filename} не найден")
        return ""

SOUL = read_file("soul.md")
CORE = read_file("core.md")

# Приложения подгружаются по необходимости
APPENDICES = {
    "safety": read_file("core.md"),  # безопасность уже в core, но можно вынести отдельно
    "fear": read_file("appendix_d.md"),
    "motivation": read_file("appendix_c.md"),
    "map": read_file("appendix_a.md"),
    "hypotheses": read_file("appendix_b.md"),
    "change": read_file("appendix_e.md"),
    "history": read_file("appendix_f.md"),
    "quotes": read_file("appendix_g.md"),
}

# ─────────────────────────────────────────────────────────────
# ВЫБОР ПРИЛОЖЕНИЯ ПО КЛЮЧЕВЫМ СЛОВАМ
# Приоритет: безопасность → страх срыва → мотивация → остальное
# ─────────────────────────────────────────────────────────────

# Ключевые слова для безопасности (самый высокий приоритет)
SAFETY_KEYWORDS = [
    "не хочу жить", "суицид", "покончить", "передоз", " overdose",
    "не дышит", "судороги", "психоз", "галлюцинац", "скорая",
    "умереть", "навредить себе", "порезал", "таблеток", "отрав",
]

FEAR_KEYWORDS = [
    "сорв", "срыва", "тяга", "хочу употребить", "хочу выпить",
    "боюсь", "страх", "гарант", "не уверен", "смогу ли",
]

MOTIVATION_KEYWORDS = [
    "не вижу смысла", "зачем бросать", "мотивац", "смысл",
    "не хочу бросать", "стоит ли", "ради чего", "что я получу",
    "потери", "приобрет", "выгод",
]

MAP_KEYWORDS = [
    "разобрать сценарий", "мой сценарий", "анкета", "карта",
    "расскажу о себе", "хочу разобраться", "почему я",
]

CHANGE_KEYWORDS = [
    "работ", "переезд", "сменить", "уволиться", "переехать",
    "барьер", "не могу уйти",
]

TRAUMA_KEYWORDS = [
    "травм", "детств", "насили", "воспоминани", "прошлое",
]

SEX_KEYWORDS = [
    "секс", "порно", "мастурбац", "фантаз", "возбужд",
]

def pick_appendix(user_message: str) -> str:
    """
    Выбирает одно приложение по ключевым словам.
    Приоритет: безопасность → страх срыва → мотивация → остальное.
    Возвращает текст приложения или пустую строку, если ничего не подошло.
    """
    text = user_message.lower()

    # 1. Безопасность — всегда первая
    for kw in SAFETY_KEYWORDS:
        if kw in text:
            return APPENDICES["safety"]

    # 2. Страх срыва
    for kw in FEAR_KEYWORDS:
        if kw in text:
            return APPENDICES["fear"]

    # 3. Мотивация
    for kw in MOTIVATION_KEYWORDS:
        if kw in text:
            return APPENDICES["motivation"]

    # 4. Остальное — по первому совпадению
    for kw in MAP_KEYWORDS:
        if kw in text:
            return APPENDICES["map"]

    for kw in CHANGE_KEYWORDS:
        if kw in text:
            return APPENDICES["change"]

    for kw in TRAUMA_KEYWORDS:
        if kw in text:
            return APPENDICES["hypotheses"]  # травма → гипотезы (там есть ограничения)

    for kw in SEX_KEYWORDS:
        if kw in text:
            return APPENDICES["hypotheses"]

    return ""  # ничего не подошло — только core

# ─────────────────────────────────────────────────────────────
# ПОДКЛЮЧЕНИЕ К POSTGRESQL (Aiven)
# ─────────────────────────────────────────────────────────────

db_pool = None

def init_db():
    """Инициализирует пул соединений и создаёт таблицу, если её нет."""
    global db_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — история диалогов не будет сохраняться")
        return

    try:
        db_pool = pool.SimpleConnectionPool(1, 5, DATABASE_URL)
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    role VARCHAR(10) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id);
            """)
        conn.commit()
        db_pool.putconn(conn)
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации базы: {e}")

def save_message(user_id: int, role: str, content: str):
    """Сохраняет сообщение в историю."""
    if not db_pool:
        return
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
                (user_id, role, content),
            )
        conn.commit()
        db_pool.putconn(conn)
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения: {e}")

def get_history(user_id: int, limit: int = 20) -> list:
    """Возвращает последние N сообщений пользователя (в хронологическом порядке)."""
    if not db_pool:
        return []
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
        db_pool.putconn(conn)
        return list(reversed(rows))  # разворачиваем в хронологический порядок
    except Exception as e:
        logger.error(f"Ошибка чтения истории: {e}")
        return []
# ─────────────────────────────────────────────────────────────
# GEMINI КЛИЕНТ
# ─────────────────────────────────────────────────────────────

gemini_client = None

def init_gemini():
    """Инициализирует клиент Gemini."""
    global gemini_client
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY не задан")
        return
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    logger.info("Gemini клиент инициализирован")

def build_system_prompt(appendix_text: str) -> str:
    """
    Собирает системный промпт из soul.md, core.md и выбранного приложения.
    """
    parts = [SOUL, CORE]
    if appendix_text:
        parts.append(appendix_text)
    return "\n\n---\n\n".join(parts)

async def get_gemini_response(user_id: int, user_message: str) -> str:
    """
    Отправляет запрос в Gemini с учётом истории и выбранного приложения.
    """
    if not gemini_client:
        return "Извини, я сейчас не могу ответить. Попробуй позже."

    # Выбираем приложение по ключевым словам
    appendix_text = pick_appendix(user_message)
    system_prompt = build_system_prompt(appendix_text)

    # Получаем историю диалога
    history = get_history(user_id, limit=20)

    # Собираем contents: история + текущее сообщение
    contents = []
    for role, content in history:
        # В Gemini роли: user и model
        gemini_role = "user" if role == "user" else "model"
        contents.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part.from_text(text=content)],
            )
        )

    # Добавляем текущее сообщение
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_message)],
        )
    )

    # ВАЖНО: Gemini 3.5 Flash-Lite не поддерживает последний ход с ролью model
    # и не принимает temperature/top_p/top_k (они игнорируются или вызывают ошибку) [citation:1]
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                ),
            )
            logger.info(f"ТОКЕНОВ НА ВХОДЕ: {response.usage_metadata.prompt_token_count}")
            return response.text if response.text else "Не могу сформулировать ответ."
        except Exception as e:
            error_str = str(e).lower()
            is_overloaded = "503" in error_str or "unavailable" in error_str or "high demand" in error_str

            if is_overloaded and attempt < max_retries - 1:
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Gemini перегружен, попытка {attempt + 1} из {max_retries}. Ждём {wait_time:.1f}с...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"Ошибка Gemini после {attempt + 1} попыток: {e}")
                return "Сейчас я немного перегружен. Попробуй написать ещё раз через минуту."

# ─────────────────────────────────────────────────────────────
# ОБРАБОТЧИКИ TELEGRAM
# ─────────────────────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    user = update.effective_user
    user_id = user.id
    logger.info(f"Новый пользователь: {user_id} ({user.first_name})")

    # Сохраняем приветствие в историю (опционально)
    save_message(user_id, "model", WELCOME_MESSAGE)

    await update.message.reply_text(WELCOME_MESSAGE)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех текстовых сообщений."""
    user_id = update.effective_user.id
    user_message = update.message.text

    if not user_message:
        return

    chat_type = update.message.chat.type
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        is_mentioned = f"@{bot_username}" in user_message
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        if not is_mentioned and not is_reply_to_bot:
            return

    logger.info(f"Сообщение от {user_id}: {user_message[:50]}...")

    # Сохраняем сообщение пользователя
    save_message(user_id, "user", user_message)

    # 1. Отправляем заглушку (она будет удалена после ответа)
    bot_msg = await update.message.reply_text("⏳ Думаю…")

    # 2. Получаем ответ от Gemini
    response_text = await get_gemini_response(user_id, user_message)

    # 3. Сохраняем ответ бота в историю
    save_message(user_id, "model", response_text)

    # 4. Удаляем заглушку
    try:
        await bot_msg.delete()
    except Exception as e:
        logger.debug(f"Не удалось удалить заглушку: {e}")

    # 5. Отправляем ответ (цитирование только в группах)
    should_reply = update.message.chat.type in ("group", "supergroup")

    if len(response_text) <= 4096:
        if should_reply:
            await update.message.reply_text(
                response_text,
                reply_to_message_id=update.message.message_id
            )
        else:
            await update.message.reply_text(response_text)
    else:
        if should_reply:
            await update.message.reply_text(
                response_text[:4096],
                reply_to_message_id=update.message.message_id
            )
        else:
            await update.message.reply_text(response_text[:4096])
        remaining = response_text[4096:]
        while remaining:
            chunk = remaining[:4096]
            remaining = remaining[4096:]
            await update.message.reply_text(chunk)
            
# ─────────────────────────────────────────────────────────────
# FLASK ДЛЯ HEALTH-CHECK (чтобы Render не ругался на отсутствие порта)
# ─────────────────────────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    """Запускает Flask в отдельном потоке."""
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ─────────────────────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────────────────────

def main():
    # Инициализация
    init_db()
    init_gemini()

    # Запускаем Flask в отдельном потоке (daemon, чтобы завершался вместе с основным)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask health-check запущен")

    # Создаём и запускаем Telegram-бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Бот запущен (polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()