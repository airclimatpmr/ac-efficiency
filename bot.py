# -*- coding: utf-8 -*-
"""
Telegram-бот AirClimat.pmr — многофункциональный, полностью бесплатный
(без Anthropic/OpenAI/любого платного ИИ-API).

Функции:
1. Падение КПД обогрева по модели кондиционера и текущей погоде (как раньше).
2. /calc — подбор рекомендуемой мощности по площади помещения + сбор лида
   (контакт клиента) с уведомлением админу.
3. /warranty — проверка гарантии по ранее зарегистрированной установке.
4. Автоматические напоминания о сезонном ТО и запрос отзыва через N дней
   после установки (фоновая ежедневная задача).
5. Склад материалов через чат (админ-команды /stock, /stock_use).
6. Учёт актов выполненных работ (/act — адрес + фото).
7. B2B-трекер обслуживания точек по контракту (/b2b_add, /b2b_done, /b2b_list)
   с ежедневным напоминанием админу о просроченных точках.

Все данные — в SQLite (db.py), переживают перезапуск бота (см. README про
Railway Volume для персистентности на Railway).

Настройка:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="..."
    export ADMIN_CHAT_IDS="123456789,987654321"   # ваш и Евгения chat_id, через запятую
    python bot.py

Свой chat_id узнать через команду /whoami в самом боте.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone, date

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
import power_calc
import db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LOG_PATH = os.environ.get("AC_BOT_LOG_PATH", "dialogs.jsonl")

ADMIN_CHAT_IDS: set[int] = {
    int(x) for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip().isdigit()
}

REVIEW_REQUEST_DAYS_AFTER_INSTALL = int(os.environ.get("REVIEW_REQUEST_DAYS_AFTER_INSTALL", "3"))
MAINTENANCE_REMINDER_INTERVAL_DAYS = int(os.environ.get("MAINTENANCE_REMINDER_INTERVAL_DAYS", "180"))
B2B_CHECK_HOUR_UTC = int(os.environ.get("B2B_CHECK_HOUR_UTC", "7"))  # ежедневная фоновая проверка

# --- Состояние в памяти процесса для многошаговых диалогов (не для данных,
#     которые должны переживать рестарт — те в db.py) ---

CHAT_CITIES: dict[int, tuple[float, float, str]] = {}
PENDING_CITY: dict[int, dict] = {}
PENDING_TYPE: dict[int, dict] = {}
PENDING_CAPACITY: dict[int, dict] = {}

# /calc: chat_id -> {"step": "area"/"contact", "area":, "top_floor":, "sunny":, "client_type":, "recommended":}
PENDING_CALC: dict[int, dict] = {}

# /act (админ): chat_id -> {"address": str} — ждём фото или "пропустить"
PENDING_ACT: dict[int, dict] = {}

# Ждём текст отзыва после автоматического запроса: chat_id -> {"installation_id": int}
PENDING_REVIEW: dict[int, dict] = {}

_log_lock = asyncio.Lock()


def is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_CHAT_IDS


# --------------------------- Погода / геокодинг ---------------------------

async def geocode_city(city_name: str) -> tuple[float, float, str] | None:
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
    url = "https://api.open-meteo.com/v1/forecast"
    params = {"latitude": lat, "longitude": lon, "current": "temperature_2m", "timezone": "auto"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data["current"]["temperature_2m"]


# ------------------------------- Логирование -------------------------------

async def log_dialog(chat_id: int, model_text: str, city_name: str, outdoor_temp: float, result: dict) -> None:
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


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Да", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton("Нет", callback_data=f"{prefix}:no"),
    ]])


def client_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Физлицо", callback_data="calctype:физлицо"),
        InlineKeyboardButton("Компания", callback_data="calctype:компания"),
    ]])


def skip_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data=callback_data)]])


# --------------------- Часть 1: КПД-калькулятор (как раньше) ---------------------

async def finalize_and_reply(
    chat_id: int,
    reply_target,
    model_text: str,
    category: str,
    capacity_class: str | None,
    capacity_was_guessed: bool,
) -> None:
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


async def start_classification(update: Update, chat_id: int, model_text: str) -> None:
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
        cap = guess_capacity_class(model_text)
        await finalize_and_reply(
            chat_id, reply_target, model_text, category, cap or "12K", capacity_was_guessed=cap is not None
        )
        return

    await finalize_and_reply(chat_id, reply_target, model_text, category, None, capacity_was_guessed=False)


# --------------------- Часть 2: /calc — подбор мощности + лид ---------------------

async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    PENDING_CALC[chat_id] = {"step": "area"}
    await update.message.reply_text(
        "Посчитаю ориентировочную мощность кондиционера под ваше помещение.\n\n"
        "Какая площадь помещения в м²? (просто число, например: 22)"
    )


async def handle_calc_text(update: Update, chat_id: int, text: str) -> None:
    state = PENDING_CALC[chat_id]

    if state["step"] == "area":
        try:
            area = float(text.replace(",", ".").strip())
            if area <= 0 or area > 500:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Не получилось распознать площадь. Введите число в м², например: 22")
            return
        state["area"] = area
        state["step"] = "floor"
        await update.message.reply_text(
            "Это последний этаж (под крышей/мансарда)?", reply_markup=yes_no_keyboard("calcfloor")
        )
        return

    if state["step"] == "contact":
        contact = text.strip()
        await finish_calc_lead(chat_id, update.message.reply_text, contact)
        return

    # Шаги floor/sunny/clienttype ожидают нажатия кнопки, а не текст.
    await update.message.reply_text("Пожалуйста, воспользуйтесь кнопками выше 👆")


async def finish_calc_lead(chat_id: int, reply_target, contact: str | None) -> None:
    state = PENDING_CALC.pop(chat_id, None)
    if state is None:
        return

    text, result = power_calc.build_recommendation_message(state["area"], state["top_floor"], state["sunny"])
    recommended = result["capacity_class"] or "индивидуальный расчёт"

    lead_id = db.add_lead(
        chat_id=chat_id,
        area=state["area"],
        top_floor=state["top_floor"],
        sunny=state["sunny"],
        client_type=state["client_type"],
        recommended_capacity=recommended,
        contact=contact,
    )

    await reply_target(f"{text}\n\nСпасибо! Заявка №{lead_id} передана менеджеру.")

    admin_text = (
        f"📥 Новая заявка с калькулятора мощности (№{lead_id})\n"
        f"Площадь: {state['area']:.0f} м²\n"
        f"Последний этаж: {'да' if state['top_floor'] else 'нет'}\n"
        f"Солнечная сторона: {'да' if state['sunny'] else 'нет'}\n"
        f"Клиент: {state['client_type']}\n"
        f"Рекомендованная мощность: {recommended}\n"
        f"Контакт: {contact or 'не оставил'}\n"
        f"Telegram chat_id клиента: {chat_id}"
    )
    await notify_admins(None, admin_text)


async def notify_admins(bot, text: str) -> None:
    """Отправляет текст всем ADMIN_CHAT_IDS. bot может быть None — тогда
    берём его из глобального APPLICATION (см. main())."""
    target_bot = bot or APPLICATION.bot
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await target_bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Не удалось отправить уведомление админу %s", admin_id)


# --------------------- Часть 3: /warranty ---------------------

async def cmd_warranty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    inst = db.get_installation_by_chat(chat_id)
    if inst is None:
        await update.message.reply_text(
            "Не нашёл запись об установке для вашего аккаунта. Если кондиционер "
            "устанавливали мы — напишите нам напрямую, уточним по номеру телефона."
        )
        return

    install_date = date.fromisoformat(inst["install_date"])
    warranty_months = inst["warranty_months"]
    # Простая арифметика месяцев без внешних зависимостей (dateutil не нужен).
    end_year = install_date.year + (install_date.month - 1 + warranty_months) // 12
    end_month = (install_date.month - 1 + warranty_months) % 12 + 1
    end_day = min(install_date.day, 28)  # избегаем проблем с несуществующими датами (31 февраля и т.п.)
    warranty_end = date(end_year, end_month, end_day)

    days_left = (warranty_end - date.today()).days

    if days_left > 0:
        await update.message.reply_text(
            f"Модель: {inst['model_text']}\n"
            f"Дата установки: {install_date.strftime('%d.%m.%Y')}\n"
            f"Гарантия действует до {warranty_end.strftime('%d.%m.%Y')} "
            f"(осталось {days_left} дн.)."
        )
    else:
        await update.message.reply_text(
            f"Модель: {inst['model_text']}\n"
            f"Дата установки: {install_date.strftime('%d.%m.%Y')}\n"
            f"Гарантия закончилась {warranty_end.strftime('%d.%m.%Y')}. "
            f"Если что-то беспокоит — можем провести платную диагностику."
        )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Ваш chat_id: {update.effective_chat.id}\n\n"
        f"Если вы владелец бота — добавьте это число в переменную ADMIN_CHAT_IDS "
        f"на Railway (через запятую, если несколько), чтобы получать уведомления "
        f"о заявках и напоминания."
    )


# --------------------- Часть 4: admin — установки/гарантия ---------------------

async def cmd_new_install(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        await update.message.reply_text(
            "Формат: /new_install Клиент/телефон | Модель | [Класс мощности] | "
            "[Город] | [Дата установки ГГГГ-ММ-ДД] | [Гарантия, мес] | [chat_id клиента]\n\n"
            "Обязательны только первые два поля. Пример:\n"
            "/new_install Иванов +373... | TCL 12K простой инвертор | 12K | Тирасполь"
        )
        return

    client_label = parts[0]
    model_text = parts[1]
    capacity_class = parts[2] if len(parts) > 2 and parts[2] else None
    city = parts[3] if len(parts) > 3 and parts[3] else None
    install_date = date.fromisoformat(parts[4]) if len(parts) > 4 and parts[4] else date.today()
    warranty_months = int(parts[5]) if len(parts) > 5 and parts[5] else 24
    client_chat_id = int(parts[6]) if len(parts) > 6 and parts[6].strip().isdigit() else None

    inst_id = db.add_installation(
        chat_id=client_chat_id,
        client_label=client_label,
        model_text=model_text,
        capacity_class=capacity_class,
        city=city,
        install_date=install_date,
        warranty_months=warranty_months,
    )
    await update.message.reply_text(
        f"Установка №{inst_id} сохранена для «{client_label}»."
        + ("" if client_chat_id else "\n(chat_id клиента не указан — автоматические напоминания и отзывы "
                                      "не будут отправлены, только внутренний учёт)")
    )


# --------------------- Часть 5: admin — склад ---------------------

async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    items = db.list_inventory()
    if not items:
        await update.message.reply_text(
            "Склад пуст. Добавьте материал: /stock_init Название | Ед.изм | Количество"
        )
        return

    lines = [f"• {i['name']}: {i['quantity']:g} {i['unit']}" for i in items]
    await update.message.reply_text("Остатки на складе:\n" + "\n".join(lines))


async def cmd_stock_init(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        await update.message.reply_text("Формат: /stock_init Название | Ед.изм | Количество")
        return

    name, unit, qty_raw = parts[0], parts[1], parts[2]
    try:
        qty = float(qty_raw.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Количество должно быть числом.")
        return

    db.upsert_inventory_item(name, unit, qty)
    await update.message.reply_text(f"Материал «{name}» добавлен на склад: {qty:g} {unit}.")


async def cmd_stock_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        await update.message.reply_text("Формат: /stock_use Название | Количество (расход)")
        return

    name, qty_raw = parts[0], parts[1]
    try:
        qty = float(qty_raw.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Количество должно быть числом.")
        return

    new_qty = db.adjust_inventory(name, -abs(qty))
    if new_qty is None:
        await update.message.reply_text(
            f"Материала «{name}» нет на складе. Сначала добавьте: /stock_init {name} | ед.изм | количество"
        )
        return

    warn = " ⚠️ остаток отрицательный, проверьте учёт!" if new_qty < 0 else ""
    await update.message.reply_text(f"Списано {qty:g}. Остаток «{name}»: {new_qty:g}.{warn}")


# --------------------- Часть 6: admin — акты выполненных работ ---------------------

async def cmd_act(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    address = " ".join(context.args).strip()
    if not address:
        await update.message.reply_text("Формат: /act Адрес объекта")
        return

    PENDING_ACT[chat_id] = {"address": address}
    await update.message.reply_text(
        "Адрес записал. Пришлите фото объекта, либо напишите «пропустить», если фото не нужно."
    )


async def cmd_acts_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    pending = db.list_pending_acts()
    if not pending:
        await update.message.reply_text("Нет актов в очереди на оформление.")
        return

    lines = [f"№{a['id']} — {a['address']} ({a['created_at'][:16]})" for a in pending]
    await update.message.reply_text("Акты в очереди:\n" + "\n".join(lines))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in PENDING_ACT:
        return  # фото вне контекста /act — просто игнорируем

    pending = PENDING_ACT.pop(chat_id)
    photo_file_id = update.message.photo[-1].file_id
    act_id = db.add_act(chat_id, pending["address"], photo_file_id)
    await update.message.reply_text(f"Акт №{act_id} по адресу «{pending['address']}» сохранён с фото.")


# --------------------- Часть 7: admin — B2B-трекер ---------------------

async def cmd_b2b_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        await update.message.reply_text(
            "Формат: /b2b_add Название договора | Название точки | [Периодичность, дней]"
        )
        return

    contract_name, point_name = parts[0], parts[1]
    interval_days = int(parts[2]) if len(parts) > 2 and parts[2] else 90

    db.add_b2b_point(contract_name, point_name, interval_days)
    await update.message.reply_text(
        f"Точка «{point_name}» ({contract_name}) добавлена, ТО каждые {interval_days} дн."
    )


async def cmd_b2b_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        await update.message.reply_text("Формат: /b2b_done Название договора | Название точки")
        return

    ok = db.mark_b2b_point_serviced(parts[0], parts[1], date.today())
    if ok:
        await update.message.reply_text(f"Отмечено: «{parts[1]}» ({parts[0]}) обслужена сегодня.")
    else:
        await update.message.reply_text("Не нашёл такую точку. Проверьте /b2b_list.")


async def cmd_b2b_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    points = db.list_all_b2b_points()
    if not points:
        await update.message.reply_text("B2B-точек пока нет. Добавьте: /b2b_add Договор | Точка | Периодичность")
        return

    today = date.today()
    lines = []
    for p in points:
        last = date.fromisoformat(p["last_service_date"])
        days_since = (today - last).days
        status = "⚠️ ПРОСРОЧЕНО" if days_since >= p["interval_days"] else f"ок, {days_since}/{p['interval_days']} дн."
        lines.append(f"• {p['contract_name']} — {p['point_name']}: {status}")
    await update.message.reply_text("B2B-точки обслуживания:\n" + "\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    lines = [
        "/calc — подбор мощности кондиционера по площади",
        "/warranty — проверить гарантию по вашей установке",
        "/city <город> — сменить город для погоды",
        "/whoami — узнать свой chat_id",
    ]
    if is_admin(chat_id):
        lines += [
            "",
            "Админ-команды:",
            "/new_install Клиент | Модель | [Класс] | [Город] | [Дата] | [Гарантия,мес] | [chat_id]",
            "/stock — остатки склада",
            "/stock_init Название | Ед.изм | Количество",
            "/stock_use Название | Количество",
            "/act Адрес — начать акт (затем фото или «пропустить»)",
            "/acts_pending — список актов в очереди",
            "/b2b_add Договор | Точка | [Периодичность,дн]",
            "/b2b_done Договор | Точка",
            "/b2b_list — статус всех B2B-точек",
        ]
    await update.message.reply_text("\n".join(lines))


# ------------------------------ Основные обработчики ------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот AirClimat.pmr 🌬️\n\n"
        "Напишите модель вашего кондиционера — прикину, насколько сейчас "
        "падает его эффективность обогрева на текущей уличной температуре.\n\n"
        "Также доступно: /calc (подбор мощности), /warranty (проверка гарантии), "
        "/help (все команды).\n\n"
        "Сначала спрошу ваш город, чтобы взять актуальную погоду — это "
        "разовый вопрос, дальше буду его помнить."
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
                f"Не нашёл город «{user_text}». Попробуйте написать по-другому."
            )
            return

        lat, lon, city_name = geocoded
        CHAT_CITIES[chat_id] = (lat, lon, city_name)
        PENDING_CITY.pop(chat_id)
        await start_classification(update, chat_id, pending["original_model"])
        return

    # 2. Ждём текст для /calc (площадь или контакт)?
    if chat_id in PENDING_CALC:
        await handle_calc_text(update, chat_id, user_text)
        return

    # 3. Ждём фото/«пропустить» для акта? («пропустить» приходит текстом)
    if chat_id in PENDING_ACT:
        if user_text.lower() in ("пропустить", "skip", "нет"):
            pending = PENDING_ACT.pop(chat_id)
            act_id = db.add_act(chat_id, pending["address"], None)
            await update.message.reply_text(f"Акт №{act_id} по адресу «{pending['address']}» сохранён без фото.")
        else:
            await update.message.reply_text("Пришлите фото объекта, либо напишите «пропустить».")
        return

    # 4. Ждём текст отзыва после автоматического запроса?
    if chat_id in PENDING_REVIEW:
        pending = PENDING_REVIEW.pop(chat_id)
        db.add_review(chat_id, pending["installation_id"], user_text)
        await update.message.reply_text("Спасибо большое за отзыв! 🙏")
        return

    # 5. Город ещё неизвестен вообще — это первое сообщение.
    if chat_id not in CHAT_CITIES:
        PENDING_CITY[chat_id] = {"original_model": user_text}
        await update.message.reply_text(
            "Из какого вы города? Это нужно, чтобы узнать текущую температуру на улице."
        )
        return

    # 6. Иначе — это модель кондиционера для расчёта КПД.
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
            chat_id, reply_target, pending["original_model"], pending["category"],
            capacity_class or "12K", capacity_was_guessed=False,
        )
        return

    if data.startswith("calcfloor:"):
        if chat_id not in PENDING_CALC or PENDING_CALC[chat_id].get("step") != "floor":
            await reply_target("Этот вопрос уже устарел, начните заново: /calc")
            return
        PENDING_CALC[chat_id]["top_floor"] = data.split(":", 1)[1] == "yes"
        PENDING_CALC[chat_id]["step"] = "sunny"
        await reply_target("Окна выходят на солнечную сторону (юг/запад)?", reply_markup=yes_no_keyboard("calcsun"))
        return

    if data.startswith("calcsun:"):
        if chat_id not in PENDING_CALC or PENDING_CALC[chat_id].get("step") != "sunny":
            await reply_target("Этот вопрос уже устарел, начните заново: /calc")
            return
        PENDING_CALC[chat_id]["sunny"] = data.split(":", 1)[1] == "yes"
        PENDING_CALC[chat_id]["step"] = "clienttype"
        await reply_target("Вы физлицо или компания?", reply_markup=client_type_keyboard())
        return

    if data.startswith("calctype:"):
        if chat_id not in PENDING_CALC or PENDING_CALC[chat_id].get("step") != "clienttype":
            await reply_target("Этот вопрос уже устарел, начните заново: /calc")
            return
        PENDING_CALC[chat_id]["client_type"] = data.split(":", 1)[1]
        PENDING_CALC[chat_id]["step"] = "contact"
        await reply_target(
            "Оставьте, пожалуйста, телефон или @username, чтобы менеджер связался с точной ценой "
            "(или нажмите «Пропустить»).",
            reply_markup=skip_keyboard("calccontact:skip"),
        )
        return

    if data == "calccontact:skip":
        if chat_id not in PENDING_CALC or PENDING_CALC[chat_id].get("step") != "contact":
            await reply_target("Этот вопрос уже устарел, начните заново: /calc")
            return
        await finish_calc_lead(chat_id, reply_target, contact=None)
        return


# ------------------------------ Фоновая ежедневная задача ------------------------------

async def daily_checks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: напоминания о ТО, запросы отзывов клиентам, сводка по
    просроченным B2B-точкам админам."""
    today = date.today()
    bot = context.bot

    for inst in db.installations_needing_review(today, REVIEW_REQUEST_DAYS_AFTER_INSTALL):
        try:
            await bot.send_message(
                inst["chat_id"],
                "Здравствуйте! Прошло несколько дней с установки кондиционера "
                f"({inst['model_text']}). Как всё работает, довольны ли результатом? "
                "Буду благодарен за пару слов отзыва 🙏",
            )
            PENDING_REVIEW[inst["chat_id"]] = {"installation_id": inst["id"]}
            db.mark_review_requested(inst["id"])
        except Exception:
            logger.exception("Не удалось запросить отзыв у chat_id=%s", inst["chat_id"])

    for inst in db.installations_needing_maintenance(today, MAINTENANCE_REMINDER_INTERVAL_DAYS):
        try:
            await bot.send_message(
                inst["chat_id"],
                f"Здравствуйте! Ваш кондиционер ({inst['model_text']}) давно не проходил "
                "техобслуживание. Рекомендуем плановую чистку — напишите нам, чтобы "
                "договориться о времени.",
            )
            db.mark_maintenance_reminded(inst["id"], today)
        except Exception:
            logger.exception("Не удалось отправить напоминание о ТО chat_id=%s", inst["chat_id"])

    due_points = db.list_b2b_points_due(today)
    if due_points and ADMIN_CHAT_IDS:
        lines = [f"• {p['contract_name']} — {p['point_name']}" for p in due_points]
        text = "⚠️ Пора выехать на плановое ТО по B2B-контрактам:\n" + "\n".join(lines)
        await notify_admins(bot, text)


APPLICATION = None  # заполняется в main(), нужен для notify_admins при вызове вне job_queue


def main() -> None:
    global APPLICATION

    db.init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    APPLICATION = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("city", set_city))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("calc", cmd_calc))
    application.add_handler(CommandHandler("warranty", cmd_warranty))
    application.add_handler(CommandHandler("new_install", cmd_new_install))
    application.add_handler(CommandHandler("stock", cmd_stock))
    application.add_handler(CommandHandler("stock_init", cmd_stock_init))
    application.add_handler(CommandHandler("stock_use", cmd_stock_use))
    application.add_handler(CommandHandler("act", cmd_act))
    application.add_handler(CommandHandler("acts_pending", cmd_acts_pending))
    application.add_handler(CommandHandler("b2b_add", cmd_b2b_add))
    application.add_handler(CommandHandler("b2b_done", cmd_b2b_done))
    application.add_handler(CommandHandler("b2b_list", cmd_b2b_list))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if application.job_queue is not None:
        application.job_queue.run_daily(daily_checks, time=datetime.min.time().replace(hour=B2B_CHECK_HOUR_UTC))
    else:
        logger.warning(
            "JobQueue недоступен (не установлен extras job-queue) — "
            "автоматические напоминания о ТО/отзывах/B2B работать не будут."
        )

    logger.info("Бот запущен (без ИИ, без Anthropic API), лог диалогов пишется в %s", LOG_PATH)
    application.run_polling()


if __name__ == "__main__":
    main()
