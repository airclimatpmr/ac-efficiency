# -*- coding: utf-8 -*-
"""
Telegram-бот "Падение КПД кондиционера" для AirClimat.pmr.

Полностью бесплатная версия — БЕЗ Anthropic API и вообще без какого-либо
платного ИИ-сервиса. Классификация типа кондиционера и расчёт падения
эффективности обогрева — детерминированные правила + таблицы (см.
classifier.py), плюс инлайн-кнопки там, где раньше решение принимал бы ИИ.

Логика:
1. Клиент пишет модель кондиционера (в любом виде, свободным текстом).
2. Если город для этого чата ещё не известен — бот сначала спрашивает город
   (нужен для погоды), запоминает его на весь чат.
3. Бот пытается по ключевым словам определить тип кондиционера
   (classifier.keyword_guess_type). Если не получилось — показывает кнопки
   с типами на выбор.
4. Если тип "зимний инвертор" — бот пытается угадать мощность (BTU/кВт) по
   тексту; если не вышло — показывает кнопки с классами мощности.
5. Считает % от паспортной мощности по нужной кривой и текущей погоде,
   отвечает клиенту.
6. Каждое взаимодействие логируется в JSONL-файл для ручного разбора.

Настройка:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="..."
    python bot.py

Anthropic/OpenAI/любой другой платный API НЕ используется и не нужен.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from classifier import (
    CATEGORY_NAMES,
    keyword_guess_type,
    guess_capacity_class,
    build_client_message,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

LOG_PATH = os.environ.get("AC_BOT_LOG_PATH", "dialogs.jsonl")

# --- Состояние в памяти процесса (для продакшена лучше вынести в БД) ---

# Город, который клиент назвал: chat_id -> (lat, lon, name). Не задан по
# умолчанию — бот обязательно спросит его при первом сообщении в чате.
CHAT_CITIES: dict[int, tuple[float, float, str]] = {}

# Ждём от клиента город (первое сообщение, либо ответ на вопрос про город):
# chat_id -> {"original_model": str}
PENDING_CITY: dict[int, dict] = {}

# Ждём от клиента выбор ТИПА кнопкой: chat_id -> {"original_model": str}
PENDING_TYPE: dict[int, dict] = {}

# Ждём от клиента выбор МОЩНОСТИ кнопкой: chat_id -> {"original_model": str, "category": str}
PENDING_CAPACITY: dict[int, dict] = {}

_log_lock = asyncio.Lock()


# --------------------------- Погода / геокодинг ---------------------------

async def geocode_city(city_name: str) -> tuple[float, float, str] | None:
    """Ищет координаты города через Open-Meteo Geocoding API (бесплатно, без ключа)."""
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city_name, "count": 1, "language": "ru", "format": "json"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    if not results:
        return None

    top = results[0]
    display_name = top.get("name", city_name)
    country = top.get("country")
    if country:
        display_name = f"{display_name}, {country}"
    return top["latitude"], top["longitude"], display_name


async def get_current_temperature(lat: float, lon: float) -> float:
    """Текущая температура на улице через Open-Meteo (бесплатно, без ключа)."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {"latitude": lat, "longitude": lon, "current": "temperature_2m", "timezone": "auto"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data["current"]["temperature_2m"]


# ------------------------------- Логирование -------------------------------

async def log_dialog(chat_id: int, model_text: str, city_name: str, outdoor_temp: float, result: dict) -> None:
    """Пишет одну строку JSON в лог-файл диалогов для ручного разбора.

    Полезно, чтобы потом руками прогнать лог и проверить, разумно ли бот
    (без ИИ, по правилам) классифицирует реальные модели клиентов, и где
    стоит расширить словарь ключевых слов в classifier.py.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "city": city_name,
        "outdoor_temp": outdoor_temp,
        "model_text": model_text,
        "result": result,
    }
    line = json.dumps(entry, ensure_ascii=False)
    async with _log_lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            logger.exception("Не удалось записать лог диалога в %s", LOG_PATH)


# ------------------------------- Клавиатуры -------------------------------

def type_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Обычный (без инвертора)", callback_data="type:type1")],
        [InlineKeyboardButton("Простой инвертор", callback_data="type:type2")],
        [InlineKeyboardButton("Инвертор с зимним комплектом", callback_data="type:type3")],
        [InlineKeyboardButton("Зимний инвертор", callback_data="type:type4")],
        [InlineKeyboardButton("Суперинвертор / тепловой насос", callback_data="type:type5")],
    ]
    return InlineKeyboardMarkup(rows)


def capacity_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("9 тыс. BTU", callback_data="cap:09K"),
            InlineKeyboardButton("12 тыс. BTU", callback_data="cap:12K"),
        ],
        [
            InlineKeyboardButton("18 тыс. BTU", callback_data="cap:18K"),
            InlineKeyboardButton("24 тыс. BTU", callback_data="cap:24K"),
        ],
        [InlineKeyboardButton("Не знаю мощность", callback_data="cap:unknown")],
    ]
    return InlineKeyboardMarkup(rows)


# --------------------------- Общая часть расчёта ---------------------------

async def finalize_and_reply(
    chat_id: int,
    reply_target,
    model_text: str,
    category: str,
    capacity_class: str | None,
    capacity_was_guessed: bool,
) -> None:
    """Последний шаг: берём погоду, считаем, отвечаем клиенту, логируем."""
    lat, lon, city_name = CHAT_CITIES[chat_id]

    try:
        outdoor_temp = await get_current_temperature(lat, lon)
    except Exception:
        logger.exception("Ошибка получения погоды")
        await reply_target("Не получилось узнать текущую температуру на улице, попробуйте чуть позже.")
        return

    client_message = build_client_message(
        model_text, category, outdoor_temp, capacity_class, capacity_was_guessed
    )

    result_for_log = {
        "category": category,
        "category_name": CATEGORY_NAMES[category],
        "capacity_class": capacity_class,
        "capacity_was_guessed": capacity_was_guessed,
    }
    await log_dialog(chat_id, model_text, city_name, outdoor_temp, result_for_log)

    await reply_target(f"🌡️ Сейчас в {city_name}: {outdoor_temp:.1f}°C\n\n{client_message}")


# ------------------------------ Обработчики ------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот AirClimat.pmr 🌬️\n\n"
        "Напишите модель вашего кондиционера (например: TCL TAC-12CHSA/TPG, "
        "Gree Lyra 12, или просто \"обычный инвертор, 9 тыс. BTU\") — и я "
        "прикину, насколько сейчас падает его эффективность обогрева на "
        "текущей уличной температуре.\n\n"
        "Сначала спрошу ваш город, чтобы взять актуальную погоду — это "
        "разовый вопрос, дальше буду его помнить. Поменять город потом можно "
        "командой /city, например: /city Тирасполь"
    )


async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    city_query = " ".join(context.args).strip() if context.args else ""

    if not city_query:
        await update.message.reply_text("Напишите название города после команды, например: /city Тирасполь")
        return

    await update.message.chat.send_action("typing")

    try:
        geocoded = await geocode_city(city_query)
    except Exception:
        logger.exception("Ошибка геокодирования города %s", city_query)
        await update.message.reply_text("Не получилось найти этот город, попробуйте ещё раз чуть позже.")
        return

    if geocoded is None:
        await update.message.reply_text(f"Не нашёл город «{city_query}». Проверьте написание и попробуйте ещё раз.")
        return

    lat, lon, display_name = geocoded
    CHAT_CITIES[chat_id] = (lat, lon, display_name)
    await update.message.reply_text(f"Готово, теперь буду брать погоду для города: {display_name} ☀️")


async def start_classification(update: Update, chat_id: int, model_text: str) -> None:
    """Первый шаг классификации после того, как город уже известен."""
    guess = keyword_guess_type(model_text)

    if guess is None:
        PENDING_TYPE[chat_id] = {"original_model": model_text}
        await update.message.reply_text(
            "Не смог однозначно понять тип кондиционера по описанию. "
            "Выберите, пожалуйста, ближайший вариант:",
            reply_markup=type_keyboard(),
        )
        return

    await proceed_with_category(update.message.reply_text, chat_id, model_text, guess)


async def proceed_with_category(reply_target, chat_id: int, model_text: str, category: str) -> None:
    """Раз тип известен — решаем, нужна ли мощность, и либо считаем, либо спрашиваем."""
    if category == "type4":
        cap = guess_capacity_class(model_text)
        if cap is None:
            PENDING_CAPACITY[chat_id] = {"original_model": model_text, "category": category}
            await reply_target(
                "Для зимних инверторов мощность сильно влияет на расчёт. "
                "Уточните, пожалуйста, мощность вашего кондиционера:",
                reply_markup=capacity_keyboard(),
            )
            return
        await finalize_and_reply(chat_id, reply_target, model_text, category, cap, capacity_was_guessed=True)
        return

    if category == "type3":
        # Мощность желательна, но необязательна — если не угадали, просто
        # используем разумное значение по умолчанию (12K) и скажем об этом.
        cap = guess_capacity_class(model_text)
        await finalize_and_reply(
            chat_id, reply_target, model_text, category, cap or "12K", capacity_was_guessed=cap is not None
        )
        return

    # type1, type2, type5 — мощность не нужна для расчёта.
    await finalize_and_reply(chat_id, reply_target, model_text, category, None, capacity_was_guessed=False)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    await update.message.chat.send_action("typing")

    # 1. Ждём город?
    if chat_id in PENDING_CITY:
        pending = PENDING_CITY[chat_id]
        try:
            geocoded = await geocode_city(user_text)
        except Exception:
            logger.exception("Ошибка геокодирования города %s", user_text)
            await update.message.reply_text("Не получилось найти этот город, попробуйте написать его ещё раз.")
            return

        if geocoded is None:
            await update.message.reply_text(
                f"Не нашёл город «{user_text}». Попробуйте написать по-другому, "
                "например просто название без области/района."
            )
            return

        lat, lon, city_name = geocoded
        CHAT_CITIES[chat_id] = (lat, lon, city_name)
        PENDING_CITY.pop(chat_id)
        await start_classification(update, chat_id, pending["original_model"])
        return

    # 2. Город ещё неизвестен вообще — это первое сообщение, откладываем его
    #    и спрашиваем город.
    if chat_id not in CHAT_CITIES:
        PENDING_CITY[chat_id] = {"original_model": user_text}
        await update.message.reply_text(
            "Из какого вы города? Это нужно, чтобы узнать текущую температуру на улице."
        )
        return

    # 3. Город уже известен — это новое сообщение с моделью кондиционера.
    #    (Уточнения типа/мощности приходят через кнопки, не сюда.)
    await start_classification(update, chat_id, user_text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    async def reply_target(text: str, reply_markup=None):
        await query.message.reply_text(text, reply_markup=reply_markup)

    if data.startswith("type:"):
        if chat_id not in PENDING_TYPE:
            await reply_target("Этот вопрос уже устарел, напишите модель ещё раз.")
            return
        pending = PENDING_TYPE.pop(chat_id)
        category = data.split(":", 1)[1]
        await proceed_with_category(reply_target, chat_id, pending["original_model"], category)
        return

    if data.startswith("cap:"):
        if chat_id not in PENDING_CAPACITY:
            await reply_target("Этот вопрос уже устарел, напишите модель ещё раз.")
            return
        pending = PENDING_CAPACITY.pop(chat_id)
        cap_value = data.split(":", 1)[1]
        capacity_class = None if cap_value == "unknown" else cap_value
        await finalize_and_reply(
            chat_id,
            reply_target,
            pending["original_model"],
            pending["category"],
            capacity_class or "12K",
            capacity_was_guessed=False,
        )
        return


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("city", set_city))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен (без ИИ, без Anthropic API), лог диалогов пишется в %s", LOG_PATH)
    application.run_polling()


if __name__ == "__main__":
    main()
