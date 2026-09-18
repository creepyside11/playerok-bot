from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.crypto import SecretCipher
from app.db import init_db, make_engine, make_session_factory
from app.handlers import Services, configure_handlers
from app.playerok import EmailAuthClient, PlayerokGateway
from app.worker import WorkerManager


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_factory = make_session_factory(engine)

    cipher = SecretCipher(settings.bot_token)
    gateway = PlayerokGateway(cipher)
    email_auth = EmailAuthClient()

    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(
        configure_handlers(Services(session_factory, gateway, cipher, email_auth))
    )

    worker = WorkerManager(
        bot=bot,
        session_factory=session_factory,
        gateway=gateway,
        cipher=cipher,
        poll_interval=settings.poll_interval,
    )
    worker_task = asyncio.create_task(worker.run(), name="playerok-worker")

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        worker.stop()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
