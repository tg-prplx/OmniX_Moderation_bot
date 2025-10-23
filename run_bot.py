#!/usr/bin/env python3
"""
Entry point for running the Spisdil moderation bot with logging enabled.

Usage:
    python run_bot.py

Environment:
    - SPISDIL_TELEGRAM_TOKEN
    - SPISDIL_OPENAI__API_KEY

The script loads configuration via BotSettings (reads .env by default) and starts
the TelegramModerationApp with graceful shutdown on Ctrl+C.
"""

import asyncio

from spisdil_moder_bot import TelegramModerationApp
from spisdil_moder_bot.config import BotSettings


async def _main() -> None:
    settings = BotSettings()
    app = TelegramModerationApp(settings)
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n\n🛑 Bot shutdown requested by user.")
