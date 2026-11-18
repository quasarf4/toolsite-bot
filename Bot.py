import asyncio
import os
import sqlite3
import hmac, hashlib, json
import uuid
import secrets
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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
    # Предварительный список сотрудников без Telegram-привязки.
    # Отрицательные ID зарезервированы для записей-черновиков и не являются Telegram ID.
    preset_staff = [
        "Мансур", "Амир", "Ваня", "Эмом", "Хусейн", "Дилшод",
        "Мирали", "Мирзоали", "Машраб", "Бахтиёр", "Хочи", "Хасан",
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO users (telegram_id, name, created_at) VALUES (?, ?, ?)",
        [(-10001 - i, name, now_text()) for i, name in enumerate(preset_staff)]
    )

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
    user = get_user(telegram_id)
    return bool(user and is_admin_name(user[0]))


@dp.message(Command("invites"), F.chat.type == "private")
async def send_staff_invites(message: Message):
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ Создавать приглашения могут только администраторы Эмом и Амир.")
        return
    with db_connect() as c:
        placeholders = c.execute("SELECT telegram_id,name FROM users WHERE telegram_id<0 ORDER BY name COLLATE NOCASE").fetchall()
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

@dp.message(Command("chat_notify"), F.chat.type.in_({"group", "supergroup"}))
async def register_chat_notification_group(message: Message):
    with db_connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO chat_notification_groups (chat_id,title,added_at) VALUES (?,?,?)",
            (message.chat.id, message.chat.title or "Группа команды", now_text())
        )
    await message.answer("✅ Эта группа подключена к уведомлениям ToolSite. Теперь новые сообщения из общего чата будут приходить сюда.")

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
            if claimed_by and int(claimed_by) != uid:
                await message.answer("❌ Это приглашение уже использовано другим Telegram-аккаунтом. Обратись к администратору.")
                return
            if current:
                if target and current[0] != target[0]:
                    await message.answer("❌ Этот Telegram уже закреплён за другим сотрудником. Обратись к администратору.")
                    return
                if not target and current[0] != c.execute("SELECT name FROM users WHERE telegram_id=?", (claimed_by,)).fetchone()[0]:
                    await message.answer("❌ Не удалось подтвердить привязку. Обратись к администратору.")
                    return
            else:
                if not target:
                    await message.answer("❌ Запись сотрудника уже привязана или удалена. Обратись к администратору.")
                    return
                duplicate = c.execute("SELECT telegram_id FROM users WHERE name=? AND telegram_id>0", (target[0],)).fetchone()
                if duplicate:
                    await message.answer("❌ Для этого сотрудника уже есть Telegram-аккаунт. Обратись к администратору.")
                    return
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
async def receive_staff_location(message: Message):
    uid = message.from_user.id
    user = get_user(uid)
    if not user:
        await message.answer("Сначала зарегистрируйся в ToolSite, чтобы привязать геопозицию к своему имени.")
        return
    loc = message.location
    local_now = datetime.now(ZoneInfo("Europe/Moscow"))
    with db_connect() as c:
        c.execute("""INSERT INTO staff_locations (telegram_id,latitude,longitude,accuracy,updated_at,report_date) VALUES (?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude,accuracy=excluded.accuracy,updated_at=excluded.updated_at,report_date=excluded.report_date""",(uid,loc.latitude,loc.longitude,loc.horizontal_accuracy,local_now.isoformat(timespec="seconds"),local_now.date().isoformat()))
    await message.answer("✅ Геопозиция получена и сохранена для карты ToolSite. Это разовая отметка, а не непрерывное отслеживание.", reply_markup=ReplyKeyboardRemove())
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
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash: return None
        check = "\n".join(f"{k}={v}" for k,v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received_hash): return None
        if abs(datetime.now(timezone.utc).timestamp()-int(pairs.get("auth_date","0"))) > 86400: return None
        user = json.loads(pairs.get("user","{}"))
        return user if user.get("id") else None
    except Exception:
        return None

@web.middleware
async def api_auth(request, handler):
    if request.path.startswith("/api/"):
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data",""), os.getenv("BOT_TOKEN",""))
        if not user: return web.json_response({"error":"Открой приложение через Telegram"},status=401)
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
    tg=request["tg_user"]; uid=int(tg["id"])
    with db_connect() as c:
        user=c.execute("SELECT name FROM users WHERE telegram_id=?",(uid,)).fetchone()
        tools=c.execute("SELECT id,name,status,holder_id,holder_name,floor,due_at FROM tools ORDER BY name COLLATE NOCASE").fetchall()
        staff=c.execute("SELECT telegram_id,name FROM users ORDER BY name COLLATE NOCASE").fetchall()
        journal=c.execute("SELECT tool_name,action,user_name,floor,created_at FROM journal ORDER BY id DESC LIMIT 50").fetchall()
        incoming=c.execute("SELECT id,tool_id,tool_name,sender_id,sender_name,created_at FROM transfer_requests WHERE recipient_id=? AND status='pending' ORDER BY id DESC",(uid,)).fetchall()
        consumables=c.execute("SELECT id,name,unit,quantity,min_quantity,updated_at FROM consumables ORDER BY name COLLATE NOCASE").fetchall()
        if user and is_admin_name(user[0]):
            consumable_requests=c.execute("SELECT id,consumable_id,consumable_name,quantity,note,requested_by_name,status,created_at FROM consumable_requests WHERE status='pending' ORDER BY id DESC LIMIT 100").fetchall()
        else:
            consumable_requests=c.execute("SELECT id,consumable_id,consumable_name,quantity,note,requested_by_name,status,created_at FROM consumable_requests WHERE requested_by=? ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
        locations=c.execute("SELECT l.telegram_id,u.name,l.latitude,l.longitude,l.accuracy,l.updated_at FROM staff_locations l JOIN users u ON u.telegram_id=l.telegram_id ORDER BY l.updated_at DESC").fetchall() if user and is_admin_name(user[0]) else []
        site_notes=c.execute("SELECT id,area,note,created_by_name,status,created_at,resolved_by,resolved_at FROM site_notes ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,id DESC LIMIT 100").fetchall()
    total=len(tools); free=sum(1 for t in tools if t[2]=="free")
    return web.json_response({"profile":{"id":uid,"name":user[0] if user else ""},"is_admin":bool(user and is_admin_name(user[0])),"stats":{"total":total,"free":free,"issued":total-free,"staff":len(staff)},"tools":[{"id":t[0],"name":t[1],"status":t[2],"holder_id":t[3],"holder":t[4],"floor":t[5],"due_at":t[6]} for t in tools],"staff":[{"id":p[0],"name":p[1]} for p in staff],"journal":[{"tool":j[0],"action":j[1],"user":j[2],"floor":j[3],"time":j[4]} for j in journal],"my_tools":[t[0] for t in tools if t[3]==uid],"incoming_transfers":[{"id":r[0],"tool_id":r[1],"tool":r[2],"sender_id":r[3],"sender":r[4],"created_at":r[5]} for r in incoming],"consumables":[{"id":r[0],"name":r[1],"unit":r[2],"quantity":r[3],"min_quantity":r[4],"updated_at":r[5]} for r in consumables],"consumable_requests":[{"id":r[0],"consumable_id":r[1],"name":r[2],"quantity":r[3],"note":r[4],"requested_by":r[5],"status":r[6],"created_at":r[7]} for r in consumable_requests], "locations":[{"staff_id":r[0],"name":r[1],"latitude":r[2],"longitude":r[3],"accuracy":r[4],"updated_at":r[5]} for r in locations],"site_notes":[{"id":r[0],"area":r[1],"note":r[2],"created_by":r[3],"status":r[4],"created_at":r[5],"resolved_by":r[6],"resolved_at":r[7]} for r in site_notes]})

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
    body = message or "Отправлено вложение"
    if attachment_name:
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
        content_type = (field.headers.get("Content-Type") or "application/octet-stream").lower()
        allowed = {"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","application/pdf":".pdf","text/plain":".txt","application/msword":".doc","application/vnd.openxmlformats-officedocument.wordprocessingml.document":".docx","application/vnd.ms-excel":".xls","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":".xlsx"}
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
    with db_connect() as c:
        person=c.execute("SELECT telegram_id,name FROM users WHERE telegram_id=?",(staff_id,)).fetchone()
        if not person: return web.json_response({"error":"Сотрудник не найден"},status=404)
        rows=c.execute("SELECT tool_name,action,floor,created_at FROM journal WHERE user_id=? ORDER BY id DESC LIMIT 50",(staff_id,)).fetchall()
        assigned=c.execute("SELECT id,name,floor,due_at FROM tools WHERE holder_id=? AND status='issued' ORDER BY name COLLATE NOCASE",(staff_id,)).fetchall()
    return web.json_response({"staff":{"id":person[0],"name":person[1]},
        "tools":[{"id":r[0],"name":r[1],"floor":r[2],"due_at":r[3]} for r in assigned],
        "history":[{"tool":r[0],"action":r[1],"floor":r[2],"time":r[3]} for r in rows]})


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
        if user[0] != name and not is_admin_user(uid):
            return web.json_response({"error":"Имя закреплено за приглашением. Для изменения обратись к администратору."},status=403)
        if user[0] != name:
            c.execute("UPDATE users SET name=? WHERE telegram_id=?",(name,uid))
    return web.json_response({"ok":True})

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
    app.router.add_get("/",app_index); app.router.add_get("/chat-files/{filename}",app_chat_file); app.router.add_get("/map.png",app_map_image); app.router.add_get("/api/state",api_state); app.router.add_get("/api/chat",api_chat_messages); app.router.add_get("/api/tools/{tool_id}/history",api_tool_history); app.router.add_get("/api/staff/{staff_id}/history",api_staff_history); app.router.add_get("/api/inventory",api_inventory_summary)
    app.router.add_post("/api/consumables",api_add_consumable); app.router.add_post("/api/consumables/update",api_update_consumable); app.router.add_post("/api/consumables/request",api_request_consumable); app.router.add_post("/api/consumables/respond",api_respond_consumable_request)
    app.router.add_post("/api/register",api_register); app.router.add_post("/api/chat/upload",api_chat_upload); app.router.add_post("/api/chat",api_chat_send); app.router.add_post("/api/tools",api_add_tool); app.router.add_post("/api/delete-tool",api_delete_tool)
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


async def location_request_loop(bot):
    tz = ZoneInfo("Europe/Moscow")
    keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
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
                    text = ("🔔 Напоминание: геопозиция на сегодня ещё не получена. Нажми кнопку ниже и отправь своё местоположение на объекте." if followup else "Доброе утро! 📍 Отправь геопозицию, чтобы отметить своё местоположение на объекте. Нажми кнопку ниже и подтверди отправку.")
                    await bot.send_message(telegram_id,text,reply_markup=keyboard)
                except Exception as exc: print(f"Location request failed for {telegram_id} ({name}): {exc}")
        except Exception as exc: print(f"Location request loop error: {exc}")
        await asyncio.sleep(30)
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
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        location_task.cancel()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
