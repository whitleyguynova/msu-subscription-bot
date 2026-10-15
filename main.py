import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

import config
import db
import scheduler
from handlers import admin, user


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.init(config.DB_PATH)
    if not config.CHANNEL_ID:  # канал, подключённый пересылкой поста
        config.CHANNEL_ID = int(await db.get_setting("channel_id", 0) or 0)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)  # админка первой: её команды и реплаи важнее
    dp.include_router(user.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="materials", description="Материалы по предметам"),
        BotCommand(command="sub", description="Моя подписка"),
    ])
    for aid in config.ADMIN_IDS:
        try:
            await bot.set_my_commands([
                BotCommand(command="admin", description="Все команды админа"),
                BotCommand(command="channel", description="Проверить канал"),
                BotCommand(command="stats", description="Статистика"),
                BotCommand(command="upload", description="Загрузить файлы"),
                BotCommand(command="subjects", description="Предметы"),
                BotCommand(command="export", description="Выгрузка в Excel"),
            ], scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            logging.warning("can't set admin commands for %s (not started the bot yet?)", aid)

    if config.CHANNEL_ID:
        problems = await admin.channel_problems(bot, config.CHANNEL_ID)
        for aid in config.ADMIN_IDS:
            if problems:
                try:
                    await bot.send_message(aid, "⚠️ Проблема с каналом: " + "; ".join(problems))
                except Exception:
                    pass
        if problems:
            logging.warning("channel problems: %s", problems)

    asyncio.create_task(scheduler.run(bot))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
