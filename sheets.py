# -*- coding: utf-8 -*-
"""
Запись данных в Google Sheets:
1. Новые клиенты — в "Список клиентов" (лист «Клиенты»), после
   распознавания фото акта (см. ocr.py, bot.py:handle_photo).
2. Остатки склада материалов — в "Склад материалов" (первый/единственный
   лист), после каждого изменения склада (списание, приход, /actcalc).

Настройка:
    1. В Google Cloud Console создайте сервисный аккаунт (Service Account),
       включите Google Sheets API, скачайте JSON-ключ.
    2. Откройте ОБЕ таблицы в Google Sheets → кнопка "Настройки доступа" →
       добавьте email сервисного аккаунта (поле client_email внутри
       скачанного JSON) с правом «Редактор» на каждую из них.
    3. Переменные окружения:
       export GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account", ...}'  # весь JSON одной строкой
       export CLIENTS_SPREADSHEET_ID="1AI-e-W5KlHt9FTxyc0049D_5TsUmXZP0hmaqTYpGJPw"  # уже подставлен ID по умолчанию
       export STOCK_SPREADSHEET_ID="1YnXhKkNAnsliRpIrdoz4SWPEGOgzx3umgbNd51TnLJ0"    # уже подставлен ID по умолчанию
       export INSTALLER_NAMES="111111111:Дмитрий,222222222:Евгений"  # необязательно, chat_id -> имя для колонки "Исполнитель"

Без GOOGLE_SERVICE_ACCOUNT_JSON обе записи просто пропускаются
(is_configured() == False) — бот продолжает работать как обычно, локальная
база (SQLite) всё равно остаётся источником истины независимо от этого.
"""

import os
import json
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
# ID таблицы "Список клиентов", найденной в Google Drive — можно переопределить
# переменной окружения, если понадобится указать другую таблицу.
CLIENTS_SPREADSHEET_ID = os.environ.get(
    "CLIENTS_SPREADSHEET_ID", "1AI-e-W5KlHt9FTxyc0049D_5TsUmXZP0hmaqTYpGJPw"
)
CLIENTS_WORKSHEET_NAME = os.environ.get("CLIENTS_WORKSHEET_NAME", "Клиенты")

# ID таблицы "Склад материалов", созданной для зеркалирования остатков склада.
STOCK_SPREADSHEET_ID = os.environ.get(
    "STOCK_SPREADSHEET_ID", "1YnXhKkNAnsliRpIrdoz4SWPEGOgzx3umgbNd51TnLJ0"
)

# chat_id монтажника -> отображаемое имя для колонки "Исполнитель".
# Без этой переменной в колонку просто попадёт числовой chat_id.
INSTALLER_NAMES: dict[int, str] = {}
for _pair in os.environ.get("INSTALLER_NAMES", "").split(","):
    if ":" in _pair:
        _chat_id_str, _name = _pair.split(":", 1)
        _chat_id_str = _chat_id_str.strip()
        if _chat_id_str.isdigit():
            INSTALLER_NAMES[int(_chat_id_str)] = _name.strip()

_gc = None  # ленивая инициализация gspread-клиента


def is_configured() -> bool:
    return bool(GOOGLE_SERVICE_ACCOUNT_JSON)


def _get_client():
    global _gc
    if _gc is None:
        import gspread
        from google.oauth2.service_account import Credentials

        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не задан")
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        _gc = gspread.authorize(creds)
    return _gc


def installer_display_name(chat_id: int | None) -> str:
    if chat_id is None:
        return ""
    return INSTALLER_NAMES.get(chat_id, str(chat_id))


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, 28)  # избегаем проблем с несуществующими датами
    return date(year, month, day)


def append_client_row(
    client_name: str,
    phone: str | None,
    address: str | None,
    equipment: str | None,
    install_date: date,
    installer_chat_id: int | None,
    notes: str | None = None,
) -> None:
    """Добавляет строку в лист «Клиенты» таблицы «Список клиентов».

    Бросает исключение при любой ошибке (нет доступа, таблица недоступна,
    сетевая ошибка и т.п.) — вызывающий код (bot.py) должен её поймать и не
    останавливать остальную обработку акта: запись в Sheets — это плюс к
    локальной базе, а не замена ей.
    """
    gc = _get_client()
    sh = gc.open_by_key(CLIENTS_SPREADSHEET_ID)
    ws = sh.worksheet(CLIENTS_WORKSHEET_NAME)

    today_str = date.today().strftime("%Y-%m-%d")
    install_date_str = install_date.strftime("%m.%Y")
    next_to_str = _add_months(install_date, 6).strftime("%d.%m.%Y")

    # Порядок строго соответствует колонкам листа «Клиенты»:
    # Дата добавления | ФИО | Телефон | Email | Адрес | Оборудование |
    # Дата установки | Последнее ТО | Тип услуги | Сумма | Исполнитель |
    # Следующее ТО | Статус | Дата последнего контакта | Примечания
    row = [
        today_str,
        client_name or "",
        phone or "",
        "",
        address or "",
        equipment or "",
        install_date_str,
        "",
        "Установка",
        "",
        installer_display_name(installer_chat_id),
        next_to_str,
        "Активный",
        today_str,
        notes or "Добавлено автоматически по фото акта",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


def sync_inventory(rows: list[dict]) -> None:
    """Полностью перезаписывает лист «Склад материалов» текущими остатками.

    rows — список словарей вида {"name":, "unit":, "quantity":}, обычно
    результат db.list_inventory(). Полная перезапись (а не точечное
    обновление отдельных ячеек) выбрана осознанно: склад — это максимум
    несколько десятков позиций, перезаписать всё целиком проще и надёжнее,
    чем вычислять, какие строки/ячейки изменились.

    Бросает исключение при любой ошибке — вызывающий код (bot.py) должен её
    поймать и не останавливать остальную работу: склад в SQLite остаётся
    источником истины, Google Sheets — это просто зеркало для просмотра.
    """
    gc = _get_client()
    sh = gc.open_by_key(STOCK_SPREADSHEET_ID)
    ws = sh.sheet1  # таблица односкладочная — берём первый (единственный) лист

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    header = ["Материал", "Ед. изм.", "Остаток", "Обновлено"]
    data = [header] + [
        [r["name"], r["unit"], r["quantity"], now_str] for r in sorted(rows, key=lambda r: r["name"])
    ]

    ws.clear()
    ws.update(range_name="A1", values=data, value_input_option="USER_ENTERED")
