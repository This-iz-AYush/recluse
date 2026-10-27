"""
webdash/app.py — Application factory for the Recluse modular web dashboard.
Sets up aiohttp-jinja2 with autoescape=True, bounded caches, SessionManager,
AuditLogger, security middleware, and Piece 2 routes.
"""

from __future__ import annotations

import pathlib
from typing import Any, Awaitable, Callable

import aiohttp_jinja2
import jinja2
from aiohttp import web

from webdash.audit import AuditLogger
from webdash.auth import SESSION_COOKIE_NAME, SessionManager
from webdash.cache import GuildConfigCache, ShortTTLCache
from webdash.router import (
    AUDIT_LOGGER_KEY,
    BOT_KEY,
    GUILD_CONFIG_CACHE_KEY,
    MEMBER_CACHE_KEY,
    OAUTH_CLIENT_KEY,
    OWNER_ID_CACHE_KEY,
    RATE_LIMITER_KEY,
    SESSION_MANAGER_KEY,
    register_routes,
)
from webdash.security import InMemoryRateLimiter, create_security_middleware

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent / "templates"


def create_webdash_app(
    bot: Any,
    *,
    rate_limiter: InMemoryRateLimiter | None = None,
    session_manager: SessionManager | None = None,
    guild_config_cache: GuildConfigCache | None = None,
    member_cache: ShortTTLCache | None = None,
    owner_id_cache: ShortTTLCache | None = None,
    audit_logger: AuditLogger | None = None,
    oauth_client: Callable[[str], Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]] | None = None,
    trusted_hops: int | None = None,
    origin_secret: str | None = None,
    require_origin_secret: bool | None = None,
    enable_debug_ip: bool | None = None,
) -> web.Application:
    """
    Construct and configure the aiohttp.web.Application for webdash.
    """
    limiter = rate_limiter if rate_limiter is not None else InMemoryRateLimiter()
    sessions = session_manager if session_manager is not None else SessionManager(bot)

    existing_cfg_cache = getattr(bot, "guild_config", None) if bot is not None else None
    if guild_config_cache is not None:
        cfg_cache = guild_config_cache
    elif isinstance(existing_cfg_cache, GuildConfigCache):
        cfg_cache = existing_cfg_cache
    else:
        cfg_cache = GuildConfigCache(bot)

    if bot is not None and getattr(bot, "guild_config", None) is None:
        try:
            bot.guild_config = cfg_cache
        except Exception:
            pass

    mem_cache = (
        member_cache
        if member_cache is not None
        else ShortTTLCache(max_size=1000, ttl_seconds=60.0)
    )
    own_cache = (
        owner_id_cache
        if owner_id_cache is not None
        else ShortTTLCache(max_size=10, ttl_seconds=600.0)
    )
    auditor = audit_logger if audit_logger is not None else AuditLogger(bot)

    async def _load_session(request: web.Request) -> dict[str, Any] | None:
        raw_sid = request.cookies.get(SESSION_COOKIE_NAME)
        return await sessions.get_session(raw_sid)

    security_mw = create_security_middleware(
        limiter,
        session_loader=_load_session,
        trusted_hops=trusted_hops,
        origin_secret=origin_secret,
        require_origin_secret=require_origin_secret,
    )

    app = web.Application(middlewares=[security_mw])

    app[BOT_KEY] = bot
    app[SESSION_MANAGER_KEY] = sessions
    app[GUILD_CONFIG_CACHE_KEY] = cfg_cache
    app[MEMBER_CACHE_KEY] = mem_cache
    app[OWNER_ID_CACHE_KEY] = own_cache
    app[AUDIT_LOGGER_KEY] = auditor
    app[RATE_LIMITER_KEY] = limiter
    if oauth_client is not None:
        app[OAUTH_CLIENT_KEY] = oauth_client

    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(
            enabled_extensions=("html", "htm", "xml"),
            default_for_string=True,
            default=True,
        ),
    )

    register_routes(app, enable_debug_ip=enable_debug_ip)
    return app
