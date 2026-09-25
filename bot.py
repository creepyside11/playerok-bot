from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.crypto import SecretCipher
from app.db import init_db, make_engine, make_session_factory
from app.advanced_handlers import configure_advanced
from app.emerald_promo_ui import router as emerald_promo_router
from app.items_manager_ui import router as items_manager_router
from app.handlers import Services, configure_handlers
from app.playerok import EmailAuthClient, PlayerokGateway
from app.plugin_system import PluginManager
from app.telegram_plugin_runtime import ExternalAPI
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

    plugin_manager = PluginManager(session_factory)
    plugin_manager.load()

    bot = Bot(settings.bot_token)
    http_client = httpx.AsyncClient(timeout=30)
    plugin_manager.set_external_api(ExternalAPI(http_client))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(configure_advanced(plugin_manager))
    dispatcher.include_router(emerald_promo_router)
    dispatcher.include_router(items_manager_router)
    dispatcher.include_router(
        configure_handlers(Services(session_factory, gateway, cipher, email_auth))
    )

    worker = WorkerManager(
        bot=bot,
        session_factory=session_factory,
        gateway=gateway,
        cipher=cipher,
        poll_interval=settings.poll_interval,
        plugin_manager=plugin_manager,
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
        await http_client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
