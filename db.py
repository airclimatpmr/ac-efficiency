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
            """
        )


# ------------------------------- Лиды -------------------------------

def add_lead(chat_id: int, area: float, top_floor: bool, sunny: bool,
             client_type: str, recommended_capacity: str, contact: str | None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO leads
               (created_at, chat_id, area, top_floor, sunny, client_type,
                recommended_capacity, contact)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), chat_id, area, int(top_floor), int(sunny),
             client_type, recommended_capacity, contact),
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


def list_inventory():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM inventory ORDER BY name").fetchall()
        return [dict(r) for r in rows]


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
