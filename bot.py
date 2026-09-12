# -*- coding: utf-8 -*-
"""
Telegram-бот "Падение КПД кондиционера" для AirClimat.pmr.

Логика:
1. Клиент (по желанию) указывает свой город командой /city.
2. Клиент пишет модель кондиционера.
3. Бот получает текущую уличную температуру для города клиента (Open-Meteo,
   без ключа) — если город не указан, используется город по умолчанию.
4. Бот отправляет модель + температуру + системный промпт с оцифрованными
   графиками в Claude (Anthropic API).
5. Claude сам решает, к какому типу относится кондиционер (без привязки к
   конкретным маркам — рассуждает по общим знаниям), при необходимости
   уточняет мощность, и считает падение эффективности по нужной кривой.
6. Каждый диалог (модель -> ответ Claude) логируется в JSONL-файл, чтобы
   потом руками проверить, насколько разумно Claude классифицирует модели.

Настройка:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="..."
    export ANTHROPIC_API_KEY="..."
    export AC_BOT_LOG_PATH="dialogs.jsonl"   # необязательно, есть значение по умолчанию
    python bot.py
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone

import httpx
from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from system_prompt import SYSTEM_PROMPT

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Модель для этой задачи: классификация + интерполяция по таблице — не нужен
# самый мощный (и дорогой) вариант, Haiku отлично справится и обойдётся
# дешевле при большом потоке сообщений.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Куда писать лог диалогов (модель -> ответ Claude) для последующего разбора.
LOG_PATH = os.environ.get("AC_BOT_LOG_PATH", "dialogs.jsonl")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Простое хранение состояния "ждём уточнение" на процесс (для продакшена
# лучше вынести в redis/бд, но для MVP хватит словаря в памяти).
# Храним не только исходную модель, но и накопленные ответы клиента —
# уточнений может быть несколько подряд (сначала тип, потом мощность).
PENDING_CLARIFICATION: dict[int, dict] = {}

# Город, который клиент назвал: chat_id -> (lat, lon, name). По умолчанию
# города НЕТ — бот обязательно спросит его один раз при первом обращении,
# прежде чем сможет что-либо посчитать (город нужен, чтобы узнать погоду).
CHAT_CITIES: dict[int, tuple[float, float, str]] = {}

# Ждём от клиента именно город (первое сообщение в чате, либо ответ на
# запрос города): chat_id -> {"original_model":, "answers": []} — то, что
# нужно будет обработать сразу после того, как город станет известен.
PENDING_CITY: dict[int, dict] = {}

# Лок, чтобы параллельные обработчики не писали в лог-файл одновременно и не
# перемешали строки.
_log_lock = asyncio.Lock()


async def geocode_city(city_name: str) -> tuple[float, float, str] | None:
    """Ищет координаты города через Open-Meteo Geocoding API (без ключа).

    Возвращает (lat, lon, отображаемое_имя) или None, если ничего не нашлось.
    """
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
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m",
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data["current"]["temperature_2m"]


def ask_claude(model_text: str, outdoor_temp: float, extra_answers: list[str] | None = None) -> dict:
    """Отправляет запрос в Claude и парсит JSON-ответ."""
    user_content = (
        f"Модель кондиционера, которую написал клиент: {model_text}\n"
        f"Текущая температура на улице: {outdoor_temp:.1f}°C\n"
    )
    if extra_answers:
        for i, answer in enumerate(extra_answers, start=1):
            user_content += f"Уточнение клиента #{i} (ответ на твой предыдущий вопрос): {answer}\n"

    message = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw_text = message.content[0].text.strip()

    # На случай если модель обернёт JSON в ```json ... ```
    raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Не удалось распарсить ответ Claude: %s", raw_text)
        return {
            "needs_clarification": False,
            "category": None,
            "capacity_ratio_percent": None,
            "client_message": (
                "Не получилось точно оценить эту модель, но в целом: чем ниже "
                "температура на улице, тем сильнее падает эффективность "
                "обогрева у любого кондиционера. Уточните модель или "
                "напишите нам напрямую — поможем разобраться."
            ),
            "_parse_error": True,
            "_raw_text": raw_text,
        }


async def log_dialog(
    chat_id: int,
    original_model: str,
    extra_answers: list[str],
    outdoor_temp: float,
    city_name: str,
    result: dict,
) -> None:
    """Пишет одну строку JSON в лог-файл диалогов для ручного разбора.

    Формат JSONL (один JSON-объект на строку) — удобно смотреть через
    `tail -f`, парсить построчно скриптом или залить в pandas/BigQuery позже.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "city": city_name,
        "outdoor_temp": outdoor_temp,
        "model_text": original_model,
        "extra_answers": extra_answers,
        "claude_result": result,
    }
    line = json.dumps(entry, ensure_ascii=False)
    async with _log_lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            logger.exception("Не удалось записать лог диалога в %s", LOG_PATH)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот AirClimat.pmr 🌬️\n\n"
        "Напишите модель вашего кондиционера (например: TCL TAC-12CHSA/TPG, "
        "Gree Lyra 12, или просто \"обычный инвертор без зимнего комплекта, "
        "9 тыс. BTU\") — и я прикину, насколько сейчас падает его "
        "эффективность обогрева на текущей уличной температуре.\n\n"
        "Сначала спрошу ваш город, чтобы взять актуальную погоду — "
        "это разовый вопрос, дальше буду его помнить. Поменять город потом "
        "можно командой /city, например: /city Тирасполь"
    )


async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет сменить уже известный город в любой момент."""
    chat_id = update.effective_chat.id
    city_query = " ".join(context.args).strip() if context.args else ""

    if not city_query:
        await update.message.reply_text(
            "Напишите название города после команды, например: /city Тирасполь"
        )
        return

    await update.message.chat.send_action("typing")

    try:
        geocoded = await geocode_city(city_query)
    except Exception:
        logger.exception("Ошибка геокодирования города %s", city_query)
        await update.message.reply_text(
            "Не получилось найти этот город, попробуйте ещё раз чуть позже."
        )
        return

    if geocoded is None:
        await update.message.reply_text(
            f"Не нашёл город «{city_query}». Проверьте написание и попробуйте ещё раз."
        )
        return

    lat, lon, display_name = geocoded
    CHAT_CITIES[chat_id] = (lat, lon, display_name)
    await update.message.reply_text(
        f"Готово, теперь буду брать погоду для города: {display_name} ☀️"
    )


async def resolve_city_and_continue(
    update: Update, chat_id: int, city_text: str
) -> tuple[float, float, str] | None:
    """Геокодирует город, который клиент написал в ответ на вопрос бота.

    Возвращает (lat, lon, name) при успехе, иначе отправляет клиенту
    сообщение об ошибке и возвращает None.
    """
    try:
        geocoded = await geocode_city(city_text)
    except Exception:
        logger.exception("Ошибка геокодирования города %s", city_text)
        await update.message.reply_text(
            "Не получилось найти этот город, попробуйте написать его ещё раз."
        )
        return None

    if geocoded is None:
        await update.message.reply_text(
            f"Не нашёл город «{city_text}». Попробуйте написать по-другому, "
            "например просто название без области/района."
        )
        return None

    lat, lon, display_name = geocoded
    CHAT_CITIES[chat_id] = (lat, lon, display_name)
    return lat, lon, display_name


async def run_classification(
    update: Update,
    chat_id: int,
    original_model: str,
    extra_answers: list[str],
    lat: float,
    lon: float,
    city_name: str,
) -> None:
    """Общая часть: погода -> Claude -> ответ клиенту (+лог)."""
    try:
        outdoor_temp = await get_current_temperature(lat, lon)
    except Exception:
        logger.exception("Ошибка получения погоды")
        await update.message.reply_text(
            "Не получилось узнать текущую температуру на улице, попробуйте чуть позже."
        )
        return

    result = ask_claude(original_model, outdoor_temp, extra_answers)

    # Логируем каждый вызов Claude — и промежуточные уточнения, и финальный
    # ответ — чтобы потом можно было руками прогнать лог и проверить, разумно
    # ли бот классифицирует модели, и где промпт стоит подправить.
    await log_dialog(chat_id, original_model, extra_answers, outdoor_temp, city_name, result)

    if result.get("needs_clarification"):
        PENDING_CLARIFICATION[chat_id] = {
            "original_model": original_model,
            "answers": extra_answers,
        }
        question = result.get("clarifying_question") or (
            "Уточните, пожалуйста: это обычный инвертор или у него есть "
            "зимний комплект/подогрев поддона, и до какой температуры "
            "заявлен обогрев?"
        )
        await update.message.reply_text(question)
        return

    client_message = result.get("client_message") or (
        "Не получилось сформировать ответ, попробуйте переформулировать "
        "модель кондиционера."
    )
    await update.message.reply_text(
        f"🌡️ Сейчас в {city_name}: {outdoor_temp:.1f}°C\n\n{client_message}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    await update.message.chat.send_action("typing")

    # 1. Если мы ждали от этого чата именно город (первое обращение вообще,
    #    либо ответ на вопрос про город после уточнения модели/мощности) —
    #    это сообщение и есть город, дальше продолжаем с тем, что копилось.
    if chat_id in PENDING_CITY:
        pending = PENDING_CITY[chat_id]
        geocoded = await resolve_city_and_continue(update, chat_id, user_text)
        if geocoded is None:
            return  # PENDING_CITY остаётся, ждём повторную попытку
        PENDING_CITY.pop(chat_id)
        lat, lon, city_name = geocoded
        await run_classification(
            update, chat_id, pending["original_model"], pending["answers"], lat, lon, city_name
        )
        return

    # 2. Определяем исходную модель и накопленные уточнения.
    original_model = user_text
    extra_answers: list[str] = []
    if chat_id in PENDING_CLARIFICATION:
        # Это ответ на ранее заданный Claude уточняющий вопрос (тип/мощность).
        state = PENDING_CLARIFICATION.pop(chat_id)
        original_model = state["original_model"]
        extra_answers = state["answers"] + [user_text]

    # 3. Город нужен обязательно (это единственный источник погоды) — если
    #    для этого чата он ещё не известен, спрашиваем его сейчас и
    #    откладываем обработку модели до ответа.
    if chat_id not in CHAT_CITIES:
        PENDING_CITY[chat_id] = {
            "original_model": original_model,
            "answers": extra_answers,
        }
        await update.message.reply_text(
            "Из какого вы города? Это нужно, чтобы узнать текущую температуру на улице."
        )
        return

    lat, lon, city_name = CHAT_CITIES[chat_id]
    await run_classification(update, chat_id, original_model, extra_answers, lat, lon, city_name)


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("city", set_city))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен, лог диалогов пишется в %s", LOG_PATH)
    application.run_polling()


if __name__ == "__main__":
    main()
