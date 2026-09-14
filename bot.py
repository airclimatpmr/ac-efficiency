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
5. Склад материалов — кнопками для монтажников (/material) и текстовыми
   командами для админа (/stock, /stock_init, /stock_use).
6. Учёт актов выполненных работ (/act — адрес + фото).
7. B2B-трекер обслуживания точек по контракту (/b2b_add, /b2b_done, /b2b_list)
   с ежедневным напоминанием админу о просроченных точках.

Все данные — в SQLite (db.py), переживают перезапуск бота (см. README про
Railway Volume для персистентности на Railway).

Настройка:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="..."
    export ADMIN_CHAT_IDS="123456789,987654321"   # владельцы, полный доступ
    export INSTALLER_CHAT_IDS="555666777"         # монтажники: только склад + акты
    python bot.py

Свой chat_id узнать через команду /whoami в самом боте.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone, date

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
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
import act_catalog
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
INSTALLER_CHAT_IDS: set[int] = {
    int(x) for x in os.environ.get("INSTALLER_CHAT_IDS", "").split(",") if x.strip().isdigit()
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

# Кнопочный выбор материала для списания: chat_id -> {"step": "await_qty"/"await_other_name", "name":, "unit":}
PENDING_MATERIAL: dict[int, dict] = {}

# Кнопочный мастер структурированного акта (/actcalc):
# chat_id -> {"capacity_group":, "page": int, "cart": {code: {...}}, "awaiting": {"code":, "field": "qty"/"price"} | None}
PENDING_ACTCALC: dict[int, dict] = {}

# Кнопочный флоу оприходования материала (кнопка "➕ Приход материала"):
# chat_id -> {"step": "name"/"qty", "name": str | None}
PENDING_STOCKIN: dict[int, dict] = {}

# Ждём адрес для «Акт с фото» после нажатия кнопки (дальше — как /act Адрес)
PENDING_ACT_ADDRESS_PROMPT: set[int] = set()

# Стандартные материалы, которые монтажник выбирает кнопками. Заводятся на
# складе автоматически при старте бота (с нулевым остатком, если ещё не
# заведены вручную через /stock_init) — чтобы не нужно было отдельно
# инициализировать каждый перед первым использованием.
STANDARD_MATERIALS = [
    ("pipe6", "Труба Ø6", "м"),
    ("pipe9", "Труба Ø9", "м"),
    ("pipe12", "Труба Ø12", "м"),
    ("pipe15", "Труба Ø15", "м"),
    ("brackets", "Кронштейны", "шт"),
    ("bolts", "Болты", "шт"),
    ("metaloplast", "Металлопласт", "м"),
]
MATERIAL_BY_CODE = {code: (name, unit) for code, name, unit in STANDARD_MATERIALS}

_log_lock = asyncio.Lock()


def is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_CHAT_IDS


def is_installer(chat_id: int) -> bool:
    """Монтажники + админы (админ автоматически имеет и права монтажника)."""
    return chat_id in INSTALLER_CHAT_IDS or is_admin(chat_id)


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


# --------------------------- Главное меню (постоянная клавиатура) ---------------------------

BTN_ACTCALC = "🧾 Посчитать акт"
BTN_MATERIAL = "📦 Списать материал"
BTN_ACT_PHOTO = "📷 Акт с фото (адрес)"
BTN_STOCK_IN = "➕ Приход материала"
BTN_STOCK = "📊 Склад"
BTN_ACTS_PENDING = "📋 Акты в очереди"
BTN_B2B_LIST = "🏢 B2B точки"
BTN_CALC = "🧮 Подбор мощности"
BTN_WARRANTY = "🛡 Проверить гарантию"
BTN_HELP = "❓ Все команды"


def main_menu_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    rows = []

    if is_installer(chat_id):
        rows.append([BTN_ACTCALC, BTN_MATERIAL])
        rows.append([BTN_ACT_PHOTO])

    if is_admin(chat_id):
        rows.append([BTN_STOCK_IN, BTN_STOCK])
        rows.append([BTN_ACTS_PENDING, BTN_B2B_LIST])

    if not is_installer(chat_id) and not is_admin(chat_id):
        rows.append([BTN_CALC, BTN_WARRANTY])

    rows.append([BTN_HELP])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def material_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for code, name, _unit in STANDARD_MATERIALS:
        row.append(InlineKeyboardButton(name, callback_data=f"mat:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Другое", callback_data="mat:other")])
    return InlineKeyboardMarkup(rows)


def capacity_group_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(g, callback_data=f"actgroup:{g}") for g in act_catalog.CAPACITY_GROUPS
    ]])


def actcalc_page_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    state = PENDING_ACTCALC[chat_id]
    page_idx = state["page"]
    title, items, has_price = act_catalog.PAGES[page_idx]
    cart = state["cart"]

    rows = []
    for entry in items:
        code = entry[0]
        name = entry[1]
        label = name
        if code in cart:
            label = f"✅ {name} ({cart[code]['qty']:g})"
        rows.append([InlineKeyboardButton(label, callback_data=f"actitem:{code}")])

    nav = []
    if page_idx > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data="actpage:prev"))
    if page_idx < len(act_catalog.PAGES) - 1:
        nav.append(InlineKeyboardButton("Далее ▶", callback_data="actpage:next"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✅ Завершить акт", callback_data="actpage:finish")])

    return InlineKeyboardMarkup(rows)


def actcalc_page_title(chat_id: int) -> str:
    state = PENDING_ACTCALC[chat_id]
    title, items, has_price = act_catalog.PAGES[state["page"]]
    group = state["capacity_group"]
    hint = "нажмите на позицию и введите количество" if has_price else (
        "нажмите на позицию — спрошу количество и цену за единицу"
    )
    return f"Группа мощности: {group}\n\n{title} — {hint}:"


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
        f"о заявках и напоминания и иметь доступ ко всем командам.\n\n"
        f"Если это монтажник, которому нужен только доступ к складу и актам — "
        f"добавьте его chat_id в переменную INSTALLER_CHAT_IDS вместо ADMIN_CHAT_IDS."
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


async def cmd_material(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопочное списание материала — для монтажников (проще, чем /stock_use)."""
    chat_id = update.effective_chat.id
    if not is_installer(chat_id):
        return

    await update.message.reply_text(
        "Какой материал списываете?", reply_markup=material_keyboard()
    )


async def handle_material_text(update: Update, chat_id: int, text: str) -> None:
    state = PENDING_MATERIAL[chat_id]

    if state["step"] == "await_other_name":
        name = text.strip()
        if not name:
            await update.message.reply_text("Напишите название материала текстом.")
            return
        state["name"] = name
        state["unit"] = "ед."
        state["step"] = "await_qty"
        await update.message.reply_text(f"Сколько списать «{name}»? Введите число ({state['unit']}).")
        return

    if state["step"] == "await_qty":
        try:
            qty = float(text.replace(",", ".").strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите положительное число, например: 4")
            return

        name = state["name"]
        unit = state["unit"]
        PENDING_MATERIAL.pop(chat_id)

        new_qty = db.adjust_inventory(name, -qty)
        auto_created = False
        if new_qty is None:
            # Материал ещё не заведён на складе (например, свой вариант через
            # "Другое") — заводим на лету с нулевого остатка, чтобы не
            # блокировать монтажника, но предупреждаем админа.
            db.upsert_inventory_item(name, unit, 0)
            new_qty = db.adjust_inventory(name, -qty)
            auto_created = True

        warn = ""
        if new_qty < 0:
            warn = " ⚠️ остаток отрицательный."
        await update.message.reply_text(f"Списано {qty:g} {unit} «{name}». Остаток: {new_qty:g} {unit}.{warn}")

        if auto_created:
            await notify_admins(
                None,
                f"ℹ️ Монтажник списал материал «{name}» ({unit}), которого не было на складе — "
                f"завёл автоматически с нулевого остатка, сейчас {new_qty:g}. Проверьте и при "
                f"необходимости поправьте остаток через /stock_init."
            )
        return


async def cmd_stock_in(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Оприходование материала (поступление на склад) — для админа."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        await update.message.reply_text("Формат: /stock_in Название | Количество")
        return

    name, qty_raw = parts[0], parts[1]
    try:
        qty = float(qty_raw.replace(",", "."))
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Количество должно быть положительным числом.")
        return

    new_qty, created = db.adjust_inventory_auto_create(name, qty, unit_if_missing="ед.")
    note = " (материал заведён автоматически, уточните ед. изм. через /stock_init при необходимости)" if created else ""
    await update.message.reply_text(f"Оприходовано {qty:g} «{name}». Остаток: {new_qty:g}.{note}")


# --------------------- Часть 6b: монтажник — расчёт акта по кнопкам ---------------------

async def cmd_actcalc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопочный мастер акта: мощность -> позиции работ/материалов (сначала с
    фиксированной ценой, потом без) -> количество -> итог + списание склада."""
    chat_id = update.effective_chat.id
    if not is_installer(chat_id):
        return

    await update.message.reply_text(
        "Считаю акт. Сначала — группа мощности кондиционера:",
        reply_markup=capacity_group_keyboard(),
    )


async def show_actcalc_page(chat_id: int, reply_target) -> None:
    await reply_target(actcalc_page_title(chat_id), reply_markup=actcalc_page_keyboard(chat_id))


async def handle_actcalc_text(update: Update, chat_id: int, text: str) -> None:
    state = PENDING_ACTCALC[chat_id]
    awaiting = state["awaiting"]
    if awaiting is None:
        return

    code = awaiting["code"]
    item_meta = act_catalog.ALL_ITEMS_BY_CODE[code]

    if awaiting["field"] == "qty":
        try:
            qty = float(text.replace(",", ".").strip())
            if qty < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(f"Введите число (можно 0), например: 3 или 2.5")
            return

        fixed_price = act_catalog.price_for(code, state["capacity_group"])
        if fixed_price is not None:
            # Цена уже известна — сразу сохраняем позицию в корзину.
            if qty == 0:
                state["cart"].pop(code, None)
            else:
                state["cart"][code] = {
                    "name": item_meta["name"], "unit": item_meta["unit"],
                    "qty": qty, "unit_price": fixed_price,
                }
            state["awaiting"] = None
            await show_actcalc_page(chat_id, update.message.reply_text)
            return

        # Цены нет в каталоге — сначала запомним qty, затем спросим цену.
        if qty == 0:
            state["cart"].pop(code, None)
            state["awaiting"] = None
            await show_actcalc_page(chat_id, update.message.reply_text)
            return

        state["awaiting"] = {"code": code, "field": "price", "qty": qty}
        await update.message.reply_text(f"Цена за единицу ({item_meta['unit']}) для «{item_meta['name']}»:")
        return

    if awaiting["field"] == "price":
        try:
            price = float(text.replace(",", ".").strip())
            if price < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите цену числом, например: 45")
            return

        qty = awaiting["qty"]
        state["cart"][code] = {
            "name": item_meta["name"], "unit": item_meta["unit"],
            "qty": qty, "unit_price": price,
        }
        state["awaiting"] = None
        await show_actcalc_page(chat_id, update.message.reply_text)
        return


async def finish_actcalc(chat_id: int, reply_target) -> None:
    state = PENDING_ACTCALC.pop(chat_id)
    cart = state["cart"]
    group = state["capacity_group"]

    if not cart:
        await reply_target("Акт пуст — ни одной позиции не указано, ничего не сохраняю.")
        return

    items = []
    total = 0.0
    for code, entry in cart.items():
        subtotal = entry["qty"] * entry["unit_price"]
        total += subtotal
        items.append({
            "code": code, "name": entry["name"], "unit": entry["unit"],
            "qty": entry["qty"], "unit_price": entry["unit_price"], "subtotal": subtotal,
        })

    act_id = db.add_material_act(chat_id, group, items, total)

    # Списываем со склада только материалы (не работы) — сумма по одинаковым
    # позициям в рамках одного акта уже не задвоится, т.к. cart хранит одну
    # запись на код.
    stock_lines = []
    for it in items:
        meta = act_catalog.ALL_ITEMS_BY_CODE[it["code"]]
        if not meta["is_material"]:
            continue
        new_qty, created = db.adjust_inventory_auto_create(it["name"], -it["qty"], unit_if_missing=it["unit"])
        flag = " (заведён автоматически)" if created else ""
        stock_lines.append(f"«{it['name']}»: списано {it['qty']:g}, остаток {new_qty:g}{flag}")

    lines = [f"• {it['name']}: {it['qty']:g} {it['unit']} × {it['unit_price']:g} = {it['subtotal']:g}" for it in items]
    text = (
        f"✅ Акт №{act_id} сохранён (группа {group}).\n\n"
        + "\n".join(lines)
        + f"\n\n**Итого: {total:g}**"
    )
    if stock_lines:
        text += "\n\nСклад обновлён:\n" + "\n".join(stock_lines)

    await reply_target(text)


# --------------------- Часть 6: admin — акты выполненных работ ---------------------

async def cmd_act(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_installer(chat_id):
        return

    address = " ".join(context.args).strip()
    if not address:
        await update.message.reply_text("Формат: /act Адрес объекта")
        return

    await start_act_with_address(chat_id, address, update.message.reply_text)


async def start_act_with_address(chat_id: int, address: str, reply_target) -> None:
    PENDING_ACT[chat_id] = {"address": address}
    await reply_target(
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
    if is_installer(chat_id):
        lines += [
            "",
            "Команды монтажника:",
            "/material — списать материал со склада (кнопками, быстро)",
            "/actcalc — посчитать акт по позициям (мощность → работы/материалы → итог, списывает склад)",
            "/act Адрес — начать акт (затем фото или «пропустить»)",
        ]
    if is_admin(chat_id):
        lines += [
            "",
            "Админ-команды:",
            "/new_install Клиент | Модель | [Класс] | [Город] | [Дата] | [Гарантия,мес] | [chat_id]",
            "/stock — остатки склада",
            "/stock_init Название | Ед.изм | Количество",
            "/stock_use Название | Количество",
            "/stock_in Название | Количество — оприходовать материал",
            "/acts_pending — список актов (фото) в очереди",
            "/b2b_add Договор | Точка | [Периодичность,дн]",
            "/b2b_done Договор | Точка",
            "/b2b_list — статус всех B2B-точек",
        ]
    await update.message.reply_text("\n".join(lines))


# ------------------------------ Основные обработчики ------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "Привет! Я бот AirClimat.pmr 🌬️\n\n"
        "Напишите модель вашего кондиционера — прикину, насколько сейчас "
        "падает его эффективность обогрева на текущей уличной температуре.\n\n"
        "Остальные действия — кнопками снизу (или /help — список всех команд).\n\n"
        "Сначала спрошу ваш город, чтобы взять актуальную погоду — это "
        "разовый вопрос, дальше буду его помнить.",
        reply_markup=main_menu_keyboard(chat_id),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text("Меню:", reply_markup=main_menu_keyboard(chat_id))


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

    # 0. Нажатие кнопки главного меню — обрабатываем раньше любых других
    #    состояний, это явная навигационная команда.
    if user_text == BTN_ACTCALC:
        await cmd_actcalc(update, context)
        return
    if user_text == BTN_MATERIAL:
        await cmd_material(update, context)
        return
    if user_text == BTN_STOCK:
        await cmd_stock(update, context)
        return
    if user_text == BTN_ACTS_PENDING:
        await cmd_acts_pending(update, context)
        return
    if user_text == BTN_B2B_LIST:
        await cmd_b2b_list(update, context)
        return
    if user_text == BTN_CALC:
        await cmd_calc(update, context)
        return
    if user_text == BTN_WARRANTY:
        await cmd_warranty(update, context)
        return
    if user_text == BTN_HELP:
        await cmd_help(update, context)
        return
    if user_text == BTN_STOCK_IN:
        if not is_admin(chat_id):
            return
        PENDING_STOCKIN[chat_id] = {"step": "name", "name": None}
        await update.message.reply_text("Название материала для прихода:")
        return
    if user_text == BTN_ACT_PHOTO:
        if not is_installer(chat_id):
            return
        PENDING_ACT_ADDRESS_PROMPT.add(chat_id)
        await update.message.reply_text("Введите адрес объекта:")
        return

    # 0b. Ждём адрес после нажатия кнопки "Акт с фото"?
    if chat_id in PENDING_ACT_ADDRESS_PROMPT:
        PENDING_ACT_ADDRESS_PROMPT.discard(chat_id)
        await start_act_with_address(chat_id, user_text, update.message.reply_text)
        return

    # 0c. Ждём название/количество для прихода материала (кнопка "Приход")?
    if chat_id in PENDING_STOCKIN:
        state = PENDING_STOCKIN[chat_id]
        if state["step"] == "name":
            state["name"] = user_text
            state["step"] = "qty"
            await update.message.reply_text(f"Количество «{user_text}» для прихода:")
            return
        if state["step"] == "qty":
            try:
                qty = float(user_text.replace(",", ".").strip())
                if qty <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Введите положительное число, например: 20")
                return
            name = state["name"]
            PENDING_STOCKIN.pop(chat_id)
            new_qty, created = db.adjust_inventory_auto_create(name, qty, unit_if_missing="ед.")
            note = " (материал заведён автоматически, единицу измерения можно поправить через /stock_init)" if created else ""
            await update.message.reply_text(f"Оприходовано {qty:g} «{name}». Остаток: {new_qty:g}.{note}")
            return

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

    # 2b. Ждём текст для кнопочного списания материала (количество / своё название)?
    if chat_id in PENDING_MATERIAL:
        await handle_material_text(update, chat_id, user_text)
        return

    # 2c. Ждём текст (количество / цену) внутри мастера /actcalc?
    if chat_id in PENDING_ACTCALC and PENDING_ACTCALC[chat_id]["awaiting"] is not None:
        await handle_actcalc_text(update, chat_id, user_text)
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

    if data.startswith("mat:"):
        if not is_installer(chat_id):
            return
        code = data.split(":", 1)[1]
        if code == "other":
            PENDING_MATERIAL[chat_id] = {"step": "await_other_name"}
            await reply_target("Напишите название материала:")
            return
        name, unit = MATERIAL_BY_CODE[code]
        PENDING_MATERIAL[chat_id] = {"step": "await_qty", "name": name, "unit": unit}
        await reply_target(f"Сколько списать «{name}»? Введите число ({unit}).")
        return

    if data.startswith("actgroup:"):
        if not is_installer(chat_id):
            return
        group = data.split(":", 1)[1]
        PENDING_ACTCALC[chat_id] = {"capacity_group": group, "page": 0, "cart": {}, "awaiting": None}
        await show_actcalc_page(chat_id, reply_target)
        return

    if data.startswith("actitem:"):
        if chat_id not in PENDING_ACTCALC:
            await reply_target("Сессия акта устарела, начните заново: /actcalc")
            return
        code = data.split(":", 1)[1]
        item_meta = act_catalog.ALL_ITEMS_BY_CODE[code]
        PENDING_ACTCALC[chat_id]["awaiting"] = {"code": code, "field": "qty"}
        await reply_target(f"Количество ({item_meta['unit']}) для «{item_meta['name']}»:")
        return

    if data.startswith("actpage:"):
        if chat_id not in PENDING_ACTCALC:
            await reply_target("Сессия акта устарела, начните заново: /actcalc")
            return
        action = data.split(":", 1)[1]
        if action == "prev":
            PENDING_ACTCALC[chat_id]["page"] -= 1
            await show_actcalc_page(chat_id, reply_target)
        elif action == "next":
            PENDING_ACTCALC[chat_id]["page"] += 1
            await show_actcalc_page(chat_id, reply_target)
        elif action == "finish":
            await finish_actcalc(chat_id, reply_target)
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


def seed_standard_materials() -> None:
    """Заводит стандартные материалы на складе с нулевым остатком, если их
    там ещё нет — чтобы кнопки /material сразу работали без ручной
    инициализации каждого через /stock_init. Если материал уже заведён
    (в том числе с ненулевым остатком) — ничего не меняет."""
    for _code, name, unit in STANDARD_MATERIALS:
        db.upsert_inventory_item(name, unit, 0)


def main() -> None:
    global APPLICATION

    db.init_db()
    seed_standard_materials()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    APPLICATION = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("city", set_city))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("calc", cmd_calc))
    application.add_handler(CommandHandler("warranty", cmd_warranty))
    application.add_handler(CommandHandler("new_install", cmd_new_install))
    application.add_handler(CommandHandler("stock", cmd_stock))
    application.add_handler(CommandHandler("stock_init", cmd_stock_init))
    application.add_handler(CommandHandler("stock_use", cmd_stock_use))
    application.add_handler(CommandHandler("material", cmd_material))
    application.add_handler(CommandHandler("stock_in", cmd_stock_in))
    application.add_handler(CommandHandler("actcalc", cmd_actcalc))
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
