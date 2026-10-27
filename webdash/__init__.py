"""
webdash — Modular Web Dashboard package for Recluse Bot.
"""

from __future__ import annotations

import logging
import os
from typing import Any

__all__ = ["is_webdash_enabled", "get_ready_db"]

logger = logging.getLogger("recluse.webdash")

_DB_NOT_READY_WARNED = False


def is_webdash_enabled() -> bool:
    """Return True only when WEBDASH_ENABLED is explicitly enabled."""
    val = os.getenv("WEBDASH_ENABLED", "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def get_ready_db(bot: Any) -> Any | None:
    """
    Return the Motor database handle only when the bot reports db_ready=True
    and bot.db is non-None. Logs one clear warning if bot.db_ready is False.
    """
    global _DB_NOT_READY_WARNED
    if bot is None:
        return None
    if not getattr(bot, "db_ready", False):
        if not _DB_NOT_READY_WARNED:
            _DB_NOT_READY_WARNED = True
            logger.warning("webdash: bot.db_ready is False, sessions disabled")
        return None
    return getattr(bot, "db", None)

