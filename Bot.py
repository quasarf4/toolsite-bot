import asyncio
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

dp = Dispatcher()
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
    conn.commit()
    return conn


def main_menu():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔧 Инструменты", callback_data="tools")
    keyboard.button(text="📍 Карта этажей", callback_data="map")
    keyboard.button(text="📋 Мои инструменты", callback_data="my_tools")
    keyboard.button(text="📖 Журнал", callback_data="journal")
    keyboard.button(text="👷 Пользователи", callback_data="users")
    keyboard.adjust(2, 1, 2)
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


@dp.message(CommandStart())
async def start(message: Message):
    pending.pop(message.from_user.id, None)
    user = get_user(message.from_user.id)
    greeting = "🏗 TOOLSITE\n━━━━━━━━━━━━━━━━━━\nУЧЁТ ИНСТРУМЕНТОВ"
    if user:
        greeting += f"\n\n👤 Сотрудник: {user[0]}"
    else:
        greeting += "\n\n👤 Ты ещё не зарегистрирован.\nОткрой «Сотрудники», чтобы начать."
    await message.answer(greeting + "\n\nВыбери раздел:", reply_markup=main_menu())


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


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная BOT_TOKEN в настройках Railway Variables")
    with db_connect():
        pass
    bot = Bot(token=token)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
