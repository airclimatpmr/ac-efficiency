# -*- coding: utf-8 -*-
"""
Простое хранилище на SQLite для всех новых функций бота: лиды с калькулятора
мощности, установленные кондиционеры (для гарантии/напоминаний о ТО/отзывов),
склад материалов, акты выполненных работ, B2B-точки обслуживания.

Почему SQLite, а не память процесса: все предыдущие функции бота (город,
уточнения) жили в dict в памяти и терялись при перезапуске — это нормально
для разового диалога, но недопустимо для склада, лидов и гарантий, которые
должны копиться месяцами.

ВАЖНО про Railway: файловая система там эфемерная — при каждом новом деплое
файл базы (DB_PATH) обнуляется. Чтобы данные реально сохранялись между
деплоями, нужно подключить Railway Volume и указать путь к нему в переменной
DB_PATH (см. README.md, раздел "Персистентное хранилище").
"""

import os
import json
import sqlite3
from datetime import datetime, date, timedelta
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                area REAL,
                top_floor INTEGER,
                sunny INTEGER,
                client_type TEXT,
                recommended_capacity TEXT,
                contact TEXT,
                status TEXT DEFAULT 'new'
            );

            CREATE TABLE IF NOT EXISTS installations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                client_label TEXT,
                model_text TEXT,
                capacity_class TEXT,
                city TEXT,
                install_date TEXT NOT NULL,
                warranty_months INTEGER DEFAULT 24,
                review_requested INTEGER DEFAULT 0,
                last_maintenance_reminder TEXT
            );

            CREATE TABLE IF NOT EXISTS inventory (
                name TEXT PRIMARY KEY,
                unit TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS acts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                installer_chat_id INTEGER,
                address TEXT,
                photo_file_id TEXT,
                status TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS b2b_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_name TEXT NOT NULL,
                point_name TEXT NOT NULL,
                interval_days INTEGER DEFAULT 90,
                last_service_date TEXT,
                UNIQUE(contract_name, point_name)
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                chat_id INTEGER,
                installation_id INTEGER,
                text TEXT
            );

            CREATE TABLE IF NOT EXISTS material_acts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                installer_chat_id INTEGER,
                capacity_group TEXT NOT NULL,
                items_json TEXT NOT NULL,
                total REAL NOT NULL
            );
            """
        )
    _ensure_column("leads", "high_ceiling", "INTEGER")
    _ensure_column("leads", "heat_equipment", "INTEGER")
    _ensure_column("leads", "window_direction", "TEXT")


def _ensure_column(table: str, column: str, coltype: str) -> None:
    """Добавляет колонку в существующую таблицу, если её ещё нет (SQLite не
    поддерживает "ADD COLUMN IF NOT EXISTS" напрямую) — нужно для того,
    чтобы у пользователей, уже запускавших более раннюю версию бота, база
    сама доросла до новой схемы без ручных миграций."""
    with get_conn() as conn:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


# ------------------------------- Лиды -------------------------------

def add_lead(chat_id: int, area: float, top_floor: bool, sunny: bool,
             client_type: str, recommended_capacity: str, contact: str | None,
             high_ceiling: bool | None = None, heat_equipment: bool | None = None,
             window_direction: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO leads
               (created_at, chat_id, area, top_floor, sunny, client_type,
                recommended_capacity, contact, high_ceiling, heat_equipment, window_direction)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), chat_id, area, int(top_floor), int(sunny),
             client_type, recommended_capacity, contact,
             None if high_ceiling is None else int(high_ceiling),
             None if heat_equipment is None else int(heat_equipment),
             window_direction),
        )
        return cur.lastrowid


# --------------------------- Установки/гарантия ---------------------------

def add_installation(chat_id: int | None, client_label: str, model_text: str,
                      capacity_class: str | None, city: str | None,
                      install_date: date, warranty_months: int = 24) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO installations
               (chat_id, client_label, model_text, capacity_class, city,
                install_date, warranty_months)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, client_label, model_text, capacity_class, city,
             install_date.isoformat(), warranty_months),
        )
        return cur.lastrowid


def get_installation_by_chat(chat_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM installations WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def installations_needing_review(today: date, days_after: int = 3):
    """Установки, которым пора отправить запрос отзыва (N дней после монтажа)."""
    cutoff = (today - timedelta(days=days_after)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM installations
               WHERE review_requested = 0 AND chat_id IS NOT NULL
                 AND install_date <= ?""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_review_requested(installation_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE installations SET review_requested = 1 WHERE id = ?",
            (installation_id,),
        )


def installations_needing_maintenance(today: date, interval_days: int = 180):
    """Установки, которым пора напомнить о сезонном ТО (раз в interval_days)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM installations WHERE chat_id IS NOT NULL"
        ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        last = row["last_maintenance_reminder"] or row["install_date"]
        last_date = date.fromisoformat(last)
        if (today - last_date).days >= interval_days:
            result.append(row)
    return result


def mark_maintenance_reminded(installation_id: int, today: date) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE installations SET last_maintenance_reminder = ? WHERE id = ?",
            (today.isoformat(), installation_id),
        )


# ------------------------------- Склад -------------------------------

def upsert_inventory_item(name: str, unit: str, initial_quantity: float = 0) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO inventory (name, unit, quantity) VALUES (?, ?, ?)
               ON CONFLICT(name) DO NOTHING""",
            (name, unit, initial_quantity),
        )


def adjust_inventory(name: str, delta: float) -> float | None:
    """Изменяет остаток на delta (может быть отрицательным). Возвращает новый
    остаток, либо None, если такого материала нет на складе."""
    with get_conn() as conn:
        row = conn.execute("SELECT quantity FROM inventory WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        new_qty = row["quantity"] + delta
        conn.execute("UPDATE inventory SET quantity = ? WHERE name = ?", (new_qty, name))
        return new_qty


def adjust_inventory_auto_create(name: str, delta: float, unit_if_missing: str = "ед.") -> tuple[float, bool]:
    """Как adjust_inventory, но если материала ещё нет на складе — заводит
    его сам (с нулевого остатка и единицей unit_if_missing) и потом
    применяет delta. Возвращает (новый_остаток, был_ли_создан)."""
    new_qty = adjust_inventory(name, delta)
    if new_qty is not None:
        return new_qty, False
    upsert_inventory_item(name, unit_if_missing, 0)
    new_qty = adjust_inventory(name, delta)
    return new_qty, True


def list_inventory():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM inventory ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def migrate_material_name(old_name: str, new_name: str, new_unit: str) -> float | None:
    """Если на складе есть материал под старым названием — переносит его
    остаток на новое название (создавая новую строку при необходимости) и
    удаляет старую строку. Возвращает перенесённое количество, либо None,
    если старой строки не было (миграция уже применена или не нужна).
    Безопасно вызывать повторно при каждом старте бота."""
    with get_conn() as conn:
        old_row = conn.execute("SELECT quantity FROM inventory WHERE name = ?", (old_name,)).fetchone()
        if old_row is None:
            return None
        qty = old_row["quantity"]
        conn.execute(
            "INSERT INTO inventory (name, unit, quantity) VALUES (?, ?, 0) "
            "ON CONFLICT(name) DO NOTHING",
            (new_name, new_unit),
        )
        conn.execute("UPDATE inventory SET quantity = quantity + ? WHERE name = ?", (qty, new_name))
        conn.execute("DELETE FROM inventory WHERE name = ?", (old_name,))
        return qty


# ------------------------------- Акты -------------------------------

def add_act(installer_chat_id: int, address: str, photo_file_id: str | None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO acts (created_at, installer_chat_id, address, photo_file_id)
               VALUES (?, ?, ?, ?)""",
            (datetime.now().isoformat(), installer_chat_id, address, photo_file_id),
        )
        return cur.lastrowid


def list_pending_acts():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM acts WHERE status = 'pending' ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ------------------------------- B2B -------------------------------

def add_b2b_point(contract_name: str, point_name: str, interval_days: int = 90) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO b2b_points (contract_name, point_name, interval_days, last_service_date)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(contract_name, point_name) DO UPDATE SET interval_days = excluded.interval_days""",
            (contract_name, point_name, interval_days, date.today().isoformat()),
        )


def mark_b2b_point_serviced(contract_name: str, point_name: str, today: date) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE b2b_points SET last_service_date = ?
               WHERE contract_name = ? AND point_name = ?""",
            (today.isoformat(), contract_name, point_name),
        )
        return cur.rowcount > 0


def list_b2b_points_due(today: date):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM b2b_points").fetchall()
    result = []
    for r in rows:
        row = dict(r)
        last = date.fromisoformat(row["last_service_date"])
        if (today - last).days >= row["interval_days"]:
            result.append(row)
    return result


def list_all_b2b_points():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM b2b_points ORDER BY contract_name, point_name"
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------- Отзывы -------------------------------

def add_review(chat_id: int, installation_id: int | None, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reviews (created_at, chat_id, installation_id, text)
               VALUES (?, ?, ?, ?)""",
            (datetime.now().isoformat(), chat_id, installation_id, text),
        )


# ------------------------- Структурированные акты (материалы) -------------------------

def add_material_act(installer_chat_id: int, capacity_group: str, items: list[dict], total: float) -> int:
    """items — список {"code":, "name":, "unit":, "qty":, "unit_price":, "subtotal":}."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO material_acts (created_at, installer_chat_id, capacity_group, items_json, total)
               VALUES (?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), installer_chat_id, capacity_group, json.dumps(items, ensure_ascii=False), total),
        )
        return cur.lastrowid


def list_material_acts(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM material_acts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        row["items"] = json.loads(row["items_json"])
        result.append(row)
    return result
