import asyncio
import os
import sqlite3
import hmac, hashlib, json
import uuid
import secrets
import html
import time
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, FSInputFile, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder

dp = Dispatcher()
WEBAPP_URL = os.getenv('WEBAPP_URL', 'https://toolsite-bot-production.up.railway.app')
# Temporary conversation states. Persistent business data is stored in SQLite.
pending = {}
DB_PATH = os.getenv("DB_PATH", "/data/toolsite.db")


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_connect():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'free',
            holder_id INTEGER,
            holder_name TEXT,
            floor TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "username" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if "skill_level" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN skill_level TEXT NOT NULL DEFAULT 'beginner'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS main_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            due_date TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(staff_tasks)").fetchall()}
    if "main_task_id" not in task_columns:
        conn.execute("ALTER TABLE staff_tasks ADD COLUMN main_task_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_tasks_staff ON staff_tasks(staff_id, status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_tasks_main ON staff_tasks(main_task_id, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        staff_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_attachments_task ON task_attachments(task_id, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_task_report_deliveries (
        report_date TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY(report_date,chat_id)
    )""")
    # Предварительный список сотрудников без Telegram-привязки.
    # Отрицательные ID зарезервированы для записей-черновиков и не являются Telegram ID.
    preset_staff = [
        "Мансур", "Амир", "Ваня", "Эмом", "Хусейн", "Дилшод",
        "Мирали", "Мирзоали", "Машраб", "Бахтиёр", "Хочи", "Хасан",
    ]
    for i, name in enumerate(preset_staff):
        exists = conn.execute("SELECT 1 FROM users WHERE name=? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
        if not exists:
            conn.execute("INSERT INTO users (telegram_id, name, created_at) VALUES (?, ?, ?)", (-10001 - i, name, now_text()))

    conn.execute("""CREATE TABLE IF NOT EXISTS admin_accounts (
        telegram_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS security_migrations (
        name TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )""")
    admin_migration = "lock_admins_to_telegram_ids_v1"
    if not conn.execute("SELECT 1 FROM security_migrations WHERE name=?", (admin_migration,)).fetchone():
        current_admins = conn.execute(
            "SELECT telegram_id FROM users WHERE telegram_id>0 AND lower(trim(name)) IN ('эмом','амир')"
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO admin_accounts (telegram_id,created_at) VALUES (?,?)",
            [(row[0], now_text()) for row in current_admins]
        )
        conn.execute("INSERT INTO security_migrations (name,applied_at) VALUES (?,?)", (admin_migration, now_text()))

    # Убираем только старую незарегистрированную карточку-черновик Амира,
    # если подтверждённая учётная запись владельца уже существует.
    cleanup_migration = "remove_amir_placeholder_duplicate_599302909_v1"
    if not conn.execute("SELECT 1 FROM security_migrations WHERE name=?", (cleanup_migration,)).fetchone():
        owner = conn.execute("SELECT 1 FROM users WHERE telegram_id=? AND lower(trim(name))='амир'", (599302909,)).fetchone()
        if owner:
            placeholders = conn.execute("SELECT telegram_id FROM users WHERE telegram_id<0 AND lower(trim(name))='амир'").fetchall()
            for (old_id,) in placeholders:
                for table, column in (
                    ("tools","holder_id"), ("journal","user_id"),
                    ("transfer_requests","sender_id"), ("transfer_requests","recipient_id"),
                    ("staff_locations","telegram_id"), ("consumable_requests","requested_by"),
                    ("site_notes","created_by"), ("chat_messages","user_id"),
                    ("inventory_sessions","created_by"), ("staff_tasks","staff_id"),
                ):
                    try:
                        conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (599302909, old_id))
                    except sqlite3.OperationalError:
                        pass
                conn.execute("DELETE FROM staff_invites WHERE staff_id=?", (old_id,))
                conn.execute("DELETE FROM users WHERE telegram_id=?", (old_id,))
        conn.execute("INSERT INTO security_migrations (name,applied_at) VALUES (?,?)", (cleanup_migration, now_text()))

    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_invites (
            staff_id INTEGER PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            claimed_by INTEGER,
            created_at TEXT NOT NULL,
            claimed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_locations (
            telegram_id INTEGER PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL,
            accuracy REAL, updated_at TEXT NOT NULL, report_date TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS location_daily_requests (
            request_date TEXT PRIMARY KEY, sent_at TEXT NOT NULL, followup_sent INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lunch_reminder_deliveries (
            reminder_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_notification_groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at TEXT NOT NULL
        )
    """)
    chat_columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if "attachment_url" not in chat_columns: conn.execute("ALTER TABLE chat_messages ADD COLUMN attachment_url TEXT")
    if "attachment_name" not in chat_columns: conn.execute("ALTER TABLE chat_messages ADD COLUMN attachment_name TEXT")
    if "attachment_type" not in chat_columns: conn.execute("ALTER TABLE chat_messages ADD COLUMN attachment_type TEXT")
    tool_columns = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
    if "due_at" not in tool_columns: conn.execute("ALTER TABLE tools ADD COLUMN due_at TEXT")
    if "reminder_sent_at" not in tool_columns: conn.execute("ALTER TABLE tools ADD COLUMN reminder_sent_at TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS transfer_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tool_id INTEGER NOT NULL, tool_name TEXT NOT NULL,
        sender_id INTEGER NOT NULL, sender_name TEXT NOT NULL, recipient_id INTEGER NOT NULL,
        recipient_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, responded_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS inventory_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER NOT NULL,
        created_by_name TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
        tool_name TEXT NOT NULL, expected_count INTEGER NOT NULL,
        counted_count INTEGER NOT NULL, difference INTEGER NOT NULL)""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            action TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            floor TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area TEXT NOT NULL,
            note TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_by_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_by TEXT,
            resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consumables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'шт.',
            quantity INTEGER NOT NULL DEFAULT 0,
            min_quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consumable_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumable_id INTEGER NOT NULL,
            consumable_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            note TEXT,
            requested_by INTEGER NOT NULL,
            requested_by_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            responded_at TEXT
        )
    """)

    # Одноразовый импорт стартового остатка: все позиции добавляются свободными.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    migration_name = "initial_inventory_import_2026_11"
    already_imported = conn.execute(
        "SELECT 1 FROM data_migrations WHERE name = ?", (migration_name,)
    ).fetchone()
    if not already_imported:
        inventory = [
            ("Плиткорез", 2),
            ("Пылесос", 2),
            ("Пистолет (тип не уточнён)", 3),
            ("Топор", 3),
            ("Молоток", 4),
            ("Кувалда", 1),
            ("Гвоздодёр", 1),
            ("Киянка", 3),
            ("Удлинитель", 4),
            ("Прожектор", 4),
            ("Миксер строительный", 2),
            ("Лобзик", 1),
            ("Перфоратор", 2),
            ("Болгарка маленькая", 3),
            ("Болгарка большая", 1),
            ("Отбойный молоток", 1),
            ("Шуруповёрт", 2),
            ("Гайковёрт", 1),
            ("Лазер (тип не уточнён)", 1),
            ("Рулетка", 17),
            ("Уровень строительный", 3),
            ("Нож малярный", 11),
            ("Пассатижи", 2),
            ("Ножницы", 2),
            ("Отвёртка", 5),
            ("Пила", 1),
            ("Насадки для шуруповёрта (пачка)", 1),
            ("Свёрла (пачка)", 1),
            ("Переходник для перфоратора", 1),
            ("Пистолет для герметика", 3),
            ("Бита для гайковёрта", 3),
        ]
        for tool_name, quantity in inventory:
            conn.executemany(
                "INSERT INTO tools (name, created_at) VALUES (?, ?)",
                [(tool_name, now_text()) for _ in range(quantity)]
            )
        conn.execute(
            "INSERT INTO data_migrations (name, applied_at) VALUES (?, ?)",
            (migration_name, now_text())
        )
    conn.commit()
    return conn


def main_menu():
    # Keep the Telegram chat clean: the Mini App is the single entry point.
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚀 Открыть ToolSite", web_app=WebAppInfo(url=WEBAPP_URL))
    keyboard.adjust(1)
    return keyboard.as_markup()


def tools_menu():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Добавить инструмент", callback_data="tool_add")
    keyboard.button(text="📋 Показать список", callback_data="tool_list")
    keyboard.button(text="📤 Выдать инструмент", callback_data="tool_issue")
    keyboard.button(text="⬅️ Главное меню", callback_data="home")
    keyboard.adjust(1)
    return keyboard.as_markup()


def users_menu():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Зарегистрироваться / изменить имя", callback_data="user_register")
    keyboard.button(text="👷 Список сотрудников", callback_data="user_list")
    keyboard.button(text="⬅️ Главное меню", callback_data="home")
    keyboard.adjust(1)
    return keyboard.as_markup()


def get_user(telegram_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT name FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def is_admin_name(name):
    normalized = " ".join(str(name or "").casefold().split())
    return normalized in {"эмом", "амир"}


def is_admin_user(telegram_id):
    try:
        uid = int(telegram_id)
    except (TypeError, ValueError):
        return False
    if uid <= 0:
        return False
    with db_connect() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM admin_accounts WHERE telegram_id=?", (uid,)
        ).fetchone())


@dp.message(Command("invites"), F.chat.type == "private")
async def send_staff_invites(message: Message):
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ Создавать приглашения могут только администраторы Эмом и Амир.")
        return
    with db_connect() as c:
        placeholders = c.execute("SELECT telegram_id,name FROM users WHERE telegram_id<0 AND lower(trim(name)) NOT IN ('эмом','амир') ORDER BY name COLLATE NOCASE").fetchall()
        for staff_id, _name in placeholders:
            existing = c.execute("SELECT token FROM staff_invites WHERE staff_id=?", (staff_id,)).fetchone()
            if not existing:
                token = secrets.token_urlsafe(12)
                c.execute("INSERT INTO staff_invites (staff_id,token,created_at) VALUES (?,?,?)", (staff_id,token,now_text()))
        links = c.execute("""SELECT u.name,i.token,i.claimed_by FROM staff_invites i
                             JOIN users u ON u.telegram_id=i.staff_id
                             ORDER BY u.name COLLATE NOCASE""").fetchall()
    bot_info = await message.bot.get_me()
    lines = ["🔐 Персональные ссылки ToolSite:", "Каждая ссылка предназначена только для указанного сотрудника."]
    for name, token, claimed_by in links:
        if claimed_by:
            lines.append(f"\n{name} — уже привязан")
        else:
            lines.append(f"\n{name}: https://t.me/{bot_info.username}?start={token}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)

@dp.message(Command("reinvite"), F.chat.type == "private")
async def rotate_staff_invite(message: Message):
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ Обновлять приглашения могут только администраторы Эмом и Амир.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажи имя сотрудника: /reinvite Эмом")
        return
    name = " ".join(parts[1].strip().split())
    if name.casefold() in {"эмом", "амир"}:
        await message.answer("❌ Для администраторов нельзя создавать обычные приглашения. Их доступ закреплён за Telegram ID.")
        return
    with db_connect() as c:
        staff = c.execute(
            "SELECT telegram_id,name FROM users WHERE telegram_id<0 AND name=? COLLATE NOCASE",
            (name,)
        ).fetchone()
        if not staff:
            await message.answer("❌ Не найден незарегистрированный сотрудник с таким именем.")
            return
        staff_id, staff_name = staff
        token = secrets.token_urlsafe(24)
        existing = c.execute(
            "SELECT 1 FROM staff_invites WHERE staff_id=?", (staff_id,)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE staff_invites SET token=?,claimed_by=NULL,created_at=?,claimed_at=NULL WHERE staff_id=?",
                (token, now_text(), staff_id)
            )
        else:
            c.execute(
                "INSERT INTO staff_invites (staff_id,token,created_at) VALUES (?,?,?)",
                (staff_id, token, now_text())
            )
    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={token}"
    await message.answer(
        f"🔐 Новая ссылка для {staff_name}:\n{link}\n\nПредыдущая ссылка больше не действует.",
        disable_web_page_preview=True
    )


@dp.message(Command("resetstaff"), F.chat.type == "private")
async def reset_staff_account(message: Message):
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ Сбросить аккаунт может только администратор.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or " ".join(parts[1].split()).casefold() != "эмом":
        await message.answer("Укажи сотрудника: /resetstaff Эмом")
        return

    staff_name = "Эмом"
    await message.answer("❌ Сброс записи администратора этой командой запрещён: это может заблокировать доступ. Для смены Telegram-аккаунта администратора нужна отдельная безопасная процедура.")
    return
    with db_connect() as c:
        old_ids = [row[0] for row in c.execute(
            "SELECT telegram_id FROM users WHERE name=? COLLATE NOCASE", (staff_name,)
        ).fetchall()]
        if not old_ids:
            await message.answer("❌ Запись Эмом не найдена.")
            return

        # Remove every old identity and its account-specific data.
        for old_id in old_ids:
            c.execute("UPDATE tools SET status='free',holder_id=NULL,holder_name=NULL,floor=NULL WHERE holder_id=?", (old_id,))
            for table, column in (
                ("staff_tasks", "staff_id"), ("staff_locations", "telegram_id"),
                ("location_daily_requests", "request_date"),
                ("transfer_requests", "sender_id"), ("transfer_requests", "recipient_id"),
                ("consumable_requests", "requested_by"), ("site_notes", "created_by"),
                ("chat_messages", "user_id"), ("journal", "user_id"),
                ("inventory_sessions", "created_by"),
            ):
                if table == "location_daily_requests":
                    continue
                try:
                    c.execute(f"DELETE FROM {table} WHERE {column}=?", (old_id,))
                except sqlite3.OperationalError:
                    pass
            c.execute("DELETE FROM staff_invites WHERE staff_id=? OR claimed_by=?", (old_id, old_id))
            c.execute("DELETE FROM users WHERE telegram_id=?", (old_id,))

        new_id = min([row[0] for row in c.execute("SELECT telegram_id FROM users WHERE telegram_id<0").fetchall()] or [-10000]) - 1
        c.execute("INSERT INTO users (telegram_id,name,created_at,username) VALUES (?,?,?,NULL)", (new_id, staff_name, now_text()))
        token = secrets.token_urlsafe(24)
        c.execute("INSERT INTO staff_invites (staff_id,token,created_at) VALUES (?,?,?)", (new_id, token, now_text()))

    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={token}"
    await message.answer(
        f"✅ Старый аккаунт Эмом сброшен. Старая ссылка аннулирована.\n"
        f"Создана новая запись сотрудника и новое приглашение:\n{link}\n\n"
        f"Отправь эту ссылку Эмому. Старые данные его аккаунта удалены, инструменты, числившиеся за ним, возвращены на склад.",
        disable_web_page_preview=True
    )


@dp.message(Command("chat_notify"), F.chat.type.in_({"group", "supergroup"}))
async def register_chat_notification_group(message: Message):
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ Подключать группы к уведомлениям могут только администраторы.")
        return
    with db_connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO chat_notification_groups (chat_id,title,added_at) VALUES (?,?,?)",
            (message.chat.id, message.chat.title or "Группа команды", now_text())
        )
    await message.answer("✅ Эта группа подключена к уведомлениям ToolSite. Теперь новые сообщения из общего чата будут приходить сюда.")

@dp.message(Command("claim_amir"), F.chat.type == "private")
async def claim_amir_account(message: Message):
    uid = message.from_user.id
    username = (message.from_user.username or "").casefold()
    if uid != 599302909 or username != "quasar_f4":
        await message.answer("❌ Эта привязка разрешена только подтверждённому аккаунту Амира.")
        return
    with db_connect() as c:
        current = c.execute("SELECT name FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if current and current[0].strip().casefold() != "амир":
            await message.answer("❌ Этот Telegram ID уже закреплён за другой учётной записью. Нужна ручная проверка.")
            return
        placeholder = c.execute("SELECT telegram_id FROM users WHERE telegram_id<0 AND name=? COLLATE NOCASE", ("Амир",)).fetchone()
        if not current and not placeholder:
            await message.answer("❌ Не найдена свободная карточка «Амир». Ничего не изменено.")
            return
        if not current:
            old_id = placeholder[0]
            for table, column in (
                ("tools","holder_id"), ("journal","user_id"),
                ("transfer_requests","sender_id"), ("transfer_requests","recipient_id"),
                ("staff_locations","telegram_id"), ("consumable_requests","requested_by"),
                ("site_notes","created_by"), ("chat_messages","user_id"),
                ("inventory_sessions","created_by"), ("staff_tasks","staff_id"),
            ):
                try:
                    c.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (uid, old_id))
                except sqlite3.OperationalError:
                    pass
            c.execute("UPDATE users SET telegram_id=?,username=? WHERE telegram_id=?", (uid, "quasar_f4", old_id))
        else:
            c.execute("UPDATE users SET username=? WHERE telegram_id=?", ("quasar_f4", uid))
        c.execute("INSERT OR IGNORE INTO admin_accounts (telegram_id,created_at) VALUES (?,?)", (uid, now_text()))
    await message.answer("✅ Аккаунт @quasar_f4 закреплён за сотрудником «Амир». Права администратора включены.")


@dp.message(Command("myid"), F.chat.type == "private")
async def show_my_telegram_id(message: Message):
    user = message.from_user
    username = f"@{user.username}" if user.username else "не задан"
    await message.answer(f"Твой Telegram ID: `{user.id}`\\nUsername: {username}", parse_mode="Markdown")


@dp.message(CommandStart())
async def start(message: Message):
    pending.pop(message.from_user.id, None)
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload:
        with db_connect() as c:
            invite = c.execute("SELECT staff_id,claimed_by FROM staff_invites WHERE token=?", (payload,)).fetchone()
            if not invite:
                await message.answer("❌ Ссылка приглашения недействительна. Попроси администратора прислать новую.")
                return
            staff_id, claimed_by = invite
            current = c.execute("SELECT name FROM users WHERE telegram_id=?", (uid,)).fetchone()
            target = c.execute("SELECT name FROM users WHERE telegram_id=?", (staff_id,)).fetchone()
            if target and target[0].strip().casefold() in {"эмом", "амир"} and uid not in {
                row[0] for row in c.execute("SELECT telegram_id FROM admin_accounts").fetchall()
            }:
                await message.answer("❌ Приглашение администратора нельзя активировать как обычный сотрудник. Обратись к действующему администратору.")
                return
            if claimed_by and int(claimed_by) != uid:
                await message.answer("❌ Это приглашение уже использовано другим Telegram-аккаунтом. Обратись к администратору.")
                return
            if current:
                if target and current[0] != target[0]:
                    await message.answer("❌ Этот Telegram уже закреплён за другим сотрудником. Обратись к администратору.")
                    return
                if target:
                    c.execute("DELETE FROM users WHERE telegram_id=?", (staff_id,))
            else:
                if not target:
                    await message.answer("❌ Запись сотрудника уже привязана или удалена. Обратись к администратору.")
                    return
                duplicate = c.execute("SELECT telegram_id FROM users WHERE name=? AND telegram_id>0", (target[0],)).fetchone()
                if duplicate:
                    await message.answer("❌ Для этого сотрудника уже есть Telegram-аккаунт. Обратись к администратору.")
                    return
                for table, column in (
                    ("tools","holder_id"), ("journal","user_id"),
                    ("transfer_requests","sender_id"), ("transfer_requests","recipient_id"),
                    ("staff_locations","telegram_id"), ("consumable_requests","requested_by"),
                    ("site_notes","created_by"), ("chat_messages","user_id"),
                    ("inventory_sessions","created_by"), ("staff_tasks","staff_id"),
                ):
                    try:
                        c.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (uid,staff_id))
                    except sqlite3.OperationalError:
                        pass
                c.execute("UPDATE users SET telegram_id=? WHERE telegram_id=?", (uid, staff_id))
            c.execute("UPDATE staff_invites SET claimed_by=?,claimed_at=? WHERE staff_id=?", (uid, now_text(), staff_id))
            linked = c.execute("SELECT name FROM users WHERE telegram_id=?", (uid,)).fetchone()
        await message.answer(f"✅ Аккаунт закреплён за сотрудником: {linked[0]}. Теперь открывай ToolSite через кнопку ниже.", reply_markup=main_menu())
        return
    user = get_user(uid)
    if user:
        await message.answer("✅ Ты уже зарегистрирован в ToolSite.", reply_markup=main_menu())
    else:
        await message.answer("Для входа в ToolSite нужна персональная ссылка-приглашение. Попроси её у администратора.")

@dp.message(Command("cancel"))
async def cancel_action(message: Message):
    pending.pop(message.from_user.id, None)
    await message.answer("Действие отменено.", reply_markup=main_menu())


@dp.message(F.location, F.chat.type == "private")
@dp.edited_message(F.location, F.chat.type == "private")
async def receive_staff_location(message: Message):
    uid = message.from_user.id
    user = get_user(uid)
    if not user:
        if not message.edit_date:
            await message.answer("Сначала зарегистрируйся в ToolSite, чтобы привязать геопозицию к своему имени.")
        return
    loc = message.location
    local_now = datetime.now(ZoneInfo("Europe/Moscow"))
    with db_connect() as c:
        c.execute("""INSERT INTO staff_locations (telegram_id,latitude,longitude,accuracy,updated_at,report_date) VALUES (?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude,accuracy=excluded.accuracy,updated_at=excluded.updated_at,report_date=excluded.report_date""",(uid,loc.latitude,loc.longitude,loc.horizontal_accuracy,local_now.isoformat(timespec="seconds"),local_now.date().isoformat()))
    # Telegram sends subsequent live-location positions as edited_message updates.
    # Save those silently so the map follows the employee without spamming the chat.
    if message.edit_date:
        return
    if loc.live_period:
        await message.answer("🟢 Трансляция геопозиции принята! ToolSite будет обновлять твою точку на карте, пока Telegram передаёт координаты.")
    else:
        await message.answer("📍 Получена разовая отметка. Чтобы точка двигалась на карте, отправь именно трансляцию: 📎 → Геопозиция → «Транслировать геопозицию» и выбери время.")
@dp.message(F.text)
async def receive_text(message: Message):
    user_id = message.from_user.id
    state = pending.get(user_id)
    if not state:
        return

    value = message.text.strip()
    if value.startswith("/"):
        await message.answer("Отправь обычный текст или /cancel для отмены.")
        return
    if not value:
        await message.answer("Поле не должно быть пустым. Попробуй ещё раз.")
        return

    if state["action"] == "add_tool":
        if not is_admin_user(user_id):
            pending.pop(user_id, None)
            await message.answer("Добавлять инструменты могут только администраторы.")
            return
        if len(value) > 100:
            await message.answer("Название слишком длинное. Максимум 100 символов.")
            return
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO tools (name, created_at) VALUES (?, ?)",
                (value, now_text())
            )
            tool_id = cur.lastrowid
        pending.pop(user_id, None)
        await message.answer(
            f"✅ Инструмент добавлен!\n№{tool_id}: {value}\nСтатус: Свободен",
            reply_markup=tools_menu()
        )
        return

    if state["action"] == "register":
        if len(value) > 60:
            await message.answer("Имя слишком длинное. Максимум 60 символов.")
            return
        with db_connect() as conn:
            conn.execute(
                """INSERT INTO users (telegram_id, name, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name""",
                (user_id, value, now_text())
            )
        pending.pop(user_id, None)
        await message.answer(f"✅ Ты зарегистрирован в ToolSite под именем: {value}", reply_markup=main_menu())
        return

    if state["action"] == "issue_floor":
        if len(value) > 40:
            await message.answer("Название этажа слишком длинное. Максимум 40 символов.")
            return
        tool_id = state["tool_id"]
        user = get_user(user_id)
        if not user:
            pending.pop(user_id, None)
            await message.answer("Сначала зарегистрируйся в разделе «Пользователи».", reply_markup=users_menu())
            return
        with db_connect() as conn:
            tool = conn.execute(
                "SELECT name, status FROM tools WHERE id = ?", (tool_id,)
            ).fetchone()
            if not tool:
                pending.pop(user_id, None)
                await message.answer("Инструмент не найден. Обнови список.")
                return
            if tool[1] != "free":
                pending.pop(user_id, None)
                await message.answer("Этот инструмент уже выдан другому сотруднику.")
                return
            changed = conn.execute(
                """UPDATE tools SET status = 'issued', holder_id = ?, holder_name = ?, floor = ?
                   WHERE id = ? AND status = 'free'""",
                (user_id, user[0], value, tool_id)
            ).rowcount
            if changed:
                conn.execute(
                    """INSERT INTO journal
                       (tool_id, tool_name, action, user_id, user_name, floor, created_at)
                       VALUES (?, ?, 'Выдача', ?, ?, ?, ?)""",
                    (tool_id, tool[0], user_id, user[0], value, now_text())
                )
        pending.pop(user_id, None)
        if changed:
            await message.answer(
                f"✅ Инструмент выдан!\n\n№{tool_id}: {tool[0]}\nСотрудник: {user[0]}\nЭтаж: {value}",
                reply_markup=main_menu()
            )
        else:
            await message.answer("Не удалось выдать инструмент: возможно, его уже забрали.")
        return


@dp.callback_query(F.data == "home")
async def go_home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "tools")
async def open_tools(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    await callback.message.answer(f"🔧 ИНСТРУМЕНТЫ\n━━━━━━━━━━━━━━━━━━\nВсего в базе: {total}\nДобавляй оборудование, смотри остатки или оформляй выдачу.", reply_markup=tools_menu())


@dp.callback_query(F.data == "tool_add")
async def add_tool_start(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Только администратор может добавлять инструменты.", show_alert=True)
        return
    pending[callback.from_user.id] = {"action": "add_tool"}
    await callback.answer()
    await callback.message.answer("➕ Напиши название инструмента.\nНапример: Перфоратор Bosch GBH 2-26.\nДля отмены отправь /cancel.")


@dp.callback_query(F.data == "tool_list")
async def show_tools(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, name, status, holder_name, floor FROM tools ORDER BY id DESC"
        ).fetchall()
    if not rows:
        text = "📋 Список пока пуст. Нажми «Добавить инструмент», чтобы внести первое оборудование."
    else:
        free_count = sum(1 for row in rows if row[2] == "free")
        issued_count = len(rows) - free_count
        lines = ["📋 СКЛАД ИНСТРУМЕНТОВ", "━━━━━━━━━━━━━━━━━━", f"Всего: {len(rows)}  ·  🟢 Свободно: {free_count}  ·  🔴 Выдано: {issued_count}", ""]
        for tool_id, name, status, holder_name, floor in rows:
            state = "🟢 СВОБОДЕН" if status == "free" else f"🔴 У {holder_name or 'сотрудника'}"
            if floor:
                state += f" · этаж: {floor}"
            lines.append(f"№{tool_id} — {name}\n{state}")
        text = "\n\n".join(lines)
    await callback.message.answer(text, reply_markup=tools_menu())


@dp.callback_query(F.data == "tool_issue")
async def issue_tool_start(callback: CallbackQuery):
    await callback.answer()
    user = get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("Чтобы выдать инструмент, сначала зарегистрируй своё имя.", reply_markup=users_menu())
        return
    with db_connect() as conn:
        rows = conn.execute("SELECT id, name FROM tools WHERE status = 'free' ORDER BY name").fetchall()
    if not rows:
        await callback.message.answer("Сейчас нет свободных инструментов.", reply_markup=tools_menu())
        return
    keyboard = InlineKeyboardBuilder()
    for tool_id, name in rows:
        keyboard.button(text=f"№{tool_id} — {name}", callback_data=f"issue:{tool_id}")
    keyboard.button(text="⬅️ Назад", callback_data="tools")
    keyboard.adjust(1)
    await callback.message.answer("Выбери инструмент для выдачи:", reply_markup=keyboard.as_markup())


@dp.callback_query(F.data.startswith("issue:"))
async def select_tool_to_issue(callback: CallbackQuery):
    await callback.answer()
    try:
        tool_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("Некорректный номер инструмента.")
        return
    with db_connect() as conn:
        tool = conn.execute("SELECT name, status FROM tools WHERE id = ?", (tool_id,)).fetchone()
    if not tool or tool[1] != "free":
        await callback.message.answer("Этот инструмент уже недоступен. Обнови список.")
        return
    pending[callback.from_user.id] = {"action": "issue_floor", "tool_id": tool_id}
    await callback.message.answer(
        f"Выбран: {tool[0]}\n\nНапиши этаж или зону, например: 2 этаж или Подвал.\nДля отмены /cancel."
    )


@dp.callback_query(F.data == "my_tools")
async def my_tools(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, name, floor FROM tools WHERE holder_id = ? ORDER BY id",
            (callback.from_user.id,)
        ).fetchall()
    if not rows:
        await callback.message.answer("📋 Сейчас за тобой не закреплено ни одного инструмента.", reply_markup=main_menu())
        return
    for tool_id, name, floor in rows:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="↩️ Вернуть инструмент", callback_data=f"return:{tool_id}")
        keyboard.button(text="🔁 Передать сотруднику", callback_data=f"transfer:start:{tool_id}")
        await callback.message.answer(
            f"№{tool_id} — {name}" + (f"\nЭтаж: {floor}" if floor else ""),
            reply_markup=keyboard.as_markup()
        )


@dp.callback_query(F.data.startswith("return:"))
async def return_tool(callback: CallbackQuery):
    await callback.answer()
    try:
        tool_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("Некорректный номер инструмента.")
        return
    user = get_user(callback.from_user.id)
    with db_connect() as conn:
        tool = conn.execute(
            "SELECT name, floor FROM tools WHERE id = ? AND holder_id = ? AND status = 'issued'",
            (tool_id, callback.from_user.id)
        ).fetchone()
        if not tool:
            await callback.message.answer("Этот инструмент не закреплён за тобой.")
            return
        conn.execute(
            "UPDATE tools SET status = 'free', holder_id = NULL, holder_name = NULL, floor = NULL WHERE id = ?",
            (tool_id,)
        )
        conn.execute(
            """INSERT INTO journal
               (tool_id, tool_name, action, user_id, user_name, floor, created_at)
               VALUES (?, ?, 'Возврат', ?, ?, ?, ?)""",
            (tool_id, tool[0], callback.from_user.id, user[0] if user else "Неизвестен", tool[1], now_text())
        )
    await callback.message.answer(f"✅ Возврат принят.\n№{tool_id}: {tool[0]} теперь свободен.", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("transfer:start:"))
async def transfer_start(callback: CallbackQuery):
    await callback.answer()
    try:
        tool_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("Некорректный номер инструмента.")
        return

    with db_connect() as conn:
        tool = conn.execute(
            "SELECT name FROM tools WHERE id = ? AND holder_id = ? AND status = 'issued'",
            (tool_id, callback.from_user.id)
        ).fetchone()
        people = conn.execute(
            "SELECT telegram_id, name FROM users WHERE telegram_id != ? ORDER BY name COLLATE NOCASE",
            (callback.from_user.id,)
        ).fetchall()

    if not tool:
        await callback.message.answer("Этот инструмент сейчас не закреплён за тобой.")
        return
    if not people:
        await callback.message.answer("Пока нет других зарегистрированных сотрудников. Попроси коллегу зарегистрироваться в ToolSite.")
        return

    keyboard = InlineKeyboardBuilder()
    for person_id, person_name in people:
        keyboard.button(
            text=person_name[:50],
            callback_data=f"transfer:to:{tool_id}:{person_id}"
        )
    keyboard.button(text="⬅️ Отмена", callback_data="my_tools")
    keyboard.adjust(1)
    await callback.message.answer(
        f"Кому передать инструмент «{tool[0]}»? Выбери сотрудника:",
        reply_markup=keyboard.as_markup()
    )


@dp.callback_query(F.data.startswith("transfer:to:"))
async def transfer_to_user(callback: CallbackQuery):
    await callback.answer()
    try:
        _, _, tool_id_text, recipient_id_text = callback.data.split(":")
        tool_id, recipient_id = int(tool_id_text), int(recipient_id_text)
    except (ValueError, IndexError):
        await callback.message.answer("Не удалось обработать передачу. Попробуй ещё раз.")
        return
    sender = get_user(callback.from_user.id)
    with db_connect() as conn:
        tool = conn.execute(
            "SELECT name FROM tools WHERE id=? AND holder_id=? AND status='issued'",
            (tool_id, callback.from_user.id)
        ).fetchone()
        recipient = conn.execute("SELECT name FROM users WHERE telegram_id=?", (recipient_id,)).fetchone()
        duplicate = conn.execute(
            "SELECT 1 FROM transfer_requests WHERE tool_id=? AND status='pending'", (tool_id,)
        ).fetchone()
        if not sender or not tool or not recipient or recipient_id <= 0 or duplicate:
            await callback.message.answer("Передача недоступна: проверь инструмент и получателя.")
            return
        cur = conn.execute(
            """INSERT INTO transfer_requests
               (tool_id,tool_name,sender_id,sender_name,recipient_id,recipient_name,status,created_at)
               VALUES (?,?,?,?,?,?,'pending',?)""",
            (tool_id, tool[0], callback.from_user.id, sender[0], recipient_id, recipient[0], now_text())
        )
        request_id = cur.lastrowid
    await callback.message.answer(
        f"Запрос на передачу №{request_id} отправлен. Инструмент останется за тобой до подтверждения."
    )
    try:
        await callback.bot.send_message(
            recipient_id,
            f"🔔 {sender[0]} предлагает принять инструмент №{tool_id} — {tool[0]}. "
            "Открой ToolSite → Сотрудники → Входящие передачи и подтверди или отклони."
        )
    except Exception:
        pass


@dp.callback_query(F.data == "users")
async def open_users(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("👷 Раздел сотрудников:", reply_markup=users_menu())


@dp.callback_query(F.data == "user_register")
async def register_start(callback: CallbackQuery):
    pending[callback.from_user.id] = {"action": "register"}
    await callback.answer()
    await callback.message.answer("Напиши своё имя и фамилию, например: Иван Петров.\nДля отмены отправь /cancel.")


@dp.callback_query(F.data == "user_list")
async def user_list(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        rows = conn.execute("SELECT name FROM users ORDER BY name COLLATE NOCASE").fetchall()
    if not rows:
        text = "Сотрудников пока нет. Нажми «Зарегистрироваться»."
    else:
        text = "👷 Зарегистрированные сотрудники:\n\n" + "\n".join(f"• {row[0]}" for row in rows)
    await callback.message.answer(text, reply_markup=users_menu())


@dp.callback_query(F.data == "journal")
async def show_journal(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT tool_name, action, user_name, floor, created_at
               FROM journal ORDER BY id DESC LIMIT 30"""
        ).fetchall()
    if not rows:
        text = "📖 Журнал пока пуст. Записи появятся после первой выдачи или возврата."
    else:
        lines = ["📖 ЖУРНАЛ ОПЕРАЦИЙ", "━━━━━━━━━━━━━━━━━━", "Последние 30 событий", ""]
        for tool_name, action, user_name, floor, created_at in rows:
            stamp = created_at.replace("T", " ")[:16]
            icon = "🟢" if action == "Выдача" else ("↩️" if action == "Возврат" else "🔁")
            line = f"{icon} {action.upper()} · {tool_name}\n👤 {user_name}"
            if floor:
                line += f" · этаж: {floor}"
            lines.append(f"{line}\n{stamp}")
        text = "\n\n".join(lines)
    await callback.message.answer(text, reply_markup=main_menu())


@dp.callback_query(F.data == "map")
async def show_map(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(TRIM(floor), ''), 'Этаж не указан') AS floor_name,
                      name, COALESCE(holder_name, 'Сотрудник не указан')
               FROM tools
               WHERE status = 'issued'
               ORDER BY floor_name COLLATE NOCASE, name COLLATE NOCASE
               LIMIT 100"""
        ).fetchall()

    if not rows:
        text = "📍 Карта этажей пока пустая. Когда инструмент выдадут сотруднику с указанием этажа, он появится здесь."
    else:
        lines = ["📍 КАРТА ОБЪЕКТА", "━━━━━━━━━━━━━━━━━━", "Расположение выданных инструментов", ""]
        current_floor = None
        for floor_name, tool_name, holder_name in rows:
            if floor_name != current_floor:
                current_floor = floor_name
                lines.append(f"🏢 {current_floor}")
            lines.append(f"  • {tool_name} — {holder_name}")
        text = "\n".join(lines)

    await callback.message.answer(text, reply_markup=main_menu())


def validate_init_data(init_data, bot_token):
    try:
        parsed = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        if not parsed or len({key for key, _ in parsed}) != len(parsed):
            return None
        pairs = dict(parsed)
        received_hash = pairs.pop("hash", None)
        if not received_hash or not bot_token:
            return None
        check = "\n".join(f"{k}={v}" for k,v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        age = datetime.now(timezone.utc).timestamp() - auth_date
        if age < -60 or age > 43200:
            return None
        user = json.loads(pairs.get("user","{}"))
        return user if user.get("id") else None
    except Exception:
        return None

API_RATE_LIMITS = {}

@web.middleware
async def api_auth(request, handler):
    if request.path.startswith("/api/"):
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data",""), os.getenv("BOT_TOKEN",""))
        if not user:
            return web.json_response({"error":"Открой приложение через Telegram заново"},status=401)
        uid = int(user["id"])
        now = time.monotonic()
        bucket = API_RATE_LIMITS.get(uid)
        if bucket is None or now - bucket[0] >= 60:
            API_RATE_LIMITS[uid] = (now, 1)
        else:
            if bucket[1] >= 180:
                return web.json_response({"error":"Слишком много запросов. Подожди минуту."},status=429)
            API_RATE_LIMITS[uid] = (bucket[0], bucket[1] + 1)
        if len(API_RATE_LIMITS) > 5000:
            API_RATE_LIMITS.clear()
        if not get_user(uid):
            return web.json_response({"error":"Нет доступа. Используй персональное приглашение администратора."},status=403)
        request["tg_user"] = user
    return await handler(request)

async def app_index(request):
    # Inject the public browser-side Yandex Maps key at response time;
    # keep it out of the repository and source file.
    html = open("webapp/index.html", encoding="utf-8").read()
    html = html.replace("__YANDEX_MAPS_API_KEY__", os.getenv("YANDEX_MAPS_API_KEY", ""))
    return web.Response(text=html, content_type="text/html", charset="utf-8", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })

async def app_map_image(request):
    return web.FileResponse("webapp/Спутниковая карта строительного комплекса.png", headers={
        "Cache-Control": "public, max-age=3600",
    })

async def api_state(request):
    tg=request["tg_user"]; uid=int(tg["id"]); admin=is_admin_user(uid)
    with db_connect() as c:
        username=str(tg.get("username") or "").strip().lstrip("@") or None
        if username:
            c.execute("UPDATE users SET username=? WHERE telegram_id=?",(username,uid))
        user=c.execute("SELECT name FROM users WHERE telegram_id=?",(uid,)).fetchone()
        tools=c.execute("SELECT id,name,status,holder_id,holder_name,floor,due_at FROM tools ORDER BY name COLLATE NOCASE").fetchall()
        staff=c.execute("SELECT telegram_id,name,username,skill_level FROM users ORDER BY name COLLATE NOCASE").fetchall()
        main_task_rows=c.execute("""SELECT m.id,m.title,m.description,m.due_date,m.created_at,
            COUNT(t.id),SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END)
            FROM main_tasks m LEFT JOIN staff_tasks t ON t.main_task_id=m.id
            GROUP BY m.id ORDER BY m.id DESC""").fetchall()
        journal=c.execute("SELECT tool_name,action,user_name,floor,created_at FROM journal ORDER BY id DESC LIMIT 50").fetchall()
        incoming=c.execute("SELECT id,tool_id,tool_name,sender_id,sender_name,created_at FROM transfer_requests WHERE recipient_id=? AND status='pending' ORDER BY id DESC",(uid,)).fetchall()
        consumables=c.execute("SELECT id,name,unit,quantity,min_quantity,updated_at FROM consumables ORDER BY name COLLATE NOCASE").fetchall()
        if admin:
            consumable_requests=c.execute("SELECT id,consumable_id,consumable_name,quantity,note,requested_by_name,status,created_at FROM consumable_requests WHERE status='pending' ORDER BY id DESC LIMIT 100").fetchall()
        else:
            consumable_requests=c.execute("SELECT id,consumable_id,consumable_name,quantity,note,requested_by_name,status,created_at FROM consumable_requests WHERE requested_by=? ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
        locations=c.execute("SELECT l.telegram_id,u.name,l.latitude,l.longitude,l.accuracy,l.updated_at,u.username FROM staff_locations l JOIN users u ON u.telegram_id=l.telegram_id ORDER BY l.updated_at DESC").fetchall()
        site_notes=c.execute("SELECT id,area,note,created_by_name,status,created_at,resolved_by,resolved_at FROM site_notes ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,id DESC LIMIT 100").fetchall()
        task_rows=c.execute("SELECT t.id,t.staff_id,u.name,t.title,t.description,t.due_date,t.status,t.created_at,t.completed_at,t.main_task_id FROM staff_tasks t JOIN users u ON u.telegram_id=t.staff_id "+("ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,t.id DESC" if admin else "WHERE t.staff_id=? ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,t.id DESC"),(() if admin else (uid,))).fetchall()
        task_attachments=c.execute("SELECT a.task_id,a.url,a.name,a.created_at FROM task_attachments a JOIN staff_tasks t ON t.id=a.task_id "+("" if admin else "WHERE t.staff_id=?")+" ORDER BY a.id",(() if admin else (uid,))).fetchall()
    attachments_by_task={}
    for a in task_attachments: attachments_by_task.setdefault(a[0],[]).append({"url":a[1],"name":a[2],"created_at":a[3]})
    total=len(tools); free=sum(1 for t in tools if t[2]=="free")
    return web.json_response({"profile":{"id":uid,"name":user[0] if user else ""},"is_admin":bool(admin),"main_tasks":[{"id":r[0],"title":r[1],"description":r[2],"due_date":r[3],"created_at":r[4],"total":r[5],"done":r[6] or 0} for r in main_task_rows],"stats":{"total":total,"free":free,"issued":total-free,"staff":len(staff)},"tools":[{"id":t[0],"name":t[1],"status":t[2],"holder_id":t[3],"holder":t[4],"floor":t[5],"due_at":t[6]} for t in tools],"staff":[{"id":p[0],"name":p[1],"username":p[2],"skill_level":p[3] or "beginner"} for p in staff],"journal":[{"tool":j[0],"action":j[1],"user":j[2],"floor":j[3],"time":j[4]} for j in journal],"my_tools":[t[0] for t in tools if t[3]==uid],"incoming_transfers":[{"id":r[0],"tool_id":r[1],"tool":r[2],"sender_id":r[3],"sender":r[4],"created_at":r[5]} for r in incoming],"consumables":[{"id":r[0],"name":r[1],"unit":r[2],"quantity":r[3],"min_quantity":r[4],"updated_at":r[5]} for r in consumables],"consumable_requests":[{"id":r[0],"consumable_id":r[1],"name":r[2],"quantity":r[3],"note":r[4],"requested_by":r[5],"status":r[6],"created_at":r[7]} for r in consumable_requests], "locations":[{"staff_id":r[0],"name":r[1],"latitude":r[2],"longitude":r[3],"accuracy":r[4],"updated_at":r[5],"username":r[6]} for r in locations],"site_notes":[{"id":r[0],"area":r[1],"note":r[2],"created_by":r[3],"status":r[4],"created_at":r[5],"resolved_by":r[6],"resolved_at":r[7]} for r in site_notes],"tasks":[{"id":r[0],"staff_id":r[1],"staff_name":r[2],"title":r[3],"description":r[4],"due_date":r[5],"status":r[6],"created_at":r[7],"completed_at":r[8],"main_task_id":r[9],"attachments":attachments_by_task.get(r[0],[])} for r in task_rows]})

async def api_site_note_add(request):
    uid=int(request["tg_user"]["id"])
    user=get_user(uid)
    if not user:
        return web.json_response({"error":"Сначала зарегистрируйся в профиле"},status=403)
    data=await request.json()
    area=str(data.get("area","")).strip()[:80]
    note=str(data.get("note","")).strip()[:600]
    if len(area)<2 or len(note)<3:
        return web.json_response({"error":"Укажи участок и опиши задачу или замечание"},status=400)
    with db_connect() as c:
        cur=c.execute("INSERT INTO site_notes (area,note,created_by,created_by_name,status,created_at) VALUES (?,?,?,?, 'open', ?)",(area,note,uid,user[0],now_text()))
        note_id=cur.lastrowid
    return web.json_response({"ok":True,"id":note_id})


async def api_site_note_resolve(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Закрывать задачи могут только администраторы Эмом и Амир"},status=403)
    data=await request.json()
    try: note_id=int(data.get("id"))
    except (TypeError,ValueError): return web.json_response({"error":"Задача не выбрана"},status=400)
    user=get_user(uid)
    with db_connect() as c:
        changed=c.execute("UPDATE site_notes SET status='done',resolved_by=?,resolved_at=? WHERE id=? AND status='open'",(user[0] if user else "Администратор",now_text(),note_id)).rowcount
    if not changed: return web.json_response({"error":"Задача уже закрыта или не найдена"},status=404)
    return web.json_response({"ok":True})


async def api_chat_messages(request):
    uid = int(request["tg_user"]["id"])
    try:
        after = max(0, int(request.query.get("after", "0")))
    except (TypeError, ValueError):
        after = 0
    with db_connect() as c:
        user = c.execute("SELECT name FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not user:
            return web.json_response({"error":"Сначала зарегистрируйся в профиле ToolSite"}, status=403)
        rows = c.execute("SELECT id,user_id,user_name,message,created_at,attachment_url,attachment_name,attachment_type FROM chat_messages WHERE id>? ORDER BY id DESC LIMIT 100", (after,)).fetchall()
    rows.reverse()
    return web.json_response({"messages":[{"id":r[0],"user_id":r[1],"user_name":r[2],"message":r[3],"created_at":r[4],"attachment_url":r[5],"attachment_name":r[6],"attachment_type":r[7]} for r in rows]})

async def api_chat_send(request):
    uid = int(request["tg_user"]["id"])
    try:
        data = await request.json()
        message = str(data.get("message", "")).strip()
        attachment = data.get("attachment") or {}
        attachment_url = str(attachment.get("url", ""))
        attachment_name = str(attachment.get("name", ""))[:180]
        attachment_type = str(attachment.get("type", ""))[:100]
        if attachment_url and (not attachment_url.startswith("/chat-files/") or "/" in attachment_url[len("/chat-files/"):]):
            return web.json_response({"error":"Некорректное вложение"}, status=400)
    except Exception:
        return web.json_response({"error":"Некорректное сообщение"}, status=400)
    if not message and not attachment_url:
        return web.json_response({"error":"Напиши сообщение или прикрепи файл"}, status=400)
    if len(message) > 1000:
        return web.json_response({"error":"Максимум 1000 символов"}, status=400)
    with db_connect() as c:
        user = c.execute("SELECT name FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not user:
            return web.json_response({"error":"Сначала зарегистрируйся в профиле ToolSite"}, status=403)
        recent = c.execute("SELECT created_at FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if recent:
            try:
                elapsed = (datetime.now().astimezone() - datetime.fromisoformat(recent[0])).total_seconds()
                if elapsed < 2:
                    return web.json_response({"error":"Подожди пару секунд перед следующим сообщением"}, status=429)
            except Exception:
                pass
        created = now_text()
        cur = c.execute("INSERT INTO chat_messages (user_id,user_name,message,created_at,attachment_url,attachment_name,attachment_type) VALUES (?,?,?,?,?,?,?)", (uid, user[0], message, created, attachment_url or None, attachment_name or None, attachment_type or None))
        mid = cur.lastrowid
        recipients = [row[0] for row in c.execute("SELECT telegram_id FROM users WHERE telegram_id>0").fetchall()]
        groups = c.execute("SELECT chat_id FROM chat_notification_groups").fetchall()
    stamp = datetime.fromisoformat(created).astimezone().strftime("%H:%M")
    body = message or ("🎤 Голосовое сообщение" if attachment_type and attachment_type.startswith("audio/") else "Отправлено вложение")
    if attachment_name and not (attachment_type and attachment_type.startswith("audio/")):
        body += f"\n📎 {attachment_name}"
    notice = f"💬 Новое сообщение в общем чате ToolSite\nОт: {user[0]}\nЧто: {body[:900]}\nВо сколько: {stamp}"
    bot = request.app["bot"]
    for recipient_id in recipients:
        try:
            await bot.send_message(recipient_id, notice)
        except Exception as exc:
            print(f"Chat notification to {recipient_id} failed: {exc}")
    for (group_id,) in groups:
        try:
            await bot.send_message(group_id, notice)
        except Exception as exc:
            print(f"Chat group notification to {group_id} failed: {exc}")
    return web.json_response({"ok":True,"message":{"id":mid,"user_id":uid,"user_name":user[0],"message":message,"created_at":created,"attachment_url":attachment_url or None,"attachment_name":attachment_name or None,"attachment_type":attachment_type or None}})


async def api_chat_upload(request):
    uid = int(request["tg_user"]["id"])
    with db_connect() as c:
        user = c.execute("SELECT 1 FROM users WHERE telegram_id=?", (uid,)).fetchone()
    if not user:
        return web.json_response({"error":"Сначала зарегистрируйся в профиле"}, status=403)
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error":"Файл не найден"}, status=400)
        original = Path(field.filename or "file").name
        content_type = (field.headers.get("Content-Type") or "application/octet-stream").lower().split(";", 1)[0].strip()
        allowed = {"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","application/pdf":".pdf","text/plain":".txt","application/msword":".doc","application/vnd.openxmlformats-officedocument.wordprocessingml.document":".docx","application/vnd.ms-excel":".xls","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":".xlsx","audio/webm":".webm","audio/ogg":".ogg","application/ogg":".ogg","audio/mp4":".m4a","audio/mpeg":".mp3","audio/wav":".wav"}
        if content_type not in allowed:
            return web.json_response({"error":"Можно прикрепить фото, PDF, Word, Excel или TXT"}, status=415)
        folder = Path(os.getenv("CHAT_UPLOAD_DIR", "/data/chat_uploads"))
        folder.mkdir(parents=True, exist_ok=True)
        filename = uuid.uuid4().hex + allowed[content_type]
        target = folder / filename
        size = 0
        with target.open("wb") as out:
            while True:
                chunk = await field.read_chunk(64 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    out.close()
                    target.unlink(missing_ok=True)
                    return web.json_response({"error":"Максимальный размер файла — 8 МБ"}, status=413)
                out.write(chunk)
        if size == 0:
            target.unlink(missing_ok=True)
            return web.json_response({"error":"Файл пустой"}, status=400)
        return web.json_response({"url":"/chat-files/"+filename,"name":original[:180],"type":content_type})
    except Exception:
        return web.json_response({"error":"Не удалось загрузить вложение"}, status=400)

async def app_chat_file(request):
    filename = request.match_info.get("filename", "")
    if not filename or Path(filename).name != filename or not filename.replace(".", "").isalnum():
        raise web.HTTPNotFound()
    path = Path(os.getenv("CHAT_UPLOAD_DIR", "/data/chat_uploads")) / filename
    if not path.is_file(): raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"X-Content-Type-Options":"nosniff","Cache-Control":"public, max-age=31536000"})

async def api_tool_history(request):
    try:
        tid = int(request.match_info["tool_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "Некорректный номер инструмента"}, status=400)
    with db_connect() as c:
        tool = c.execute(
            "SELECT id, name, status, holder_name, floor FROM tools WHERE id = ?", (tid,)
        ).fetchone()
        if not tool:
            return web.json_response({"error": "Инструмент не найден"}, status=404)
        rows = c.execute(
            """SELECT action, user_name, floor, created_at
               FROM journal WHERE tool_id = ? ORDER BY id DESC LIMIT 100""",
            (tid,)
        ).fetchall()
    return web.json_response({
        "tool": {"id": tool[0], "name": tool[1], "status": tool[2],
                 "holder": tool[3], "floor": tool[4]},
        "history": [{"action": r[0], "user": r[1], "floor": r[2], "time": r[3]}
                    for r in rows]
    })


async def api_staff_history(request):
    try: staff_id = int(request.match_info["staff_id"])
    except (TypeError, ValueError): return web.json_response({"error":"Некорректный сотрудник"},status=400)
    requester_id = int(request["tg_user"]["id"])
    # Все зарегистрированные сотрудники могут просматривать карточки коллег.
    # Изменять задачи и выполнять административные действия по-прежнему могут только администраторы.
    with db_connect() as c:
        person=c.execute("SELECT telegram_id,name,username,skill_level FROM users WHERE telegram_id=?",(staff_id,)).fetchone()
        if not person: return web.json_response({"error":"Сотрудник не найден"},status=404)
        rows=c.execute("SELECT tool_name,action,floor,created_at FROM journal WHERE user_id=? ORDER BY id DESC LIMIT 50",(staff_id,)).fetchall()
        assigned=c.execute("SELECT id,name,floor,due_at FROM tools WHERE holder_id=? AND status='issued' ORDER BY name COLLATE NOCASE",(staff_id,)).fetchall()
        tasks=c.execute("SELECT id,title,description,due_date,status,created_at,completed_at FROM staff_tasks WHERE staff_id=? ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,id DESC",(staff_id,)).fetchall()
        attachments=c.execute("SELECT task_id,url,name,created_at FROM task_attachments WHERE task_id IN (SELECT id FROM staff_tasks WHERE staff_id=?) ORDER BY id",(staff_id,)).fetchall()
    attachments_by_task={}
    for a in attachments: attachments_by_task.setdefault(a[0],[]).append({"url":a[1],"name":a[2],"created_at":a[3]})
    return web.json_response({"staff":{"id":person[0],"name":person[1],"username":person[2],"skill_level":person[3] or "beginner"},
        "tools":[{"id":r[0],"name":r[1],"floor":r[2],"due_at":r[3]} for r in assigned],
        "tasks":[{"id":r[0],"title":r[1],"description":r[2],"due_date":r[3],"status":r[4],"created_at":r[5],"completed_at":r[6],"attachments":attachments_by_task.get(r[0],[])} for r in tasks],
        "history":[{"tool":r[0],"action":r[1],"floor":r[2],"time":r[3]} for r in rows]})



async def api_main_task_create(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Создавать главные задачи могут только администраторы"},status=403)
    data=await request.json()
    title=str(data.get("title","")).strip()[:120]
    description=str(data.get("description","")).strip()[:1000]
    due_date=str(data.get("due_date","")).strip()[:10] or None
    if len(title)<2:
        return web.json_response({"error":"Название главной задачи должно быть не короче 2 символов"},status=400)
    if due_date and (len(due_date)!=10 or due_date[4:5]!="-" or due_date[7:8]!="-"):
        return web.json_response({"error":"Проверь срок выполнения"},status=400)
    with db_connect() as c:
        cur=c.execute("INSERT INTO main_tasks (title,description,due_date,created_by,created_at) VALUES (?,?,?,?,?)",(title,description,due_date,uid,now_text()))
        task_id=cur.lastrowid
    return web.json_response({"ok":True,"id":task_id})

async def api_staff_skill(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Менять уровень сотрудников могут только администраторы"},status=403)
    data=await request.json()
    try: staff_id=int(data.get("staff_id"))
    except (TypeError,ValueError): return web.json_response({"error":"Сотрудник не выбран"},status=400)
    level=str(data.get("skill_level","")).strip().lower()
    if level not in {"beginner","intermediate","master"}:
        return web.json_response({"error":"Некорректный уровень профессионализма"},status=400)
    with db_connect() as c:
        changed=c.execute("UPDATE users SET skill_level=? WHERE telegram_id=?",(level,staff_id)).rowcount
    if not changed:
        return web.json_response({"error":"Сотрудник не найден"},status=404)
    return web.json_response({"ok":True})

async def api_task_create(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Назначать задачи могут только администраторы Эмом и Амир"},status=403)
    data=await request.json()
    try: staff_id=int(data.get("staff_id"))
    except (TypeError,ValueError): return web.json_response({"error":"Выбери сотрудника"},status=400)
    title=str(data.get("title","")).strip()[:120]
    description=str(data.get("description","")).strip()[:600]
    due_date=str(data.get("due_date","")).strip()[:10] or None
    if len(title)<2: return web.json_response({"error":"Название задачи должно быть не короче 2 символов"},status=400)
    if due_date and (len(due_date)!=10 or due_date[4:5]!="-" or due_date[7:8]!="-"):
        return web.json_response({"error":"Проверь срок выполнения"},status=400)
    with db_connect() as c:
        person=c.execute("SELECT name FROM users WHERE telegram_id=?",(staff_id,)).fetchone()
        if not person: return web.json_response({"error":"Сотрудник не найден"},status=404)
        main_task_id=data.get("main_task_id")
        if main_task_id not in (None,"",0,"0"):
            try: main_task_id=int(main_task_id)
            except (TypeError,ValueError): return web.json_response({"error":"Главная задача не выбрана"},status=400)
            if not c.execute("SELECT 1 FROM main_tasks WHERE id=?",(main_task_id,)).fetchone():
                return web.json_response({"error":"Главная задача не найдена"},status=404)
        else:
            main_task_id=None
        cur=c.execute("INSERT INTO staff_tasks (staff_id,title,description,due_date,status,created_by,created_at,main_task_id) VALUES (?,?,?,?,'open',?,?,?)",(staff_id,title,description,due_date,uid,now_text(),main_task_id))
        task_id=cur.lastrowid
    return web.json_response({"ok":True,"id":task_id,"staff_name":person[0]})


async def api_task_complete(request):
    uid=int(request["tg_user"]["id"])
    data=await request.json()
    try: task_id=int(data.get("id"))
    except (TypeError,ValueError): return web.json_response({"error":"Задача не выбрана"},status=400)
    completed=bool(data.get("completed",True))
    with db_connect() as c:
        row=c.execute("SELECT staff_id FROM staff_tasks WHERE id=?",(task_id,)).fetchone()
        if not row: return web.json_response({"error":"Задача не найдена"},status=404)
        if int(row[0])!=uid and not is_admin_user(uid):
            return web.json_response({"error":"Можно отмечать только свои задачи"},status=403)
        c.execute("UPDATE staff_tasks SET status=?,completed_at=? WHERE id=?",("done" if completed else "open",now_text() if completed else None,task_id))
    return web.json_response({"ok":True})


async def api_task_upload(request):
    uid=int(request["tg_user"]["id"])
    user=get_user(uid)
    if not user:
        return web.json_response({"error":"Сначала зарегистрируйся в профиле"},status=403)
    try:
        reader=await request.multipart()
        task_part=await reader.next()
        if task_part is None or task_part.name!="task_id":
            return web.json_response({"error":"Задача не выбрана"},status=400)
        try: task_id=int(await task_part.text())
        except (TypeError,ValueError): return web.json_response({"error":"Задача не выбрана"},status=400)
        with db_connect() as c:
            task=c.execute("SELECT staff_id FROM staff_tasks WHERE id=?",(task_id,)).fetchone()
            if not task: return web.json_response({"error":"Задача не найдена"},status=404)
            if int(task[0])!=uid and not is_admin_user(uid):
                return web.json_response({"error":"Можно прикреплять фото только к своим задачам"},status=403)
            count=c.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id=?",(task_id,)).fetchone()[0]
        if count>=5: return web.json_response({"error":"К одной задаче можно прикрепить не больше 5 фотографий"},status=400)
        file_field=await reader.next()
        if file_field is None or file_field.name!="file":
            return web.json_response({"error":"Выбери фотографию"},status=400)
        content_type=(file_field.headers.get("Content-Type") or "").lower()
        allowed={"image/jpeg":".jpg","image/png":".png","image/webp":".webp"}
        if content_type not in allowed:
            return web.json_response({"error":"Поддерживаются JPG, PNG и WEBP"},status=415)
        folder=Path(os.getenv("TASK_UPLOAD_DIR","/data/task_uploads"))
        folder.mkdir(parents=True,exist_ok=True)
        filename=uuid.uuid4().hex+allowed[content_type]
        target=folder/filename
        size=0
        with target.open("wb") as out:
            while True:
                chunk=await file_field.read_chunk(64*1024)
                if not chunk: break
                size+=len(chunk)
                if size>8*1024*1024:
                    out.close(); target.unlink(missing_ok=True)
                    return web.json_response({"error":"Максимальный размер фото — 8 МБ"},status=413)
                out.write(chunk)
        if size==0:
            target.unlink(missing_ok=True)
            return web.json_response({"error":"Фотография пустая"},status=400)
        original=Path(file_field.filename or "Фото").name[:180]
        url="/task-files/"+filename
        with db_connect() as c:
            c.execute("INSERT INTO task_attachments (task_id,staff_id,url,name,created_at) VALUES (?,?,?,?,?)",(task_id,uid,url,original,now_text()))
        return web.json_response({"ok":True,"url":url,"name":original})
    except Exception:
        return web.json_response({"error":"Не удалось загрузить фотографию"},status=400)

async def app_task_file(request):
    filename=request.match_info.get("filename","")
    if not filename or Path(filename).name!=filename or not filename.replace(".","").isalnum():
        raise web.HTTPNotFound()
    path=Path(os.getenv("TASK_UPLOAD_DIR","/data/task_uploads"))/filename
    if not path.is_file(): raise web.HTTPNotFound()
    return web.FileResponse(path,headers={"X-Content-Type-Options":"nosniff","Cache-Control":"public, max-age=31536000"})


async def api_task_delete(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Удалять задачи могут только администраторы Эмом и Амир"},status=403)
    data=await request.json()
    try: task_id=int(data.get("id"))
    except (TypeError,ValueError): return web.json_response({"error":"Задача не выбрана"},status=400)
    with db_connect() as c:
        cur=c.execute("DELETE FROM staff_tasks WHERE id=?",(task_id,))
        if not cur.rowcount: return web.json_response({"error":"Задача не найдена"},status=404)
    return web.json_response({"ok":True})


async def api_inventory_summary(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Только администратор может проводить инвентаризацию"},status=403)
    with db_connect() as c:
        rows=c.execute("SELECT name,COUNT(*),SUM(CASE WHEN status='free' THEN 1 ELSE 0 END) FROM tools GROUP BY name ORDER BY name COLLATE NOCASE").fetchall()
    return web.json_response({"items":[{"name":r[0],"total":r[1],"free":r[2] or 0} for r in rows]})


async def api_inventory_check(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Только администратор может проводить инвентаризацию"},status=403)
    user=get_user(uid); data=await request.json(); entries=data.get("items",[])
    if not isinstance(entries,list) or not entries: return web.json_response({"error":"Нет данных для проверки"},status=400)
    clean=[]
    for entry in entries:
        name=str(entry.get("name","")).strip()[:100]
        try: counted=int(entry.get("counted"))
        except (TypeError,ValueError): return web.json_response({"error":"Укажи целое количество для каждой позиции"},status=400)
        if not name or counted<0 or counted>10000: return web.json_response({"error":"Некорректное количество или название"},status=400)
        clean.append((name,counted))
    with db_connect() as c:
        cur=c.execute("INSERT INTO inventory_sessions (created_by,created_by_name,created_at) VALUES (?,?,?)",(uid,user[0] if user else "Администратор",now_text()))
        sid=cur.lastrowid; results=[]
        for name,counted in clean:
            expected=c.execute("SELECT COUNT(*) FROM tools WHERE name=?",(name,)).fetchone()[0]
            diff=counted-expected
            c.execute("INSERT INTO inventory_items (session_id,tool_name,expected_count,counted_count,difference) VALUES (?,?,?,?,?)",(sid,name,expected,counted,diff))
            results.append({"name":name,"expected":expected,"counted":counted,"difference":diff})
    return web.json_response({"ok":True,"session_id":sid,"items":results})


async def api_transfer_respond(request):
    uid=int(request["tg_user"]["id"]); data=await request.json()
    try: rid=int(data.get("request_id"))
    except (TypeError,ValueError): return web.json_response({"error":"Заявка не выбрана"},status=400)
    decision=data.get("decision")
    if decision not in ("accept","reject"): return web.json_response({"error":"Некорректное решение"},status=400)
    with db_connect() as c:
        tr=c.execute("SELECT tool_id,tool_name,sender_id,sender_name,recipient_name FROM transfer_requests WHERE id=? AND recipient_id=? AND status='pending'",(rid,uid)).fetchone()
        if not tr: return web.json_response({"error":"Заявка уже обработана или недоступна"},status=409)
        tid,name,sender_id,sender_name,recipient_name=tr
        if decision=="accept":
            changed=c.execute("UPDATE tools SET holder_id=?,holder_name=? WHERE id=? AND holder_id=? AND status='issued'",(uid,recipient_name,tid,sender_id)).rowcount
            if not changed:
                c.execute("UPDATE transfer_requests SET status='cancelled',responded_at=? WHERE id=?",(now_text(),rid))
                return web.json_response({"error":"Инструмент уже не закреплён за отправителем"},status=409)
            floor=c.execute("SELECT floor FROM tools WHERE id=?",(tid,)).fetchone()[0]
            c.execute("INSERT INTO journal (tool_id,tool_name,action,user_id,user_name,floor,created_at) VALUES (?,?,?,?,?,?,?)",(tid,name,f"Передача подтверждена: {sender_name} → {recipient_name}",uid,recipient_name,floor,now_text()))
            status="accepted"
        else: status="rejected"
        c.execute("UPDATE transfer_requests SET status=?,responded_at=? WHERE id=?",(status,now_text(),rid))
    if decision=="accept":
        created=now_text()
        notice=f"🔄 Передача инструмента подтверждена\nОт: {sender_name}\nКому: {recipient_name}\nИнструмент: {name} · №{tid}"
        with db_connect() as c:
            c.execute("INSERT INTO chat_messages (user_id,user_name,message,created_at) VALUES (?,?,?,?)",(uid,"Система · ToolSite",notice,created))
            recipients=[row[0] for row in c.execute("SELECT telegram_id FROM users WHERE telegram_id>0").fetchall()]
            groups=[row[0] for row in c.execute("SELECT chat_id FROM chat_notification_groups").fetchall()]
        bot=request.app["bot"]
        for recipient_id in recipients:
            try: await bot.send_message(recipient_id,"📣 "+notice)
            except Exception as exc: print(f"Transfer notification to {recipient_id} failed: {exc}")
        for group_id in groups:
            try: await bot.send_message(group_id,"📣 "+notice)
            except Exception as exc: print(f"Transfer group notification to {group_id} failed: {exc}")
    else:
        try: await request.app["bot"].send_message(sender_id,f"🔔 {recipient_name} отклонил передачу инструмента №{tid} — {name}.")
        except Exception: pass
    return web.json_response({"ok":True,"status":status})


async def api_register(request):
    uid=int(request["tg_user"]["id"]); d=await request.json(); name=str(d.get("name","")).strip()[:60]
    if len(name)<2: return web.json_response({"error":"Укажи имя и фамилию"},status=400)
    with db_connect() as c:
        user=c.execute("SELECT name FROM users WHERE telegram_id=?",(uid,)).fetchone()
        if not user:
            return web.json_response({"error":"Вход доступен только по персональной ссылке-приглашению администратора"},status=403)
        if user[0] != name:
            return web.json_response({"error":"Менять имя может только администрация. Обратись к администратору."},status=403)
    return web.json_response({"ok":True})

async def api_staff_rename(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Менять имена сотрудников могут только администраторы"},status=403)
    data=await request.json()
    try: staff_id=int(data.get("staff_id"))
    except (TypeError,ValueError): return web.json_response({"error":"Сотрудник не выбран"},status=400)
    name=" ".join(str(data.get("name","")).strip().split())[:60]
    if len(name)<2: return web.json_response({"error":"Укажи имя и фамилию"},status=400)
    with db_connect() as c:
        exists=c.execute("SELECT 1 FROM users WHERE telegram_id=?",(staff_id,)).fetchone()
        if not exists: return web.json_response({"error":"Сотрудник не найден"},status=404)
        c.execute("UPDATE users SET name=? WHERE telegram_id=?",(name,staff_id))
    return web.json_response({"ok":True,"name":name})

async def api_chat_delete(request):
    uid=int(request["tg_user"]["id"])
    data=await request.json()
    try: message_id=int(data.get("message_id"))
    except (TypeError,ValueError): return web.json_response({"error":"Сообщение не выбрано"},status=400)
    with db_connect() as c:
        row=c.execute("SELECT user_id,attachment_url FROM chat_messages WHERE id=?",(message_id,)).fetchone()
        if not row: return web.json_response({"error":"Сообщение уже удалено или не найдено"},status=404)
        owner_id, attachment_url=row
        if owner_id!=uid and not is_admin_user(uid):
            return web.json_response({"error":"Можно удалять только свои сообщения"},status=403)
        c.execute("DELETE FROM chat_messages WHERE id=?",(message_id,))
    if attachment_url and attachment_url.startswith("/chat-files/"):
        filename=attachment_url[len("/chat-files/"):]
        if filename and Path(filename).name==filename:
            try: (Path(os.getenv("CHAT_UPLOAD_DIR","/data/chat_uploads"))/filename).unlink(missing_ok=True)
            except OSError as exc: print(f"Chat attachment cleanup failed: {exc}")
    return web.json_response({"ok":True,"deleted_id":message_id})

async def api_add_consumable(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Добавлять расходники могут только администраторы Эмом и Амир"},status=403)
    data=await request.json()
    name=str(data.get("name","")).strip()[:100]
    unit=str(data.get("unit","шт.")).strip()[:20] or "шт."
    try:
        quantity=int(data.get("quantity",0)); minimum=int(data.get("min_quantity",0))
    except (TypeError,ValueError):
        return web.json_response({"error":"Количество и минимальный остаток должны быть целыми числами"},status=400)
    if len(name)<2 or quantity<0 or minimum<0 or quantity>100000 or minimum>100000:
        return web.json_response({"error":"Проверь название и количество расходника"},status=400)
    with db_connect() as c:
        cur=c.execute("INSERT INTO consumables (name,unit,quantity,min_quantity,updated_at) VALUES (?,?,?,?,?)",(name,unit,quantity,minimum,now_text()))
        item_id=cur.lastrowid
    return web.json_response({"ok":True,"id":item_id})


async def api_update_consumable(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Изменять остатки могут только администраторы Эмом и Амир"},status=403)
    data=await request.json()
    try:
        item_id=int(data.get("id")); quantity=int(data.get("quantity")); minimum=int(data.get("min_quantity"))
    except (TypeError,ValueError):
        return web.json_response({"error":"Укажи корректные количества"},status=400)
    if quantity<0 or minimum<0 or quantity>100000 or minimum>100000:
        return web.json_response({"error":"Количество не может быть отрицательным или больше 100000"},status=400)
    with db_connect() as c:
        changed=c.execute("UPDATE consumables SET quantity=?,min_quantity=?,updated_at=? WHERE id=?",(quantity,minimum,now_text(),item_id)).rowcount
    if not changed: return web.json_response({"error":"Расходник не найден"},status=404)
    return web.json_response({"ok":True})


async def api_request_consumable(request):
    uid=int(request["tg_user"]["id"]); user=get_user(uid)
    if not user: return web.json_response({"error":"Сначала зарегистрируйся в профиле"},status=403)
    data=await request.json()
    try: item_id=int(data.get("id")); quantity=int(data.get("quantity"))
    except (TypeError,ValueError): return web.json_response({"error":"Укажи количество для заказа"},status=400)
    note=str(data.get("note","")).strip()[:300]
    if quantity<1 or quantity>100000: return web.json_response({"error":"Количество для заказа должно быть от 1 до 100000"},status=400)
    with db_connect() as c:
        item=c.execute("SELECT name FROM consumables WHERE id=?",(item_id,)).fetchone()
        if not item: return web.json_response({"error":"Расходник не найден"},status=404)
        cur=c.execute("INSERT INTO consumable_requests (consumable_id,consumable_name,quantity,note,requested_by,requested_by_name,status,created_at) VALUES (?,?,?,?,?,?,'pending',?)",(item_id,item[0],quantity,note,uid,user[0],now_text()))
        request_id=cur.lastrowid
        admins=c.execute("SELECT telegram_id FROM users WHERE telegram_id>0 AND lower(name) IN ('эмом','амир')").fetchall()
    for (admin_id,) in admins:
        try:
            await request.app["bot"].send_message(admin_id,f"📦 Новая заявка на расходники\nОт: {user[0]}\nПозиция: {item[0]}\nКоличество: {quantity}\n{('Комментарий: '+note) if note else ''}\nОткрой ToolSite → Расходники.")
        except Exception:
            pass
    return web.json_response({"ok":True,"request_id":request_id})


async def api_respond_consumable_request(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Обрабатывать заявки могут только администраторы Эмом и Амир"},status=403)
    data=await request.json()
    try: request_id=int(data.get("request_id"))
    except (TypeError,ValueError): return web.json_response({"error":"Заявка не выбрана"},status=400)
    status=str(data.get("status",""))
    if status not in ("ordered","rejected"): return web.json_response({"error":"Некорректный статус заявки"},status=400)
    with db_connect() as c:
        row=c.execute("SELECT requested_by,consumable_name,quantity FROM consumable_requests WHERE id=? AND status='pending'",(request_id,)).fetchone()
        if not row: return web.json_response({"error":"Заявка уже обработана или не найдена"},status=409)
        c.execute("UPDATE consumable_requests SET status=?,responded_at=? WHERE id=?",(status,now_text(),request_id))
    try:
        label="отмечена как заказанная" if status=="ordered" else "отклонена"
        await request.app["bot"].send_message(row[0],f"📦 Твоя заявка: {row[1]} · {row[2]} — {label}.")
    except Exception:
        pass
    return web.json_response({"ok":True,"status":status})


async def api_add_tool(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Добавлять инструменты могут только администраторы Эмом и Амир"},status=403)
    if not get_user(uid): return web.json_response({"error":"Сначала укажи имя в профиле"},status=400)
    d=await request.json(); name=str(d.get("name","")).strip()[:100]
    if len(name)<2: return web.json_response({"error":"Название слишком короткое"},status=400)
    with db_connect() as c: cur=c.execute("INSERT INTO tools (name,created_at) VALUES (?,?)",(name,now_text())); tid=cur.lastrowid
    return web.json_response({"ok":True,"id":tid})

async def api_delete_tool(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid):
        return web.json_response({"error":"Удалять инструменты могут только администраторы Эмом и Амир"},status=403)
    d=await request.json()
    try: tid=int(d.get("tool_id"))
    except: return web.json_response({"error":"Инструмент не выбран"},status=400)
    with db_connect() as c:
        tool=c.execute("SELECT name,status,holder_name,floor FROM tools WHERE id=?",(tid,)).fetchone()
        if not tool: return web.json_response({"error":"Инструмент не найден"},status=404)
        if tool[1]=="issued": return web.json_response({"error":"Нельзя удалить выданный инструмент. Сначала верните его."},status=409)
        c.execute("DELETE FROM tools WHERE id=?",(tid,))
    return web.json_response({"ok":True,"name":tool[0]})

async def api_issue(request):
    uid=int(request["tg_user"]["id"])
    if not is_admin_user(uid): return web.json_response({"error":"Выдавать инструменты могут только администраторы Эмом и Амир"},status=403)
    user=get_user(uid); d=await request.json()
    if not user: return web.json_response({"error":"Сначала укажи имя в профиле"},status=400)
    try: tid=int(d.get("tool_id"))
    except: return web.json_response({"error":"Выбери инструмент"},status=400)
    floor=str(d.get("floor","")).strip()[:40]
    if not floor: return web.json_response({"error":"Укажи этаж или зону"},status=400)
    due_date=str(d.get("due_date","")).strip(); due_at=None
    if due_date:
        try: due_at=datetime.fromisoformat(due_date).replace(hour=23,minute=59,second=0).astimezone().isoformat(timespec="seconds")
        except ValueError: return web.json_response({"error":"Некорректная дата возврата"},status=400)
    with db_connect() as c:
        tool=c.execute("SELECT name FROM tools WHERE id=? AND status='free'",(tid,)).fetchone()
        if not tool: return web.json_response({"error":"Инструмент уже выдан или не найден"},status=409)
        changed=c.execute("UPDATE tools SET status='issued',holder_id=?,holder_name=?,floor=?,due_at=?,reminder_sent_at=NULL WHERE id=? AND status='free'",(uid,user[0],floor,due_at,tid)).rowcount
        if changed: c.execute("INSERT INTO journal (tool_id,tool_name,action,user_id,user_name,floor,created_at) VALUES (?,?,'Выдача',?,?,?,?)",(tid,tool[0],uid,user[0],floor,now_text()))
    return web.json_response({"ok":bool(changed)})

async def api_return(request):
    uid=int(request["tg_user"]["id"]); d=await request.json()
    try: tid=int(d.get("tool_id"))
    except: return web.json_response({"error":"Инструмент не выбран"},status=400)
    user=get_user(uid)
    if not is_admin_user(uid): return web.json_response({"error":"Сотрудники могут только передавать закреплённые инструменты. Возврат на склад выполняет администратор."},status=403)
    with db_connect() as c:
        tool=c.execute("SELECT name,floor FROM tools WHERE id=? AND holder_id=? AND status='issued'",(tid,uid)).fetchone()
        if not tool: return web.json_response({"error":"Инструмент не закреплён за тобой"},status=409)
        c.execute("UPDATE tools SET status='free',holder_id=NULL,holder_name=NULL,floor=NULL,due_at=NULL,reminder_sent_at=NULL WHERE id=?",(tid,))
        c.execute("INSERT INTO journal (tool_id,tool_name,action,user_id,user_name,floor,created_at) VALUES (?,?,'Возврат',?,?,?,?)",(tid,tool[0],uid,user[0] if user else "Сотрудник",tool[1],now_text()))
    return web.json_response({"ok":True})

async def api_transfer(request):
    uid=int(request["tg_user"]["id"]); user=get_user(uid); d=await request.json()
    try: tid,rid=int(d.get("tool_id")),int(d.get("recipient_id"))
    except: return web.json_response({"error":"Выбери сотрудника"},status=400)
    with db_connect() as c:
        tool=c.execute("SELECT name,floor FROM tools WHERE id=? AND holder_id=? AND status='issued'",(tid,uid)).fetchone()
        recipient=c.execute("SELECT name FROM users WHERE telegram_id=?",(rid,)).fetchone()
        if not tool or not recipient or not user: return web.json_response({"error":"Передача недоступна"},status=409)
        if rid<=0: return web.json_response({"error":"Получатель ещё не привязал Telegram. Попроси его зарегистрироваться."},status=400)
        if rid==uid: return web.json_response({"error":"Нельзя передать инструмент самому себе"},status=400)
        if c.execute("SELECT 1 FROM transfer_requests WHERE tool_id=? AND status='pending'",(tid,)).fetchone():
            return web.json_response({"error":"Для инструмента уже ожидается подтверждение передачи"},status=409)
        cur=c.execute("INSERT INTO transfer_requests (tool_id,tool_name,sender_id,sender_name,recipient_id,recipient_name,status,created_at) VALUES (?,?,?,?,?,?,'pending',?)",(tid,tool[0],uid,user[0],rid,recipient[0],now_text()))
        request_id=cur.lastrowid
    try: await request.app["bot"].send_message(rid,f"🔔 {user[0]} предлагает принять инструмент №{tid} — {tool[0]}. Открой ToolSite → Сотрудники → Входящие передачи и подтверди или отклони.")
    except Exception: pass
    return web.json_response({"ok":True,"request_id":request_id,"status":"pending"})

async def run_web_server(bot):
    app=web.Application(middlewares=[api_auth], client_max_size=9 * 1024 * 1024); app["bot"]=bot
    app.router.add_get("/",app_index); app.router.add_get("/chat-files/{filename}",app_chat_file); app.router.add_get("/task-files/{filename}",app_task_file); app.router.add_get("/map.png",app_map_image); app.router.add_get("/api/state",api_state); app.router.add_get("/api/chat",api_chat_messages); app.router.add_get("/api/tools/{tool_id}/history",api_tool_history); app.router.add_get("/api/staff/{staff_id}/history",api_staff_history); app.router.add_post("/api/main-tasks/create",api_main_task_create); app.router.add_post("/api/staff/skill",api_staff_skill); app.router.add_post("/api/tasks/create",api_task_create); app.router.add_post("/api/tasks/complete",api_task_complete); app.router.add_post("/api/tasks/upload",api_task_upload); app.router.add_post("/api/tasks/delete",api_task_delete); app.router.add_get("/api/inventory",api_inventory_summary)
    app.router.add_post("/api/consumables",api_add_consumable); app.router.add_post("/api/consumables/update",api_update_consumable); app.router.add_post("/api/consumables/request",api_request_consumable); app.router.add_post("/api/consumables/respond",api_respond_consumable_request)
    app.router.add_post("/api/register",api_register); app.router.add_post("/api/staff/rename",api_staff_rename); app.router.add_post("/api/chat/delete",api_chat_delete); app.router.add_post("/api/chat/upload",api_chat_upload); app.router.add_post("/api/chat",api_chat_send); app.router.add_post("/api/tools",api_add_tool); app.router.add_post("/api/delete-tool",api_delete_tool)
    app.router.add_post("/api/site-notes",api_site_note_add); app.router.add_post("/api/site-notes/resolve",api_site_note_resolve); app.router.add_post("/api/issue",api_issue); app.router.add_post("/api/return",api_return); app.router.add_post("/api/transfer",api_transfer); app.router.add_post("/api/transfer/respond",api_transfer_respond); app.router.add_post("/api/inventory/check",api_inventory_check)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",int(os.getenv("PORT","8080"))).start()
    return runner



async def reminder_loop(bot):
    while True:
        try:
            now=datetime.now().astimezone()
            with db_connect() as c:
                rows=c.execute("SELECT id,name,holder_id,holder_name,due_at FROM tools WHERE status='issued' AND due_at IS NOT NULL AND due_at<=? AND (reminder_sent_at IS NULL OR reminder_sent_at<?)",(now.isoformat(timespec="seconds"),now.date().isoformat())).fetchall()
                for tid,_,_,_,_ in rows: c.execute("UPDATE tools SET reminder_sent_at=? WHERE id=?",(now.isoformat(timespec="seconds"),tid))
                admins=c.execute("SELECT telegram_id FROM users WHERE telegram_id>0 AND lower(name) IN ('эмом','амир')").fetchall()
            for tid,name,holder_id,holder_name,due_at in rows:
                try: await bot.send_message(holder_id,f"⏰ Срок возврата истёк: №{tid} — {name}. Верни инструмент или свяжись с администратором.")
                except Exception: pass
                for (admin_id,) in admins:
                    try: await bot.send_message(admin_id,f"⚠️ Просрочен возврат: №{tid} — {name}. Ответственный: {holder_name}. Срок: {due_at}.")
                    except Exception: pass
        except Exception as exc: print(f"Reminder loop error: {exc}")
        await asyncio.sleep(3600)


async def daily_task_report_loop(bot):
    tz = ZoneInfo("Europe/Moscow")
    while True:
        try:
            now = datetime.now(tz)
            report_date = now.date().isoformat()
            if now.hour >= 17:
                with db_connect() as c:
                    users = c.execute("SELECT telegram_id,name FROM users WHERE telegram_id>0 ORDER BY name COLLATE NOCASE").fetchall()
                    task_rows = c.execute("""SELECT t.id,t.staff_id,t.title,t.description,t.status,t.created_at,t.completed_at
                                             FROM staff_tasks t JOIN users u ON u.telegram_id=t.staff_id
                                             WHERE t.status='open' OR t.completed_at IS NOT NULL OR t.created_at IS NOT NULL
                                             ORDER BY u.name COLLATE NOCASE,t.id""").fetchall()
                    attachment_rows = c.execute("""SELECT a.task_id,a.url,a.name FROM task_attachments a
                                                   JOIN staff_tasks t ON t.id=a.task_id
                                                   ORDER BY a.id""").fetchall()
                    groups = c.execute("SELECT chat_id FROM chat_notification_groups").fetchall()
                    admins = c.execute("SELECT telegram_id FROM users WHERE telegram_id>0 AND lower(name) IN ('эмом','амир')").fetchall()
                    delivered = {int(row[0]) for row in c.execute("SELECT chat_id FROM daily_task_report_deliveries WHERE report_date=?",(report_date,)).fetchall()}
                by_staff = {int(uid): [] for uid,_ in users}
                for row in task_rows:
                    tid, sid, title, description, status, created_at, completed_at = row
                    sid=int(sid)
                    if sid not in by_staff: continue
                    def is_today(value):
                        if not value: return False
                        try: return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(tz).date().isoformat()==report_date
                        except Exception: return str(value)[:10]==report_date
                    if status=="open" or is_today(completed_at) or is_today(created_at):
                        by_staff[sid].append({"id":int(tid),"title":title,"description":description,"status":status,"completed_at":completed_at})
                photos_by_task={}
                for tid,url,name in attachment_rows:
                    photos_by_task.setdefault(int(tid),[]).append((url,name))
                lines=["<b>📋 ЕЖЕДНЕВНЫЙ ОТЧЁТ ToolSite</b>",f"📅 {now.strftime('%d.%m.%Y')}  ·  Итоги на 17:00", ""]
                for uid,name in users:
                    tasks=by_staff.get(int(uid),[])
                    done=sum(1 for t in tasks if t["status"]=="done")
                    lines.append(f"<b>👷 {html.escape(name)}</b>  <code>{done}/{len(tasks)}</code>")
                    if not tasks:
                        lines.append("   — Задач нет")
                    for task in tasks:
                        icon="✅" if task["status"]=="done" else "🟡"
                        status_text="Выполнена" if task["status"]=="done" else "В работе / не завершена"
                        lines.append(f"   {icon} {html.escape(task['title'])} — {status_text}")
                        if task["description"]:
                            lines.append(f"      <i>{html.escape(task['description'][:180])}</i>")
                    lines.append("")
                total=sum(len(v) for v in by_staff.values())
                completed=sum(1 for v in by_staff.values() for t in v if t["status"]=="done")
                lines.append(f"<b>ИТОГО:</b> {completed} из {total} задач выполнено.")
                report_chunks=[]
                current=[]
                for line in lines:
                    candidate="\n".join(current+[line])
                    if len(candidate)>3800 and current:
                        report_chunks.append("\n".join(current))
                        current=[line]
                    else:
                        current.append(line)
                if current: report_chunks.append("\n".join(current))
                recipients={int(x[0]) for x in groups}
                recipients.update(int(x[0]) for x in admins)
                for chat_id in sorted(recipients-delivered):
                    try:
                        for report_chunk in report_chunks:
                            await bot.send_message(chat_id,report_chunk,parse_mode="HTML",disable_web_page_preview=True)
                        for uid,name in users:
                            for task in by_staff.get(int(uid),[]):
                                attachments=photos_by_task.get(task["id"],[])
                                if not attachments: continue
                                media=[]
                                for url,filename in attachments[:5]:
                                    safe_name=Path(str(url).split("?",1)[0]).name
                                    if not safe_name or safe_name in {".",".."}: continue
                                    path=Path(os.getenv("TASK_UPLOAD_DIR","/data/task_uploads"))/safe_name
                                    if path.is_file():
                                        caption=f"{name} · {task['title']}" if not media else None
                                        media.append(InputMediaPhoto(media=FSInputFile(str(path)),caption=caption))
                                if len(media)==1:
                                    await bot.send_photo(chat_id,media[0].media,caption=media[0].caption)
                                elif media:
                                    await bot.send_media_group(chat_id,media)
                        with db_connect() as c:
                            c.execute("INSERT OR REPLACE INTO daily_task_report_deliveries (report_date,chat_id,sent_at) VALUES (?,?,?)",(report_date,chat_id,now.isoformat(timespec="seconds")))
                    except Exception as exc:
                        print(f"Daily task report delivery failed for {chat_id}: {exc}")
            await asyncio.sleep(30)
        except Exception as exc:
            print(f"Daily task report loop error: {exc}")
            await asyncio.sleep(30)



async def lunch_reminder_loop(bot):
    """Send the daily 10-minute lunch reminder at 12:50 Moscow time."""
    tz = ZoneInfo("Europe/Moscow")
    while True:
        try:
            now = datetime.now(tz)
            today = now.date().isoformat()
            if now.hour == 12 and now.minute == 50:
                with db_connect() as c:
                    already_sent = c.execute(
                        "SELECT 1 FROM lunch_reminder_deliveries WHERE reminder_date=?",
                        (today,)
                    ).fetchone()
                    if not already_sent:
                        recipients = c.execute(
                            "SELECT telegram_id FROM users WHERE telegram_id>0"
                        ).fetchall()
                        c.execute(
                            "INSERT OR IGNORE INTO lunch_reminder_deliveries (reminder_date,sent_at) VALUES (?,?)",
                            (today, now.isoformat(timespec="seconds"))
                        )
                    else:
                        recipients = []
                for (telegram_id,) in recipients:
                    try:
                        await bot.send_message(
                            telegram_id,
                            "🍽️ Через 10 минут обед! Завершайте текущие дела и готовьтесь к перерыву."
                        )
                    except Exception as exc:
                        print(f"Lunch reminder failed for {telegram_id}: {exc}")
        except Exception as exc:
            print(f"Lunch reminder loop error: {exc}")
        await asyncio.sleep(20)


async def location_request_loop(bot):
    tz = ZoneInfo("Europe/Moscow")
    while True:
        try:
            now = datetime.now(tz)
            today = now.date().isoformat()
            with db_connect() as c:
                sent = c.execute("SELECT sent_at,followup_sent FROM location_daily_requests WHERE request_date=?", (today,)).fetchone()
                users = c.execute("SELECT telegram_id,name FROM users WHERE telegram_id>0 ORDER BY name").fetchall()
                if now.hour == 9 and not sent:
                    c.execute("INSERT OR IGNORE INTO location_daily_requests (request_date,sent_at,followup_sent) VALUES (?,?,0)", (today,now.isoformat(timespec="seconds")))
                    targets = list(users); followup = False
                elif now.hour == 9 and now.minute >= 15 and sent and not sent[1]:
                    responded = {row[0] for row in c.execute("SELECT telegram_id FROM staff_locations WHERE report_date=?", (today,)).fetchall()}
                    targets = [row for row in users if row[0] not in responded]
                    c.execute("UPDATE location_daily_requests SET followup_sent=1 WHERE request_date=?", (today,)); followup = True
                else:
                    targets = []; followup = False
            for telegram_id,name in targets:
                try:
                    text = ("🔔 Напоминание: трансляция геопозиции ещё не включена. Открой 📎 → Геопозиция → «Транслировать геопозицию» и выбери время." if followup else "Доброе утро! 📍 Чтобы сотрудники видели друг друга на карте ToolSite в реальном времени, отправь трансляцию геопозиции: нажми 📎 → Геопозиция → «Транслировать геопозицию» → выбери время. Обычная отправка геопозиции даёт только одну точку.")
                    await bot.send_message(telegram_id,text)
                except Exception as exc: print(f"Location request failed for {telegram_id} ({name}): {exc}")
        except Exception as exc: print(f"Location request loop error: {exc}")
        await asyncio.sleep(30)
async def chat_cleanup_loop():
    """Remove general-chat messages and their uploaded files after 36 hours."""
    while True:
        try:
            cutoff = datetime.now().astimezone() - timedelta(hours=36)
            folder = Path(os.getenv("CHAT_UPLOAD_DIR", "/data/chat_uploads"))
            with db_connect() as c:
                rows = c.execute("SELECT id,attachment_url,created_at FROM chat_messages").fetchall()
                expired = []
                for message_id, attachment_url, created_at in rows:
                    try:
                        created = datetime.fromisoformat(created_at)
                        if created.tzinfo is None:
                            created = created.astimezone()
                        if created < cutoff:
                            expired.append((message_id, attachment_url))
                    except (TypeError, ValueError):
                        continue
                for message_id, attachment_url in expired:
                    c.execute("DELETE FROM chat_messages WHERE id=?", (message_id,))
                    if attachment_url and attachment_url.startswith("/chat-files/"):
                        filename = attachment_url[len("/chat-files/"):]
                        if filename and Path(filename).name == filename:
                            try:
                                (folder / filename).unlink(missing_ok=True)
                            except OSError as exc:
                                print(f"Expired chat file cleanup failed: {exc}")
            if expired:
                print(f"Chat cleanup removed {len(expired)} messages older than 36 hours")
        except Exception as exc:
            print(f"Chat cleanup loop error: {exc}")
        await asyncio.sleep(300)


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная BOT_TOKEN в настройках Railway Variables")
    with db_connect():
        pass
    bot = Bot(token=token)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="ToolSite", web_app=WebAppInfo(url=WEBAPP_URL)))
    runner = await run_web_server(bot)
    reminder_task = asyncio.create_task(reminder_loop(bot))
    location_task = asyncio.create_task(location_request_loop(bot))
    lunch_reminder_task = asyncio.create_task(lunch_reminder_loop(bot))
    task_report_task = asyncio.create_task(daily_task_report_loop(bot))
    chat_cleanup_task = asyncio.create_task(chat_cleanup_loop())
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        location_task.cancel()
        lunch_reminder_task.cancel()
        task_report_task.cancel()
        chat_cleanup_task.cancel()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
