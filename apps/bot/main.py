from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from pathlib import Path

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties

from apps.bot.handlers.admin import router as admin_router
from apps.bot.handlers.user import router as user_router
from apps.bot.middlewares.database import DatabaseSessionMiddleware
from apps.bot.middlewares.error_handler import GlobalErrorMiddleware
from apps.bot.premium_bot import PremiumEmojiBot
from core.config import settings
from core.database import dispose_database


HEARTBEAT_PATH = Path(os.environ.get("BOT_HEARTBEAT_FILE", "/tmp/bot_heartbeat"))
HEARTBEAT_INTERVAL_S = 15


async def _heartbeat_loop() -> None:
    """Touch the heartbeat file periodically.

    The container healthcheck reads this file and reports unhealthy if it
    hasn't been updated recently — a real liveness signal instead of the
    previous always-true probe.
    """
    while True:
        try:
            HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_PATH.write_text(str(time.time()))
        except OSError as exc:
            logging.getLogger(__name__).warning("heartbeat write failed: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def on_startup(bot: PremiumEmojiBot) -> None:
    me = await bot.get_me()
    logging.getLogger(__name__).info("Bot started: id=%s username=@%s", me.id, me.username)
    # Prime the button-style cache so the first keyboard render after
    # boot already has the operator's color preferences.
    try:
        from apps.bot.utils.button_style import (
            install_global_button_coloring,
            prime_button_style_cache,
            prime_premium_icon_cache,
        )
        await prime_button_style_cache()
        # Load the premium-emoji → button-icon map (for icon_custom_emoji_id).
        await prime_premium_icon_cache()
        # Color EVERY inline button by default (not just the few that opt in
        # via styled_button), and apply premium icons. Explicit colors/icons win.
        install_global_button_coloring()
    except Exception as exc:
        logging.getLogger(__name__).warning("button style cache prime failed: %s", exc)
    try:
        from services.text_templates import prime_text_template_cache
        await prime_text_template_cache()
    except Exception as exc:
        logging.getLogger(__name__).warning("text-template cache prime failed: %s", exc)
    # Keep this process's settings caches in sync with changes made in the
    # dashboard / worker (Redis pub/sub) — no more "restart the bot to apply".
    try:
        from core.cache_sync import register_default_caches, run_cache_invalidation_listener
        register_default_caches()
        asyncio.create_task(run_cache_invalidation_listener(), name="cache-sync")
    except Exception as exc:
        logging.getLogger(__name__).warning("cache-sync listener start failed: %s", exc)


async def on_shutdown(bot: PremiumEmojiBot) -> None:
    await bot.session.close()
    await dispose_database()


async def main() -> None:
    configure_logging()

    from core.observability import init_sentry
    init_sentry("bot")

    # Write a heartbeat synchronously BEFORE starting the dispatcher so the
    # container healthcheck has something to read during the slow first
    # bring-up (DB pool warmup, initial Telegram polling handshake).
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(str(time.time()))
    except OSError as exc:
        logging.getLogger(__name__).warning("bootstrap heartbeat write failed: %s", exc)

    bot = PremiumEmojiBot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
    )
    dispatcher = Dispatcher()
    dispatcher.update.middleware(DatabaseSessionMiddleware())
    dispatcher.message.middleware(GlobalErrorMiddleware())
    dispatcher.callback_query.middleware(GlobalErrorMiddleware())
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)
    # Force join check needs to be at top level (no admin/user middleware)
    from apps.bot.handlers.admin.settings import _force_join_check_router
    dispatcher.include_router(_force_join_check_router)
    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="bot-heartbeat")
    try:
        await dispatcher.start_polling(
            bot,
            drop_pending_updates=settings.bot_drop_pending_updates,
        )
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
