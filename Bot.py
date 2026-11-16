import asyncio
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

dp = Dispatcher()
pending_add = set()

DB_PATH = os.getenv("DB_PATH", "/data/toolsite.db")


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
    keyboard.button(text="⬅️ Главное меню", callback_data="home")
    keyboard.adjust(1)
    return keyboard.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    pending_add.discard(message.from_user.id)
    await message.answer(
        "🏗 Добро пожаловать в ToolSite!\n\n"
        "Система учёта строительных инструментов.\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_menu()
    )


@dp.message(Command("cancel"))
async def cancel_add(message: Message):
    pending_add.discard(message.from_user.id)
    await message.answer("Добавление отменено.", reply_markup=main_menu())


@dp.message(F.text)
async def receive_tool_name(message: Message):
    user_id = message.from_user.id
    if user_id not in pending_add:
        return

    name = message.text.strip()
    if not name or name.startswith("/"):
        await message.answer("Напиши название инструмента обычным текстом или отправь /cancel для отмены.")
        return
    if len(name) > 100:
        await message.answer("Название слишком длинное. Максимум 100 символов. Попробуй ещё раз.")
        return

    with db_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO tools (name, created_at) VALUES (?, ?)",
            (name, datetime.now().astimezone().isoformat(timespec="seconds"))
        )
        tool_id = cursor.lastrowid

    pending_add.discard(user_id)
    await message.answer(
        f"✅ Инструмент добавлен!\n\n№{tool_id}: {name}\nСтатус: Свободен",
        reply_markup=tools_menu()
    )


@dp.callback_query(F.data == "home")
async def go_home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "tools")
async def open_tools(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    await callback.message.answer(
        f"🔧 Раздел инструментов\nВсего в базе: {total}",
        reply_markup=tools_menu()
    )


@dp.callback_query(F.data == "tool_add")
async def add_tool_start(callback: CallbackQuery):
    pending_add.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        "➕ Напиши название инструмента.\nНапример: Перфоратор Bosch GBH 2-26.\n\n"
        "Для отмены отправь /cancel."
    )


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
        lines = ["📋 **Список инструментов:**", ""]
        for tool_id, name, status, holder_name, floor in rows:
            if status == "free":
                state = "🟢 Свободен"
            else:
                state = f"🔴 Выдан: {holder_name or 'сотрудник не указан'}"
            if floor:
                state += f" · этаж: {floor}"
            lines.append(f"№{tool_id} — {name}\n{state}")
        text = "\n\n".join(lines)

    await callback.message.answer(text, reply_markup=tools_menu(), parse_mode="Markdown")


@dp.callback_query(F.data == "my_tools")
async def my_tools(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, name, floor FROM tools WHERE holder_id = ? ORDER BY id",
            (callback.from_user.id,)
        ).fetchall()
    if rows:
        lines = [f"№{tool_id} — {name}" + (f" · этаж: {floor}" if floor else "")
                 for tool_id, name, floor in rows]
        text = "📋 Твои инструменты:\n\n" + "\n".join(lines)
    else:
        text = "📋 Сейчас за тобой не закреплено ни одного инструмента."
    await callback.message.answer(text, reply_markup=main_menu())


@dp.callback_query(F.data.in_({"map", "journal", "users"}))
async def other_sections(callback: CallbackQuery):
    messages = {
        "map": "📍 Карта объекта и этажи — следующий этап разработки.",
        "journal": "📖 Журнал выдачи и возврата подключим вместе с функцией выдачи.",
        "users": "👷 Регистрацию сотрудников добавим следующим этапом."
    }
    await callback.answer()
    await callback.message.answer(messages[callback.data], reply_markup=main_menu())


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная BOT_TOKEN в настройках Railway Variables")

    # Проверяем доступность базы и создаём таблицу при запуске.
    with db_connect():
        pass

    bot = Bot(token=token)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
