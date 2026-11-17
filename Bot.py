import asyncio
import os
import sqlite3
import hmac, hashlib, json
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
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


@dp.message(CommandStart())
async def start(message: Message):
    pending.pop(message.from_user.id, None)
    # Telegram requires text for inline keyboards; use an invisible separator.
    await message.answer("\u2063", reply_markup=main_menu())

@dp.message(Command("cancel"))
async def cancel_action(message: Message):
    pending.pop(message.from_user.id, None)
    await message.answer("Действие отменено.", reply_markup=main_menu())


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
        tool_id = int(tool_id_text)
        recipient_id = int(recipient_id_text)
    except (ValueError, IndexError):
        await callback.message.answer("Не удалось обработать передачу. Попробуй ещё раз.")
        return

    sender = get_user(callback.from_user.id)
    if not sender:
        await callback.message.answer("Сначала зарегистрируйся в разделе «Пользователи».")
        return

    with db_connect() as conn:
        tool = conn.execute(
            "SELECT name, floor FROM tools WHERE id = ? AND holder_id = ? AND status = 'issued'",
            (tool_id, callback.from_user.id)
        ).fetchone()
        recipient = conn.execute(
            "SELECT name FROM users WHERE telegram_id = ?",
            (recipient_id,)
        ).fetchone()

        if not tool or not recipient:
            await callback.message.answer("Передача не выполнена: инструмент или сотрудник уже недоступен.")
            return

        changed = conn.execute(
            """UPDATE tools SET holder_id = ?, holder_name = ?
               WHERE id = ? AND holder_id = ? AND status = 'issued'""",
            (recipient_id, recipient[0], tool_id, callback.from_user.id)
        ).rowcount

        if changed:
            conn.execute(
                """INSERT INTO journal
                   (tool_id, tool_name, action, user_id, user_name, floor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool_id, tool[0],
                    f"Передача: {sender[0]} → {recipient[0]}",
                    callback.from_user.id, sender[0], tool[1], now_text()
                )
            )

    if changed:
        await callback.message.answer(
            f"✅ Инструмент передан!\n№{tool_id}: {tool[0]}\nОт: {sender[0]}\nКому: {recipient[0]}\nЭтаж: {tool[1] or 'не указан'}",
            reply_markup=main_menu()
        )
        try:
            await callback.bot.send_message(
                recipient_id,
                f"🔔 Тебе передали инструмент: №{tool_id} — {tool[0]}.\nПередал: {sender[0]}.\nЭтаж: {tool[1] or 'не указан'}."
            )
        except Exception:
            pass
    else:
        await callback.message.answer("Передача не выполнена: инструмент уже мог быть передан другому сотруднику.")


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
    # Telegram WebView can keep an old HTML/JS snapshot after a redeploy.
    # Always fetch the current Mini App so button handlers are not stale.
    return web.FileResponse("webapp/index.html", headers={
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
        tools=c.execute("SELECT id,name,status,holder_id,holder_name,floor FROM tools ORDER BY name COLLATE NOCASE").fetchall()
        staff=c.execute("SELECT telegram_id,name FROM users ORDER BY name COLLATE NOCASE").fetchall()
        journal=c.execute("SELECT tool_name,action,user_name,floor,created_at FROM journal ORDER BY id DESC LIMIT 30").fetchall()
    total=len(tools); free=sum(1 for t in tools if t[2]=="free")
    return web.json_response({"profile":{"id":uid,"name":user[0] if user else ""},"is_admin":bool(user and is_admin_name(user[0])),"stats":{"total":total,"free":free,"issued":total-free,"staff":len(staff)},"tools":[{"id":t[0],"name":t[1],"status":t[2],"holder_id":t[3],"holder":t[4],"floor":t[5]} for t in tools],"staff":[{"id":p[0],"name":p[1]} for p in staff],"journal":[{"tool":j[0],"action":j[1],"user":j[2],"floor":j[3],"time":j[4]} for j in journal],"my_tools":[t[0] for t in tools if t[3]==uid]})

async def api_register(request):
    uid=int(request["tg_user"]["id"]); d=await request.json(); name=str(d.get("name","")).strip()[:60]
    if len(name)<2: return web.json_response({"error":"Укажи имя и фамилию"},status=400)
    with db_connect() as c: c.execute("INSERT INTO users (telegram_id,name,created_at) VALUES (?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name",(uid,name,now_text()))
    return web.json_response({"ok":True})

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
    uid=int(request["tg_user"]["id"]); user=get_user(uid); d=await request.json()
    if not user: return web.json_response({"error":"Сначала укажи имя в профиле"},status=400)
    try: tid=int(d.get("tool_id"))
    except: return web.json_response({"error":"Выбери инструмент"},status=400)
    floor=str(d.get("floor","")).strip()[:40]
    if not floor: return web.json_response({"error":"Укажи этаж или зону"},status=400)
    with db_connect() as c:
        tool=c.execute("SELECT name FROM tools WHERE id=? AND status='free'",(tid,)).fetchone()
        if not tool: return web.json_response({"error":"Инструмент уже выдан или не найден"},status=409)
        changed=c.execute("UPDATE tools SET status='issued',holder_id=?,holder_name=?,floor=? WHERE id=? AND status='free'",(uid,user[0],floor,tid)).rowcount
        if changed: c.execute("INSERT INTO journal (tool_id,tool_name,action,user_id,user_name,floor,created_at) VALUES (?,?,'Выдача',?,?,?,?)",(tid,tool[0],uid,user[0],floor,now_text()))
    return web.json_response({"ok":bool(changed)})

async def api_return(request):
    uid=int(request["tg_user"]["id"]); d=await request.json()
    try: tid=int(d.get("tool_id"))
    except: return web.json_response({"error":"Инструмент не выбран"},status=400)
    user=get_user(uid)
    with db_connect() as c:
        tool=c.execute("SELECT name,floor FROM tools WHERE id=? AND holder_id=? AND status='issued'",(tid,uid)).fetchone()
        if not tool: return web.json_response({"error":"Инструмент не закреплён за тобой"},status=409)
        c.execute("UPDATE tools SET status='free',holder_id=NULL,holder_name=NULL,floor=NULL WHERE id=?",(tid,))
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
        changed=c.execute("UPDATE tools SET holder_id=?,holder_name=? WHERE id=? AND holder_id=? AND status='issued'",(rid,recipient[0],tid,uid)).rowcount
        if changed: c.execute("INSERT INTO journal (tool_id,tool_name,action,user_id,user_name,floor,created_at) VALUES (?,?,?,?,?,?,?)",(tid,tool[0],f"Передача: {user[0]} → {recipient[0]}",uid,user[0],tool[1],now_text()))
    if changed:
        try: await request.app["bot"].send_message(rid,f"🔔 Тебе передали {tool[0]} (№{tid}). Этаж: {tool[1] or 'не указан'}. Передал: {user[0]}.")
        except Exception: pass
    return web.json_response({"ok":bool(changed)})

async def run_web_server(bot):
    app=web.Application(middlewares=[api_auth]); app["bot"]=bot
    app.router.add_get("/",app_index); app.router.add_get("/map.png",app_map_image); app.router.add_get("/api/state",api_state)
    app.router.add_post("/api/register",api_register); app.router.add_post("/api/tools",api_add_tool); app.router.add_post("/api/delete-tool",api_delete_tool)
    app.router.add_post("/api/issue",api_issue); app.router.add_post("/api/return",api_return); app.router.add_post("/api/transfer",api_transfer)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",int(os.getenv("PORT","8080"))).start()
    return runner



async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная BOT_TOKEN в настройках Railway Variables")
    with db_connect():
        pass
    bot = Bot(token=token)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="ToolSite", web_app=WebAppInfo(url=WEBAPP_URL)))
    runner = await run_web_server(bot)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
