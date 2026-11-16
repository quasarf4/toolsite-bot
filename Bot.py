import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

dp = Dispatcher()


def main_menu():
    keyboard = InlineKeyboardBuilder()

    keyboard.button(text="🔧 Инструменты", callback_data="tools")
    keyboard.button(text="📍 Карта этажей", callback_data="map")
    keyboard.button(text="📋 Мои инструменты", callback_data="my_tools")
    keyboard.button(text="📖 Журнал", callback_data="journal")
    keyboard.button(text="👷 Пользователи", callback_data="users")

    keyboard.adjust(2, 1, 2)
    return keyboard.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🏗 Добро пожаловать в ToolSite!\n\n"
        "Система учёта строительных инструментов.\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data.in_({
    "tools", "map", "my_tools", "journal", "users"
}))
async def menu_click(callback: CallbackQuery):
    messages = {
        "tools": "🔧 Раздел инструментов.\nЗдесь будет список оборудования.",
        "map": "📍 Карта объекта.\nЗдесь будут этажи и расположение инструментов.",
        "my_tools": "📋 Здесь будут инструменты, закреплённые за тобой.",
        "journal": "📖 Здесь будет история выдачи и возврата.",
        "users": "👷 Здесь будет список сотрудников."
    }

    await callback.answer()
    await callback.message.answer(
        messages[callback.data],
        reply_markup=main_menu()
    )


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задана переменная BOT_TOKEN в настройках Railway Variables"
        )

    bot = Bot(token=token)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
