"""
Spisdil Moderation Bot core package.

Exposes high-level orchestration utilities required to wire the moderation
pipeline into an aiogram bot while keeping individual layers and integrations
modular and reusable.
"""

from .services.moderation_service import ModerationCoordinator
from .services.telegram_bot import TelegramModerationApp, telegram_app

__all__ = ["ModerationCoordinator", "TelegramModerationApp", "telegram_app"]
