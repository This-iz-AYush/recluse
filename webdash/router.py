"""
webdash/router.py — Phase A1 Checkpoint 3A route handlers:
- GET /health (no DB call; JSON with NaN/inf-safe latency_ms; ?strict=1 -> 503 when not ready)
- GET /debug/ip (Bot Owner only; registered only when WEBDASH_DEBUG_IP=1)
- GET / (landing page when logged out; server picker when logged in)
- GET /servers (server picker; redirects to /login when logged out)
- GET /login (friendly 503 if bot.db_ready is False; OAuth2 state cookie + redirect to Discord)
- GET /callback (verify_oauth_state, session rotation revoking any old cookie, new session in db.webdash_sessions)
- POST /logout (revokes session server-side and clears recluse_sid cookie; GET /logout removed -> 405)
- GET /manage/{guild_id} (guild overview evaluated via permissions.evaluate_permission with live member roles)
- Checkpoint 3A 501 placeholders for unported guild/owner pages
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime
import json
import logging
import math
import os
import re
import urllib.parse
from typing import Any, Awaitable, Callable

import aiohttp
import aiohttp_jinja2
from aiohttp import web

from webdash import get_ready_db
from webdash.audit import AuditLogger, compute_diff
from webdash.auth import (
    DEFAULT_SESSION_TTL_SECONDS,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SessionManager,
    generate_oauth_state,
    verify_oauth_state,
)
from webdash.cache import GuildConfigCache, IpBanCache, ShortTTLCache
from webdash.permissions import (
    DISCORD_PERM_ADMINISTRATOR,
    DISCORD_PERM_MANAGE_GUILD,
    evaluate_permission,
    resolve_user_permissions,
)
from webdash.security import (
    CLIENT_IP_KEY,
    CLIENT_IP_SOURCE_KEY,
    CLIENT_IP_TRUSTED_KEY,
    SESSION_DATA_KEY,
    InMemoryRateLimiter,
    is_secure_request,
    normalize_ip,
)

logger = logging.getLogger("recluse.webdash.router")

# Typed application state keys
_AppKey = getattr(web, "AppKey", lambda name, _t=None: name)
BOT_KEY = _AppKey("bot", Any)
SESSION_MANAGER_KEY = _AppKey("session_manager", SessionManager)
GUILD_CONFIG_CACHE_KEY = _AppKey("guild_config_cache", GuildConfigCache)
MEMBER_CACHE_KEY = _AppKey("member_cache", ShortTTLCache)
OWNER_ID_CACHE_KEY = _AppKey("owner_id_cache", ShortTTLCache)
AUDIT_LOGGER_KEY = _AppKey("audit_logger", AuditLogger)
RATE_LIMITER_KEY = _AppKey("rate_limiter", InMemoryRateLimiter)
IP_BAN_CACHE_KEY = _AppKey("ip_ban_cache", IpBanCache)
OAUTH_CLIENT_KEY = _AppKey("oauth_client", Any)

_MISSING_MEMBER_SENTINEL = object()

DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"

MAX_BANNED_WORDS_DISPLAY = 500
MAX_BANNED_WORDS_ENTRIES = 500
MAX_BANNED_WORD_LENGTH = 64
MAX_DISABLED_COGS_ENTRIES = 100
MAX_COMMAND_SETTINGS_ENTRIES = 200
MAX_COMMAND_LIST_ITEMS = 100
VALID_COMMAND_SETTING_KEYS: tuple[str, ...] = (
    "disabled_subcommands",
    "allowed_channels",
    "ignored_channels",
    "allowed_roles",
    "ignored_roles",
)
MAX_JSON_BODY_BYTES = 65536
MAX_SECURITY_LOG_CONTENT_CHARS = 300
LOGS_QUERY_LIMIT = 50
LOGS_DB_TIMEOUT_SECONDS = 2.0
OWNER_VISIT_LOGS_LIMIT = 30
OWNER_RELOAD_TIMEOUT_SECONDS = 10.0
OWNER_RELOAD_COG_RE = re.compile(r"^[a-z0-9_]{1,32}$")
DISALLOWED_WEB_RELOAD_COGS: frozenset[str] = frozenset({"dashboard", "database"})

# Protected system cogs excluded from normal disabling in /modules/{guild_id}
# (cogs/dashboard.py:620 excludes dashboard, jishaku, database, owner; core added per 3B spec).
# If a guild already has one of these in disabled_cogs (exact case), it is still rendered with a
# "Protected (Re-enable Only)" badge so an admin can switch it back on.
PROTECTED_COGS_LOWER: frozenset[str] = frozenset(
    {"core", "database", "dashboard", "owner", "jishaku"}
)
PROTECTED_COG_CANONICAL_NAMES: frozenset[str] = frozenset(
    {"Core", "Database", "Dashboard", "Owner", "Jishaku"}
)

# Verified AI models in cogs/ai.py:77-91, 291-299, 401-411, 657, 743-754
AI_MODEL_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "value": "auto",
        "title": "Auto (Smart Routing)",
        "subtitle": "Smart routing across engines",
        "icon": "fa-solid fa-wand-magic-sparkles",
        "icon_color": "text-blue-400",
    },
    {
        "value": "deepseek",
        "title": "DeepSeek V4.1 Flash",
        "subtitle": "Smartest reasoning engine",
        "icon": "fa-solid fa-brain",
        "icon_color": "text-indigo-400",
    },
    {
        "value": "groq",
        "title": "Groq (GPT OSS 120B)",
        "subtitle": "Fast & uncensored responses",
        "icon": "fa-solid fa-bolt",
        "icon_color": "text-amber-400",
    },
    {
        "value": "gemini",
        "title": "Gemini 2.5 Flash",
        "subtitle": "Multimodal & vision support",
        "icon": "fa-brands fa-google",
        "icon_color": "text-emerald-400",
    },
)
VALID_AI_MODELS: frozenset[str] = frozenset(m["value"] for m in AI_MODEL_OPTIONS)


@dataclasses.dataclass
class GuildAccessContext:
    guild: Any | None
    allowed: bool
    status_code: int
    reason: str
    user_id: int
    bot_owner_id: int | None = None
    is_bot_owner: bool = False
    is_guild_owner: bool = False
    is_discord_admin: bool = False
    has_manage_guild: bool = False
    member_role_ids: list[int] = dataclasses.field(default_factory=list)
    granted_permissions: set[str] = dataclasses.field(default_factory=set)
    settings: dict[str, Any] = dataclasses.field(default_factory=dict)
    settings_doc: dict[str, Any] | None = None
    settings_read_failed: bool = False


def get_bot_avatar_url(bot: Any) -> str:
    if bot is not None and getattr(bot, "user", None) is not None:
        display_avatar = getattr(bot.user, "display_avatar", None)
        url = getattr(display_avatar, "url", None)
        if url:
            return str(url).replace(".webp", ".png")
    return DEFAULT_AVATAR_URL


def get_user_avatar_url(session_doc: dict[str, Any]) -> str:
    discord_id = session_doc.get("discord_id", "")
    avatar_hash = session_doc.get("avatar")
    username = str(session_doc.get("username") or "User")
    if discord_id and avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
    encoded_name = urllib.parse.quote(username)
    return f"https://ui-avatars.com/api/?name={encoded_name}&background=18181b&color=fff"


def get_guild_icon_url(guild_id: int | str, guild_name: str, icon_or_url: str | None) -> str:
    if icon_or_url:
        val = str(icon_or_url)
        if val.startswith("http://") or val.startswith("https://"):
            return val
        return f"https://cdn.discordapp.com/icons/{guild_id}/{val}.png"
    encoded_name = urllib.parse.quote(guild_name or "Server")
    return f"https://ui-avatars.com/api/?name={encoded_name}&background=18181b&color=fff"


def build_invite_url() -> str:
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    return (
        f"https://discord.com/oauth2/authorize?client_id={urllib.parse.quote(client_id)}"
        "&permissions=8&scope=bot%20applications.commands"
    )


async def resolve_bot_owner_id(request: web.Request) -> int | None:
    """Resolve the Bot Owner's Discord ID using bot.owner_id, TTL cache, or application_info()."""
    bot = request.app.get(BOT_KEY)
    if bot is not None:
        direct_owner = getattr(bot, "owner_id", None)
        if isinstance(direct_owner, int) and direct_owner > 0:
            return direct_owner

    owner_cache: ShortTTLCache | None = request.app.get(OWNER_ID_CACHE_KEY)
    if owner_cache is not None:
        hit, cached_val = owner_cache.get_sync("bot_owner_id")
        if hit and isinstance(cached_val, int):
            return cached_val

    env_owner = os.getenv("OWNER_ID", "").strip()
    if env_owner:
        try:
            parsed = int(env_owner)
            if owner_cache is not None:
                owner_cache.set_sync("bot_owner_id", parsed)
            return parsed
        except ValueError:
            pass

    if bot is not None and hasattr(bot, "application_info"):
        try:
            app_info = await asyncio.wait_for(bot.application_info(), timeout=3.0)
            owner_obj = getattr(app_info, "owner", None)
            owner_id = getattr(owner_obj, "id", None)
            if owner_id is not None:
                resolved = int(owner_id)
                try:
                    bot.owner_id = resolved
                except Exception:
                    pass
                if owner_cache is not None:
                    owner_cache.set_sync("bot_owner_id", resolved)
                return resolved
        except Exception:
            logger.debug("Failed to resolve bot.application_info() for owner_id", exc_info=True)

    return None


async def resolve_live_member(
    guild: Any,
    user_id: int,
    member_cache: ShortTTLCache | None,
) -> Any | None:
    """
    Resolve a guild member using:
    1. `guild.get_member(user_id)` (discord.py in-memory cache)
    2. `member_cache` (`ShortTTLCache`, 60s TTL, including negative cache sentinel)
    3. `await guild.fetch_member(user_id)` fallback with 3.0s timeout
    """
    if guild is None:
        return None

    get_member_fn = getattr(guild, "get_member", None)
    if callable(get_member_fn):
        cached_member = get_member_fn(int(user_id))
        if cached_member is not None:
            return cached_member

    cache_key = (int(getattr(guild, "id", 0)), int(user_id))
    if member_cache is not None:
        hit, cached_entry = member_cache.get_sync(cache_key)
        if hit:
            return None if cached_entry is _MISSING_MEMBER_SENTINEL else cached_entry

    fetch_member_fn = getattr(guild, "fetch_member", None)
    if not callable(fetch_member_fn):
        return None

    try:
        fetched_member = await asyncio.wait_for(fetch_member_fn(int(user_id)), timeout=3.0)
        if member_cache is not None:
            member_cache.set_sync(
                cache_key,
                _MISSING_MEMBER_SENTINEL if fetched_member is None else fetched_member,
            )
        return fetched_member
    except Exception as exc:
        # Cache negative lookup on NotFound / Forbidden so repeated hits don't spam Discord REST API
        exc_name = type(exc).__name__
        if member_cache is not None and exc_name in ("NotFound", "Forbidden"):
            member_cache.set_sync(cache_key, _MISSING_MEMBER_SENTINEL)
        return None


def _extract_member_perm_bits_and_roles(member: Any) -> tuple[int, bool, bool, list[int]]:
    if member is None:
        return 0, False, False, []

    perms_obj = getattr(member, "guild_permissions", None)
    perm_bits = 0
    is_admin = False
    has_manage = False

    if perms_obj is not None:
        raw_val = getattr(perms_obj, "value", 0)
        if isinstance(raw_val, int):
            perm_bits = raw_val
        is_admin = bool(getattr(perms_obj, "administrator", False)) or bool(
            perm_bits & DISCORD_PERM_ADMINISTRATOR
        )
        has_manage = bool(getattr(perms_obj, "manage_guild", False)) or bool(
            perm_bits & DISCORD_PERM_MANAGE_GUILD
        )
        if is_admin:
            perm_bits |= DISCORD_PERM_ADMINISTRATOR
        if has_manage:
            perm_bits |= DISCORD_PERM_MANAGE_GUILD

    role_ids: list[int] = []
    raw_roles = getattr(member, "roles", None)
    if isinstance(raw_roles, (list, tuple, set)):
        for r in raw_roles:
            rid = getattr(r, "id", None)
            if rid is not None:
                try:
                    role_ids.append(int(rid))
                except (ValueError, TypeError):
                    continue

    return perm_bits, is_admin, has_manage, role_ids


def _extract_dashboard_rev(settings: dict[str, Any] | None) -> int:
    """Return the integer `dashboard_rev` from a guild_settings document (missing/invalid = 0)."""
    if not isinstance(settings, dict):
        return 0
    raw = settings.get("dashboard_rev", 0)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return int(raw)
    return 0


async def _fetch_fresh_guild_settings_strict(
    bot: Any, guild_id: int
) -> tuple[bool, dict[str, Any] | None]:
    """
    Fetch `db.guild_settings` directly from MongoDB (bypassing GuildConfigCache) and
    strictly distinguish:
    - `(True, dict)` -> read succeeded and document exists
    - `(True, None)` -> read succeeded and document is missing (safe for upsert on rev 0)
    - `(False, None)` -> read failed or database unavailable (mutations MUST return 503)
    """
    db = get_ready_db(bot)
    if db is None or getattr(db, "guild_settings", None) is None:
        return False, None
    try:
        doc = await asyncio.wait_for(
            db.guild_settings.find_one({"guild_id": int(guild_id)}),
            timeout=LOGS_DB_TIMEOUT_SECONDS,
        )
        if doc is None:
            return True, None
        if isinstance(doc, dict):
            return True, copy.deepcopy(doc)
        return False, None
    except Exception:
        logger.exception("Failed to fetch fresh guild_settings for guild_id=%s", guild_id)
        return False, None


async def _fetch_fresh_guild_settings(bot: Any, guild_id: int) -> dict[str, Any]:
    """
    Fail-open wrapper around `_fetch_fresh_guild_settings_strict` for display GET pages.
    """
    read_ok, doc = await _fetch_fresh_guild_settings_strict(bot, guild_id)
    return doc if (read_ok and isinstance(doc, dict)) else {}


async def resolve_guild_access(
    request: web.Request,
    guild_id: int,
    user_id: int,
    required_permission: str,
    *,
    fresh_settings: bool = False,
    strict_settings_read: bool | None = None,
) -> GuildAccessContext:
    """
    Evaluate whether `user_id` has `required_permission` in `guild_id` using
    live guild membership, live member roles/permissions, and `GuildConfigCache`
    (or a fresh MongoDB read when `fresh_settings=True`).
    For POST/mutation endpoints (or when `strict_settings_read=True`), a failed fresh
    MongoDB read fails closed with `status_code=503` and `settings_read_failed=True`
    so a transient read failure is never mistaken for "no document".
    Never relies on the session's stored guild list.
    """
    bot = request.app.get(BOT_KEY)
    get_guild_fn = getattr(bot, "get_guild", None) if bot is not None else None
    guild = get_guild_fn(int(guild_id)) if callable(get_guild_fn) else None

    if guild is None:
        return GuildAccessContext(
            guild=None,
            allowed=False,
            status_code=404,
            reason="Bot is not in this server.",
            user_id=int(user_id),
        )

    bot_owner_id = await resolve_bot_owner_id(request)
    guild_owner_id = getattr(guild, "owner_id", None)
    is_bot_owner = bot_owner_id is not None and int(user_id) == int(bot_owner_id)
    is_guild_owner = guild_owner_id is not None and int(user_id) == int(guild_owner_id)

    member_cache: ShortTTLCache | None = request.app.get(MEMBER_CACHE_KEY)
    member = await resolve_live_member(guild, int(user_id), member_cache)

    perm_bits, is_admin, has_manage, role_ids = _extract_member_perm_bits_and_roles(member)
    is_member = (member is not None) or is_guild_owner

    # Non-members who are not the Bot Owner are always denied before touching guild_settings
    if not is_member and not is_bot_owner:
        return GuildAccessContext(
            guild=guild,
            allowed=False,
            status_code=403,
            reason="You do not have permission to view this page.",
            user_id=int(user_id),
            bot_owner_id=bot_owner_id,
            is_bot_owner=False,
            is_guild_owner=False,
            is_discord_admin=False,
            has_manage_guild=False,
            member_role_ids=[],
            granted_permissions=set(),
            settings={},
            settings_doc=None,
            settings_read_failed=False,
        )

    settings: dict[str, Any] = {}
    settings_doc: dict[str, Any] | None = None
    if fresh_settings:
        is_strict = (
            strict_settings_read
            if strict_settings_read is not None
            else (str(getattr(request, "method", "GET")).upper() != "GET")
        )
        read_ok, raw_doc = await _fetch_fresh_guild_settings_strict(bot, int(guild_id))
        if not read_ok:
            if is_strict:
                return GuildAccessContext(
                    guild=guild,
                    allowed=False,
                    status_code=503,
                    reason="Database temporarily unavailable, please retry",
                    user_id=int(user_id),
                    bot_owner_id=bot_owner_id,
                    is_bot_owner=is_bot_owner,
                    is_guild_owner=is_guild_owner,
                    is_discord_admin=is_admin,
                    has_manage_guild=has_manage,
                    member_role_ids=role_ids,
                    granted_permissions=set(),
                    settings={},
                    settings_doc=None,
                    settings_read_failed=True,
                )
            settings = {}
            settings_doc = None
        else:
            settings_doc = raw_doc
            settings = copy.deepcopy(raw_doc) if isinstance(raw_doc, dict) else {}
    else:
        cfg_cache: GuildConfigCache | None = request.app.get(GUILD_CONFIG_CACHE_KEY)
        if cfg_cache is not None:
            settings = await cfg_cache.get(int(guild_id))

    acl_config = settings.get("dashboard_permissions") if isinstance(settings, dict) else None

    allowed = evaluate_permission(
        required_permission,
        user_id=int(user_id),
        guild_owner_id=guild_owner_id,
        bot_owner_id=bot_owner_id,
        is_member=is_member,
        discord_permissions=perm_bits,
        is_administrator=is_admin,
        has_manage_guild=has_manage,
        member_role_ids=role_ids,
        acl_config=acl_config,
    )

    granted = resolve_user_permissions(
        user_id=int(user_id),
        guild_owner_id=guild_owner_id,
        bot_owner_id=bot_owner_id,
        is_member=is_member,
        discord_permissions=perm_bits,
        is_administrator=is_admin,
        has_manage_guild=has_manage,
        member_role_ids=role_ids,
        acl_config=acl_config,
    )

    return GuildAccessContext(
        guild=guild,
        allowed=allowed,
        status_code=200 if allowed else 403,
        reason="OK" if allowed else "You do not have permission to view this page.",
        user_id=int(user_id),
        bot_owner_id=bot_owner_id,
        is_bot_owner=is_bot_owner,
        is_guild_owner=is_guild_owner,
        is_discord_admin=is_admin,
        has_manage_guild=has_manage,
        member_role_ids=role_ids,
        granted_permissions=granted,
        settings=settings,
        settings_doc=settings_doc,
        settings_read_failed=False,
    )


def build_sidebar_nav(guild_id: int, granted: set[str]) -> list[dict[str, str]]:
    """
    Build sidebar navigation items filtered by the user's granted permissions.
    Pages are added in Checkpoint 3B only as their templates and handlers land.
    """
    candidates = [
        ("overview", f"/manage/{guild_id}", "fa-chart-pie", "Overview", "overview.view"),
        ("modules", f"/modules/{guild_id}", "fa-layer-group", "Modules & Commands", "modules.view"),
        ("automod", f"/automod/{guild_id}", "fa-shield-halved", "AutoMod", "automod.view"),
        ("lockdown", f"/lockdown/{guild_id}", "fa-lock", "Lockdown", "antinuke.view"),
        ("wizard", f"/wizard/{guild_id}", "fa-wand-magic-sparkles", "Setup Wizard", "settings.view"),
        ("misc", f"/misc/{guild_id}", "fa-sliders", "Utilities & Misc", "settings.view"),
        ("logs", f"/logs/{guild_id}", "fa-scroll", "Audit Logs", "logging.view"),
    ]
    nav: list[dict[str, str]] = []
    for key, href, icon, title, req_perm in candidates:
        if req_perm in granted or key == "overview":
            nav.append({"key": key, "href": href, "icon": icon, "title": title})
    return nav


def render_error_page(
    request: web.Request,
    *,
    status_code: int,
    title: str,
    message: str,
    show_login: bool = False,
) -> web.Response:
    bot = request.app.get(BOT_KEY)
    context = {
        "status_code": status_code,
        "title": title,
        "message": message,
        "show_login": show_login,
        "bot_avatar": get_bot_avatar_url(bot),
    }
    try:
        return aiohttp_jinja2.render_template(
            "error.html",
            request,
            context,
            status=status_code,
        )
    except Exception:
        return web.Response(
            text=f"{status_code} {title}: {message}",
            status=status_code,
            content_type="text/plain",
        )


def _redirect(location: str) -> web.Response:
    """Return a standard 302 redirect Response (avoids aiohttp HTTPException return deprecation)."""
    return web.Response(status=302, headers={"Location": location})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE HANDLERS
# ─────────────────────────────────────────────────────────────────────────────


async def handle_health(request: web.Request) -> web.Response:
    """
    GET /health
    - Zero database queries.
    - Returns {"status": "ok", "bot_ready": bool, "latency_ms": int | null}.
    - Safely handles NaN / inf / None / negative bot.latency.
    - ?strict=1 returns HTTP 503 while bot_ready is False.
    """
    bot = request.app.get(BOT_KEY)
    is_ready_fn = getattr(bot, "is_ready", None) if bot is not None else None
    is_closed_fn = getattr(bot, "is_closed", None) if bot is not None else None

    try:
        ready_val = bool(is_ready_fn()) if callable(is_ready_fn) else bool(getattr(bot, "ready", False))
    except Exception:
        ready_val = False

    try:
        closed_val = bool(is_closed_fn()) if callable(is_closed_fn) else False
    except Exception:
        closed_val = False

    bot_ready = bool(bot is not None and ready_val and not closed_val)

    raw_latency = getattr(bot, "latency", None) if bot is not None else None
    latency_ms: int | None = None
    if (
        isinstance(raw_latency, (int, float))
        and not isinstance(raw_latency, bool)
        and math.isfinite(raw_latency)
        and raw_latency >= 0.0
    ):
        latency_ms = int(round(raw_latency * 1000))

    strict_arg = request.query.get("strict", "").strip().lower()
    strict_mode = strict_arg in ("1", "true", "yes")
    http_status = 503 if (strict_mode and not bot_ready) else 200

    return web.json_response(
        {
            "status": "ok",
            "bot_ready": bot_ready,
            "latency_ms": latency_ms,
        },
        status=http_status,
    )


async def handle_debug_ip(request: web.Request) -> web.Response:
    """
    GET /debug/ip
    Temporary Bot-Owner-only diagnostic endpoint registered only when WEBDASH_DEBUG_IP=1.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return _redirect("/login")

    try:
        user_id = int(session_doc.get("discord_id", 0))
    except (ValueError, TypeError):
        return _redirect("/login")

    bot_owner_id = await resolve_bot_owner_id(request)
    if bot_owner_id is None or user_id != int(bot_owner_id):
        return web.json_response(
            {"error": "Forbidden: Bot Owner access required."},
            status=403,
        )

    cf_headers = {
        k: v for k, v in request.headers.items() if k.lower().startswith("cf-")
    }
    return web.json_response(
        {
            "resolved_ip": request.get(CLIENT_IP_KEY, "invalid"),
            "ip_trusted": bool(request.get(CLIENT_IP_TRUSTED_KEY, False)),
            "trusted_source": request.get(CLIENT_IP_SOURCE_KEY, "none"),
            "request_remote": getattr(request, "remote", None),
            "raw_x_forwarded_for": request.headers.get("X-Forwarded-For"),
            "raw_x_recluse_client_ip": request.headers.get("X-Recluse-Client-IP"),
            "origin_secret_header_present": "X-Recluse-Origin-Secret" in request.headers,
            "origin_shared_secret_configured": bool(os.getenv("ORIGIN_SHARED_SECRET", "")),
            "require_origin_shared_secret": os.getenv("REQUIRE_ORIGIN_SHARED_SECRET", "0").strip().lower()
            in ("1", "true", "yes", "on"),
            "cf_headers": cf_headers,
            "trusted_proxy_hops_env": os.getenv("TRUSTED_PROXY_HOPS", "1"),
        }
    )


async def handle_login(request: web.Request) -> web.StreamResponse:
    """
    GET /login
    - If bot.db_ready is False (get_ready_db(bot) is None), logs one clear warning
      and renders a friendly 503 error page instead of failing with a 500.
    - Generates a random OAuth2 state token, sets `recluse_oauth_state` cookie,
      and redirects to Discord's OAuth2 authorization screen.
    """
    bot = request.app.get(BOT_KEY)
    if get_ready_db(bot) is None:
        return render_error_page(
            request,
            status_code=503,
            title="Sessions Temporarily Unavailable",
            message="The database connection is still initializing or unavailable (sessions disabled). Please try logging in again shortly.",
        )

    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("REDIRECT_URI", "").strip()
    if not client_id or not redirect_uri:
        return render_error_page(
            request,
            status_code=500,
            title="Configuration Error",
            message="Discord OAuth2 Client ID or Redirect URI is not configured on the server.",
        )

    state = generate_oauth_state()
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
    )
    oauth_url = f"https://discord.com/api/oauth2/authorize?{params}"

    response = _redirect(oauth_url)
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=300,
        path="/",
        httponly=True,
        secure=is_secure_request(request),
        samesite="Lax",
    )
    return response


async def _default_exchange_discord_oauth(code: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Exchange OAuth2 code with Discord for user profile and guild list."""
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("REDIRECT_URI", "").strip()

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    timeout = aiohttp.ClientTimeout(total=10.0)

    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with http_session.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data=data,
            headers=headers,
        ) as token_resp:
            if token_resp.status != 200:
                raise RuntimeError(f"Discord token exchange failed with status {token_resp.status}")
            token_data = await token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise RuntimeError("Missing access_token in Discord response")

        auth_headers = {"Authorization": f"Bearer {access_token}"}
        async with http_session.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers=auth_headers,
        ) as user_resp:
            if user_resp.status != 200:
                raise RuntimeError(f"Discord /users/@me failed with status {user_resp.status}")
            user_data = await user_resp.json()

        guilds_data: list[dict[str, Any]] = []
        async with http_session.get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers=auth_headers,
        ) as guilds_resp:
            if guilds_resp.status == 200:
                raw_guilds = await guilds_resp.json()
                if isinstance(raw_guilds, list):
                    guilds_data = raw_guilds

    return user_data, guilds_data


async def handle_callback(request: web.Request) -> web.StreamResponse:
    """
    GET /callback
    - Verifies OAuth2 state cookie against query parameter using verify_oauth_state.
    - Rotates session on login: revokes any previously presented recluse_sid cookie.
    - Creates a fresh session in db.webdash_sessions and sets recluse_sid cookie.
    """
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    query_state = request.query.get("state")

    if not verify_oauth_state(cookie_state, query_state):
        resp = render_error_page(
            request,
            status_code=400,
            title="Invalid OAuth State",
            message="Your login state expired or did not match. Please try logging in again.",
            show_login=True,
        )
        resp.del_cookie(OAUTH_STATE_COOKIE_NAME, path="/")
        return resp

    code = request.query.get("code", "").strip()
    if not code:
        resp = render_error_page(
            request,
            status_code=400,
            title="Missing Authorization Code",
            message="Discord did not return an authorization code.",
            show_login=True,
        )
        resp.del_cookie(OAUTH_STATE_COOKIE_NAME, path="/")
        return resp

    session_manager: SessionManager = request.app[SESSION_MANAGER_KEY]

    # Session rotation: always revoke any presented session cookie before issuing a new one
    old_sid = request.cookies.get(SESSION_COOKIE_NAME)
    if old_sid:
        await session_manager.revoke_session(old_sid)

    oauth_client: Callable[[str], Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]] = (
        request.app.get(OAUTH_CLIENT_KEY) or _default_exchange_discord_oauth
    )
    try:
        user_data, guilds_data = await oauth_client(code)
        discord_id = int(user_data["id"])
        username = str(user_data.get("username") or "User")
        avatar = user_data.get("avatar")
    except Exception:
        logger.exception("OAuth2 callback exchange failed")
        resp = render_error_page(
            request,
            status_code=502,
            title="Discord Authentication Failed",
            message="Could not exchange authorization code with Discord. Please try again.",
            show_login=True,
        )
        resp.del_cookie(OAUTH_STATE_COOKIE_NAME, path="/")
        return resp

    created = await session_manager.create_session(
        discord_id=discord_id,
        username=username,
        avatar=str(avatar) if avatar else None,
        guilds=guilds_data,
        client_ip=request.get(CLIENT_IP_KEY, "invalid"),
        user_agent=request.headers.get("User-Agent"),
    )
    if created is None:
        resp = render_error_page(
            request,
            status_code=503,
            title="Service Temporarily Unavailable",
            message="Database is currently unavailable to store your session. Please try again shortly.",
            show_login=True,
        )
        resp.del_cookie(OAUTH_STATE_COOKIE_NAME, path="/")
        return resp

    new_raw_sid, _doc = created
    redirect_resp = _redirect("/")
    redirect_resp.del_cookie(OAUTH_STATE_COOKIE_NAME, path="/")
    redirect_resp.set_cookie(
        SESSION_COOKIE_NAME,
        new_raw_sid,
        max_age=DEFAULT_SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        secure=is_secure_request(request),
        samesite="Lax",
    )
    return redirect_resp


async def handle_logout(request: web.Request) -> web.StreamResponse:
    """
    POST /logout
    Revokes the current session in db.webdash_sessions and clears the recluse_sid cookie.
    """
    session_manager: SessionManager = request.app[SESSION_MANAGER_KEY]
    raw_sid = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_sid:
        await session_manager.revoke_session(raw_sid)

    response = _redirect("/")
    response.del_cookie(SESSION_COOKIE_NAME, path="/")
    return response


async def _build_server_picker_cards(
    request: web.Request,
    session_doc: dict[str, Any],
    *,
    bot_owner_id: int | None,
) -> list[dict[str, Any]]:
    """
    Build the list of manageable/invitable server cards for the Server Picker.
    - For guilds where the bot IS present (`bot.get_guild(gid)` is not None):
      skips guilds immediately unless the user is a member or guild/bot owner
      (never reads GuildConfigCache for guilds where the user is not a member or owner),
      then evaluates `overview.view` using LIVE member roles and `GuildConfigCache`.
    - For guilds in `session_doc['guilds']` where the bot is NOT present:
      shows an "Invite Bot" card if the user has Owner / Administrator / Manage Server in Discord.
    """
    bot = request.app.get(BOT_KEY)
    user_id = int(session_doc["discord_id"])
    invite_base = build_invite_url()

    cards: list[dict[str, Any]] = []
    seen_guild_ids: set[int] = set()

    # 1. Check guilds where the bot is currently a member
    bot_guilds = list(getattr(bot, "guilds", []) or []) if bot is not None else []
    for guild in bot_guilds:
        gid = getattr(guild, "id", None)
        if gid is None:
            continue
        gid_int = int(gid)
        seen_guild_ids.add(gid_int)

        # For the server picker list, check cached member first (avoid N sequential REST calls)
        member = guild.get_member(user_id) if callable(getattr(guild, "get_member", None)) else None
        if member is None:
            member_cache: ShortTTLCache | None = request.app.get(MEMBER_CACHE_KEY)
            if member_cache is not None:
                hit, cached_entry = member_cache.get_sync((gid_int, user_id))
                if hit and cached_entry is not _MISSING_MEMBER_SENTINEL:
                    member = cached_entry

        guild_owner_id = getattr(guild, "owner_id", None)
        is_guild_owner = guild_owner_id is not None and int(guild_owner_id) == user_id
        is_bot_owner = bot_owner_id is not None and int(bot_owner_id) == user_id
        perm_bits, is_admin, has_manage, role_ids = _extract_member_perm_bits_and_roles(member)
        is_member = (member is not None) or is_guild_owner

        # Piece 2 Review #2: Only read GuildConfigCache for guilds where the user
        # is a member or guild/bot owner.
        if not (is_member or is_guild_owner or is_bot_owner):
            continue

        cfg_cache: GuildConfigCache | None = request.app.get(GUILD_CONFIG_CACHE_KEY)
        acl_config = None
        if cfg_cache is not None:
            settings = await cfg_cache.get(gid_int)
            acl_config = settings.get("dashboard_permissions") if isinstance(settings, dict) else None

        can_view = evaluate_permission(
            "overview.view",
            user_id=user_id,
            guild_owner_id=guild_owner_id,
            bot_owner_id=bot_owner_id,
            is_member=is_member,
            discord_permissions=perm_bits,
            is_administrator=is_admin,
            has_manage_guild=has_manage,
            member_role_ids=role_ids,
            acl_config=acl_config,
        )
        if not can_view:
            continue

        g_name = str(getattr(guild, "name", None) or "Server")
        icon_obj = getattr(guild, "icon", None)
        icon_url_raw = getattr(icon_obj, "url", None) if icon_obj is not None else None
        role_badge = (
            "Owner"
            if is_guild_owner
            else ("Admin" if is_admin else ("Bot Owner" if is_bot_owner else "Manager"))
        )
        cards.append(
            {
                "id": str(gid_int),
                "name": g_name,
                "icon_url": get_guild_icon_url(gid_int, g_name, icon_url_raw),
                "in_server": True,
                "role_badge": role_badge,
                "invite_url": f"{invite_base}&guild_id={gid_int}",
            }
        )

    # 2. Include OAuth session guilds where the bot is NOT yet invited
    session_guilds = session_doc.get("guilds")
    if isinstance(session_guilds, list):
        for g in session_guilds:
            if not isinstance(g, dict) or "id" not in g:
                continue
            try:
                gid_int = int(g["id"])
            except (ValueError, TypeError):
                continue
            if gid_int in seen_guild_ids:
                continue
            try:
                perms = int(g.get("permissions", 0))
            except (ValueError, TypeError):
                perms = 0
            is_owner = bool(g.get("owner", False))
            has_admin_or_manage = (
                is_owner
                or bool(perms & DISCORD_PERM_ADMINISTRATOR)
                or bool(perms & DISCORD_PERM_MANAGE_GUILD)
            )
            if not has_admin_or_manage:
                continue

            g_name = str(g.get("name") or "Server")
            cards.append(
                {
                    "id": str(gid_int),
                    "name": g_name,
                    "icon_url": get_guild_icon_url(gid_int, g_name, g.get("icon")),
                    "in_server": False,
                    "role_badge": "Invite Needed",
                    "invite_url": f"{invite_base}&guild_id={gid_int}",
                }
            )

    return cards


async def handle_home(request: web.Request) -> web.Response:
    """
    GET /
    - Unauthenticated: renders landing.html (middleware fast-path: zero DB hits).
    - Authenticated: renders servers.html (Server Picker).
    """
    bot = request.app.get(BOT_KEY)
    session_doc = request.get(SESSION_DATA_KEY)

    if not session_doc:
        context = {
            "bot_avatar": get_bot_avatar_url(bot),
            "invite_link": build_invite_url(),
        }
        return aiohttp_jinja2.render_template("landing.html", request, context)

    return await _render_servers_page(request, session_doc)


async def handle_servers(request: web.Request) -> web.Response:
    """
    GET /servers
    Redirects to /login if unauthenticated; renders servers.html if authenticated.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return _redirect("/login")
    return await _render_servers_page(request, session_doc)


async def _render_servers_page(request: web.Request, session_doc: dict[str, Any]) -> web.Response:
    bot = request.app.get(BOT_KEY)
    bot_owner_id = await resolve_bot_owner_id(request)
    user_id = int(session_doc["discord_id"])
    is_bot_owner = bot_owner_id is not None and user_id == int(bot_owner_id)

    cards = await _build_server_picker_cards(
        request,
        session_doc,
        bot_owner_id=bot_owner_id,
    )
    context = {
        "bot_avatar": get_bot_avatar_url(bot),
        "user_name": str(session_doc.get("username") or "User"),
        "user_avatar": get_user_avatar_url(session_doc),
        "csrf_token": str(session_doc.get("csrf_token") or ""),
        "is_bot_owner": is_bot_owner,
        "guilds": cards,
        "invite_link": build_invite_url(),
    }
    return aiohttp_jinja2.render_template("servers.html", request, context)


async def _prepare_guild_page(
    request: web.Request,
    *,
    required_permission: str,
    active_tab: str,
    fresh_settings: bool = False,
) -> tuple[web.Response | None, GuildAccessContext | None, dict[str, Any]]:
    """
    Validate session, guild_id snowflake, live guild membership, and required RBAC
    permission. Returns `(error_response, None, {})` on failure, or
    `(None, access, base_context)` when authorized.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return _redirect("/login"), None, {}

    try:
        user_id = int(session_doc["discord_id"])
    except (KeyError, ValueError, TypeError):
        return _redirect("/login"), None, {}

    raw_guild_id = request.match_info.get("guild_id", "")
    try:
        guild_id = int(raw_guild_id)
    except (ValueError, TypeError):
        return (
            render_error_page(
                request,
                status_code=400,
                title="Invalid Server ID",
                message="The requested server ID is not a valid Discord snowflake.",
            ),
            None,
            {},
        )

    access = await resolve_guild_access(
        request,
        guild_id,
        user_id,
        required_permission,
        fresh_settings=fresh_settings,
    )
    if access.guild is None:
        return (
            render_error_page(
                request,
                status_code=404,
                title="Server Not Found",
                message="Recluse is not currently a member of this server.",
            ),
            None,
            {},
        )
    if not access.allowed:
        return (
            render_error_page(
                request,
                status_code=403,
                title="Access Denied",
                message="You do not have permission to access this server's dashboard.",
            ),
            None,
            {},
        )

    guild = access.guild
    bot = request.app.get(BOT_KEY)
    guild_name = str(getattr(guild, "name", None) or "Server")
    icon_obj = getattr(guild, "icon", None)
    raw_icon_url = getattr(icon_obj, "url", None) if icon_obj is not None else None
    guild_icon = get_guild_icon_url(guild_id, guild_name, raw_icon_url)

    base_context: dict[str, Any] = {
        "bot_avatar": get_bot_avatar_url(bot),
        "user_name": str(session_doc.get("username") or "User"),
        "user_avatar": get_user_avatar_url(session_doc),
        "csrf_token": str(session_doc.get("csrf_token") or ""),
        "guild_id": str(guild_id),
        "guild_name": guild_name,
        "guild_icon": guild_icon,
        "active_tab": active_tab,
        "nav_items": build_sidebar_nav(guild_id, access.granted_permissions),
        "is_bot_owner": access.is_bot_owner,
        "is_guild_owner": access.is_guild_owner,
        "is_discord_admin": access.is_discord_admin,
    }
    return None, access, base_context


async def handle_guild_overview(request: web.Request) -> web.Response:
    """
    GET /manage/{guild_id}
    - Redirects to /login when logged out.
    - Evaluates `overview.view` via `resolve_guild_access` using live member roles.
    - Renders overview.html with live guild stats and cached guild_settings.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="overview.view",
        active_tab="overview",
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    guild = access.guild
    settings = access.settings

    member_count_val = getattr(guild, "member_count", None)
    if member_count_val is None:
        members_list = getattr(guild, "members", None)
        member_count_val = len(members_list) if isinstance(members_list, (list, tuple, set)) else 0

    channels_list = getattr(guild, "channels", None) or getattr(guild, "text_channels", None) or []
    roles_list = getattr(guild, "roles", None) or []

    # Verified cog defaults (re-read in current session):
    # - automod_enabled defaults to False:
    #   bot.py:189 (`if cfg.get("automod_enabled"):`), cogs/ai.py:591 (`if cfg.get("automod_enabled")`),
    #   cogs/admin.py:247 (`cfg.get("automod_enabled", False)`), cogs/admin.py:843 (`cfg.get("automod_enabled", False)`)
    automod_enabled = bool(settings.get("automod_enabled", False))
    banned_words = settings.get("banned_words")
    banned_words_count = len(banned_words) if isinstance(banned_words, list) else 0

    # - default_ai_model defaults to "auto":
    #   cogs/ai.py:657 (`cfg.get("default_ai_model", "auto")`),
    #   cogs/admin.py:256 (`cfg.get("default_ai_model", "auto")`),
    #   cogs/admin.py:842 (`cfg.get("default_ai_model", "auto")`)
    #   Valid options in cogs/ai.py:77-87, 657: auto / deepseek / groq / gemini
    raw_ai_model = str(settings.get("default_ai_model") or "auto").strip().lower()
    default_ai_model = raw_ai_model if raw_ai_model in ("auto", "deepseek", "groq", "gemini") else "auto"

    # - ai_enabled defaults to True:
    #   cogs/ai.py:607 (`if not cfg.get("ai_enabled", True): return`),
    #   cogs/admin.py:240 (`cfg.get("ai_enabled", True)`), cogs/admin.py:841 (`cfg.get("ai_enabled", True)`)
    ai_enabled = bool(settings.get("ai_enabled", True))

    disabled_cogs = settings.get("disabled_cogs")
    disabled_cogs_count = len(disabled_cogs) if isinstance(disabled_cogs, list) else 0

    command_settings = settings.get("command_settings")
    command_overrides_count = len(command_settings) if isinstance(command_settings, dict) else 0

    module_cards = [
        # welcome_enabled defaults to True: cogs/admin.py:243, cogs/admin.py:983, cogs/admin.py:1014
        {"label": "Welcome & Leave", "icon": "fa-door-open", "enabled": bool(settings.get("welcome_enabled", True))},
        # mod_enabled defaults to True: cogs/moderation.py:23, cogs/admin.py:241
        {"label": "Moderation", "icon": "fa-gavel", "enabled": bool(settings.get("mod_enabled", True))},
        # automod_enabled defaults to False: bot.py:189, cogs/ai.py:591, cogs/admin.py:247, cogs/admin.py:843
        {"label": "AutoMod", "icon": "fa-shield-halved", "enabled": automod_enabled},
        # autoresponse_enabled defaults to True: cogs/autoresponse.py:81
        {"label": "Auto-Responder", "icon": "fa-reply-all", "enabled": bool(settings.get("autoresponse_enabled", True))},
        # leveling_enabled defaults to True: cogs/leveling.py:367, cogs/leveling.py:428, cogs/admin.py:242
        {"label": "Leveling & XP", "icon": "fa-ranking-star", "enabled": bool(settings.get("leveling_enabled", True))},
        # ai_enabled defaults to True: cogs/ai.py:607, cogs/admin.py:240, cogs/admin.py:841
        {"label": "AI Chat", "icon": "fa-robot", "enabled": ai_enabled},
        # sports_enabled defaults to True: cogs/sports.py:35, cogs/admin.py:244
        {"label": "Sports", "icon": "fa-basketball", "enabled": bool(settings.get("sports_enabled", True))},
        # misc_enabled defaults to True: cogs/misc.py:174, cogs/admin.py:246
        {"label": "Utilities & Misc", "icon": "fa-sliders", "enabled": bool(settings.get("misc_enabled", True))},
    ]

    context.update(
        {
            "member_count": f"{int(member_count_val):,}",
            "channel_count": len(channels_list) if hasattr(channels_list, "__len__") else 0,
            "role_count": len(roles_list) if hasattr(roles_list, "__len__") else 0,
            "automod_enabled": automod_enabled,
            "banned_words_count": banned_words_count,
            "default_ai_model": default_ai_model,
            "ai_enabled": ai_enabled,
            "disabled_cogs_count": disabled_cogs_count,
            "command_overrides_count": command_overrides_count,
            "module_cards": module_cards,
        }
    )
    return aiohttp_jinja2.render_template("overview.html", request, context)


async def handle_guild_automod(request: web.Request) -> web.Response:
    """
    GET /automod/{guild_id}
    - Requires `automod.view` (webdash/permissions.py:33); editing requires `automod.edit` (webdash/permissions.py:34).
    - Reads settings fresh from MongoDB (bypassing GuildConfigCache).
    - Reads `banned_words` (bot.py:191, cogs/ai.py:593, cogs/admin.py:907, default=[])
      and `automod_enabled` (bot.py:189, cogs/ai.py:591, cogs/admin.py:247, 843, default=False).
    - Caps displayed `banned_words` at MAX_BANNED_WORDS_DISPLAY (500) with a "+N more" note.
      If the stored list has > 500 entries, the editor is locked read-only with a notice so saving
      cannot truncate unseen entries.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="automod.view",
        active_tab="automod",
        fresh_settings=True,
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    settings = access.settings
    raw_banned = settings.get("banned_words", [])
    all_banned_words: list[str] = []
    if isinstance(raw_banned, list):
        for item in raw_banned:
            if isinstance(item, (str, int, float)):
                cleaned = str(item).strip()
                if cleaned:
                    all_banned_words.append(cleaned)

    total_banned_words = len(all_banned_words)
    displayed_words = all_banned_words[:MAX_BANNED_WORDS_DISPLAY]
    extra_count = max(0, total_banned_words - MAX_BANNED_WORDS_DISPLAY)
    over_display_cap = extra_count > 0
    can_edit_automod = ("automod.edit" in access.granted_permissions) and not over_display_cap

    context.update(
        {
            "automod_enabled": bool(settings.get("automod_enabled", False)),
            "banned_words": displayed_words,
            "total_banned_words": total_banned_words,
            "extra_banned_words_count": extra_count,
            "over_display_cap": over_display_cap,
            "can_edit_automod": can_edit_automod,
            "dashboard_rev": _extract_dashboard_rev(settings),
            "words_string": ", ".join(displayed_words),
        }
    )
    return aiohttp_jinja2.render_template("automod.html", request, context)


async def handle_guild_lockdown(request: web.Request) -> web.Response:
    """
    GET /lockdown/{guild_id}
    - Requires `antinuke.view` (webdash/permissions.py:36).
    - Renders lockdown.html showing the "Disabled: being rebuilt" status banner
      and disabled button (cogs/dashboard.py:1365-1378).
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="antinuke.view",
        active_tab="lockdown",
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    return aiohttp_jinja2.render_template("lockdown.html", request, context)


async def handle_guild_misc(request: web.Request) -> web.Response:
    """
    GET /misc/{guild_id}
    - Requires `settings.view` (webdash/permissions.py:16); toggling requires `settings.edit` (webdash/permissions.py:17).
    - Reads settings fresh from MongoDB (bypassing GuildConfigCache).
    - Exposes `misc_enabled` read at cogs/misc.py:174, 538, cogs/admin.py:246, default=True.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="settings.view",
        active_tab="misc",
        fresh_settings=True,
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    settings = access.settings
    context.update(
        {
            "misc_enabled": bool(settings.get("misc_enabled", True)),
            "can_edit_misc": "settings.edit" in access.granted_permissions,
            "dashboard_rev": _extract_dashboard_rev(settings),
        }
    )
    return aiohttp_jinja2.render_template("misc.html", request, context)


async def handle_guild_wizard(request: web.Request) -> web.Response:
    """
    GET /wizard/{guild_id}
    - Requires `settings.view` (webdash/permissions.py:16); saving `default_ai_model`
      requires `ai.edit` (webdash/permissions.py:47).
    - Reads settings fresh from MongoDB (bypassing GuildConfigCache).
    - Exposes the 4 real AI models wired in cogs/ai.py:77-91, 291-299, 401-411, 657, 743-754
      (`auto`, `deepseek`, `groq`, `gemini`).
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="settings.view",
        active_tab="wizard",
        fresh_settings=True,
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    settings = access.settings
    raw_model = str(settings.get("default_ai_model") or "auto").strip().lower()
    default_ai_model = raw_model if raw_model in VALID_AI_MODELS else "auto"

    context.update(
        {
            "default_ai_model": default_ai_model,
            "ai_enabled": bool(settings.get("ai_enabled", True)),
            "ai_models": AI_MODEL_OPTIONS,
            "can_edit_ai": "ai.edit" in access.granted_permissions,
            "dashboard_rev": _extract_dashboard_rev(settings),
        }
    )
    return aiohttp_jinja2.render_template("wizard.html", request, context)


async def _fetch_cursor_docs(
    cursor: Any,
    *,
    limit: int = LOGS_QUERY_LIMIT,
    timeout: float = LOGS_DB_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Drain an async Motor/mock cursor with a strict timeout, returning at most `limit` dicts."""
    async def _drain() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if hasattr(cursor, "__aiter__"):
            async for doc in cursor:
                if isinstance(doc, dict):
                    results.append(doc)
                    if len(results) >= limit:
                        break
            return results
        to_list_fn = getattr(cursor, "to_list", None)
        if callable(to_list_fn):
            raw_list = await to_list_fn(length=limit)
            if isinstance(raw_list, list):
                return [d for d in raw_list[:limit] if isinstance(d, dict)]
        return results

    return await asyncio.wait_for(_drain(), timeout=timeout)


def _format_utc_timestamp(raw_val: Any) -> str:
    """Format either a float/int epoch timestamp or a datetime into 'YYYY-MM-DD HH:MM UTC'."""
    if isinstance(raw_val, datetime.datetime):
        dt = raw_val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool) and math.isfinite(raw_val):
        try:
            dt = datetime.datetime.fromtimestamp(float(raw_val), tz=datetime.timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, OSError, OverflowError):
            return "Unknown"
    if isinstance(raw_val, str) and raw_val.strip():
        try:
            dt = datetime.datetime.fromtimestamp(float(raw_val.strip()), tz=datetime.timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, OSError, OverflowError):
            return raw_val.strip()[:32]
    return "Unknown"


def _format_audit_value(val: Any) -> str:
    """Format an old/new audit diff value concisely for read-only display."""
    try:
        rendered = json.dumps(val, ensure_ascii=False, default=str)
    except Exception:
        rendered = str(val)
    if len(rendered) > 120:
        return rendered[:117] + "..."
    return rendered


def _truncate_security_log_content(
    raw_content: Any,
    max_chars: int = MAX_SECURITY_LOG_CONTENT_CHARS,
) -> str:
    """Truncate displayed security_logs content to `max_chars` (300) characters with an ellipsis."""
    text = str(raw_content if raw_content is not None else "—")
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


async def handle_guild_logs(request: web.Request) -> web.Response:
    """
    GET /logs/{guild_id}
    - Requires `logging.view` (webdash/permissions.py:62).
    - Queries `db.security_logs` (sorted by float `timestamp` descending per cogs/dashboard.py:1425,
      bot.py:203-208, 262-267) for all users with `logging.view`.
    - Queries and renders `db.dashboard_audit` (sorted by UTC `created_at` descending per
      webdash/audit.py:83, 138) ONLY when the user also holds `audit.view` (webdash/permissions.py:63).
    - If either query fails, times out (>2s), or the database is unavailable, sets
      `security_logs_error` / `audit_logs_error` so the template renders
      "Could not load logs (database unavailable)".
    - Truncates displayed `security_logs` content to 300 characters with an ellipsis.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="logging.view",
        active_tab="logs",
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    guild_id = int(context["guild_id"])
    can_view_audit = "audit.view" in access.granted_permissions
    bot = request.app.get(BOT_KEY)
    db = get_ready_db(bot)

    security_logs: list[dict[str, str]] = []
    security_logs_error = False
    if db is None or getattr(db, "security_logs", None) is None:
        security_logs_error = True
    else:
        try:
            sec_cursor = (
                db.security_logs.find({"guild_id": guild_id})
                .sort("timestamp", -1)
                .limit(LOGS_QUERY_LIMIT)
            )
            raw_sec_logs = await _fetch_cursor_docs(sec_cursor, limit=LOGS_QUERY_LIMIT)
            for doc in raw_sec_logs:
                security_logs.append(
                    {
                        "timestamp": _format_utc_timestamp(doc.get("timestamp")),
                        "action": str(doc.get("action") or "Unknown"),
                        "user_id": str(doc.get("user_id") if doc.get("user_id") is not None else "—"),
                        "content": _truncate_security_log_content(doc.get("content")),
                    }
                )
        except Exception:
            security_logs_error = True
            logger.exception("Failed to query db.security_logs for guild_id=%s", guild_id)

    audit_logs: list[dict[str, Any]] = []
    audit_logs_error = False
    if can_view_audit:
        if db is None or getattr(db, "dashboard_audit", None) is None:
            audit_logs_error = True
        else:
            try:
                audit_cursor = (
                    db.dashboard_audit.find({"guild_id": guild_id})
                    .sort("created_at", -1)
                    .limit(LOGS_QUERY_LIMIT)
                )
                raw_audit_logs = await _fetch_cursor_docs(audit_cursor, limit=LOGS_QUERY_LIMIT)
                for doc in raw_audit_logs:
                    raw_changes = doc.get("changes")
                    change_items: list[dict[str, str]] = []
                    if isinstance(raw_changes, dict):
                        for field_key, diff_obj in raw_changes.items():
                            if isinstance(diff_obj, dict):
                                old_str = _format_audit_value(diff_obj.get("old"))
                                new_str = _format_audit_value(diff_obj.get("new"))
                            else:
                                old_str = "—"
                                new_str = _format_audit_value(diff_obj)
                            change_items.append(
                                {
                                    "key": str(field_key),
                                    "old": old_str,
                                    "new": new_str,
                                }
                            )
                    audit_logs.append(
                        {
                            "created_at": _format_utc_timestamp(doc.get("created_at")),
                            "actor_name": str(doc.get("actor_name") or "Unknown"),
                            "actor_id": str(doc.get("actor_id") if doc.get("actor_id") is not None else "—"),
                            "module": str(doc.get("module") or "settings"),
                            "action": str(doc.get("action") or "update"),
                            "change_items": change_items,
                        }
                    )
            except Exception:
                audit_logs_error = True
                logger.exception("Failed to query db.dashboard_audit for guild_id=%s", guild_id)

    context.update(
        {
            "security_logs": security_logs,
            "security_logs_error": security_logs_error,
            "can_view_audit": can_view_audit,
            "audit_logs": audit_logs,
            "audit_logs_error": audit_logs_error,
        }
    )
    return aiohttp_jinja2.render_template("logs.html", request, context)


def _extract_cog_commands(cog: Any) -> dict[str, dict[str, Any]]:
    """
    Collect top-level commands and subcommands from a loaded Discord Cog
    (matching cogs/dashboard.py:626-642).
    """
    cmds_dict: dict[str, dict[str, Any]] = {}
    if cog is None:
        return cmds_dict

    get_commands_fn = getattr(cog, "get_commands", None)
    if callable(get_commands_fn):
        try:
            for cmd in get_commands_fn() or []:
                cmd_name = str(getattr(cmd, "name", "") or "").strip()
                if not cmd_name:
                    continue
                if cmd_name not in cmds_dict:
                    desc = getattr(cmd, "help", None) or getattr(cmd, "description", None) or "No description"
                    cmds_dict[cmd_name] = {"desc": str(desc), "subs": []}
                sub_commands = getattr(cmd, "commands", None)
                if isinstance(sub_commands, (list, tuple, set)):
                    for sub in sub_commands:
                        sub_name = str(getattr(sub, "name", "") or "").strip()
                        if sub_name and sub_name not in cmds_dict[cmd_name]["subs"]:
                            cmds_dict[cmd_name]["subs"].append(sub_name)
        except Exception:
            logger.debug("Failed to inspect get_commands() on cog %r", cog, exc_info=True)

    walk_app_fn = getattr(cog, "walk_app_commands", None)
    if callable(walk_app_fn):
        try:
            for app_cmd in walk_app_fn() or []:
                parent_obj = getattr(app_cmd, "parent", None)
                if parent_obj is not None:
                    # Handled via parent's .commands below or attached to root
                    root_name = str(getattr(parent_obj, "name", "") or "").strip()
                    sub_name = str(getattr(app_cmd, "name", "") or "").strip()
                    if root_name and sub_name:
                        if root_name not in cmds_dict:
                            cmds_dict[root_name] = {"desc": "No description", "subs": []}
                        if sub_name not in cmds_dict[root_name]["subs"]:
                            cmds_dict[root_name]["subs"].append(sub_name)
                    continue

                cmd_name = str(getattr(app_cmd, "name", "") or "").strip()
                if not cmd_name:
                    continue
                if cmd_name not in cmds_dict:
                    desc = getattr(app_cmd, "description", None) or "No description"
                    cmds_dict[cmd_name] = {"desc": str(desc), "subs": []}
                sub_commands = getattr(app_cmd, "commands", None)
                if isinstance(sub_commands, (list, tuple, set)):
                    for sub in sub_commands:
                        sub_name = str(getattr(sub, "name", "") or "").strip()
                        if sub_name and sub_name not in cmds_dict[cmd_name]["subs"]:
                            cmds_dict[cmd_name]["subs"].append(sub_name)
        except Exception:
            logger.debug("Failed to inspect walk_app_commands() on cog %r", cog, exc_info=True)

    return cmds_dict


def _resolve_id_badges(
    raw_ids: Any,
    lookup_map: dict[str, str],
    fallback_prefix: str,
) -> list[dict[str, str]]:
    """Resolve a list of channel/role ID strings against live guild channels/roles."""
    badges: list[dict[str, str]] = []
    if not isinstance(raw_ids, list):
        return badges
    for item in raw_ids:
        if not isinstance(item, (str, int)):
            continue
        id_str = str(item).strip()
        if not id_str:
            continue
        name = lookup_map.get(id_str, f"{fallback_prefix}-{id_str}")
        badges.append({"id": id_str, "name": name})
    return badges


async def handle_guild_modules(request: web.Request) -> web.Response:
    """
    GET /modules/{guild_id}
    - Requires `modules.view` (webdash/permissions.py:21).
    - Reads settings fresh from MongoDB (bypassing GuildConfigCache).
    - Reads `disabled_cogs` (bot.py:107, 110, cogs/core.py:745, 803, cogs/dashboard.py:606),
      `command_settings` (cogs/core.py:749-777, 806-829, cogs/dashboard.py:607), and
      read-only `disabled_cmds` (bot.py:108, 113).
    - Exact-case `is_disabled` rule: `bot.py:110` and `cogs/core.py:745, 803` compare
      `cog_name in disabled_cogs` with exact case sensitivity. `is_disabled` is decided
      strictly by `cog_name_str in disabled_cogs`. Case-insensitive matching
      (`PROTECTED_COGS_LOWER`) is used ONLY for hiding enabled protected cogs.
    - Protected-cog recovery rule: excludes `core`, `database`, `dashboard`, `owner`, `jishaku`
      when enabled; if a server already has one of them disabled (exact case, e.g. `"Core"`),
      still shows that cog with a clear "Protected (Re-enable Only)" badge.
    - Exposes ONLY the 5 `command_settings` keys that `cogs/core.py:753-764, 808-818` reads:
      `disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="modules.view",
        active_tab="modules",
        fresh_settings=True,
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    guild = access.guild
    settings = access.settings
    bot = request.app.get(BOT_KEY)

    raw_disabled_cogs = settings.get("disabled_cogs", [])
    disabled_cogs: list[str] = []
    if isinstance(raw_disabled_cogs, list):
        for entry in raw_disabled_cogs:
            if isinstance(entry, str) and entry.strip():
                disabled_cogs.append(entry.strip())

    raw_disabled_cmds = settings.get("disabled_cmds", [])
    disabled_cmds: list[str] = []
    if isinstance(raw_disabled_cmds, list):
        for entry in raw_disabled_cmds:
            if isinstance(entry, str) and entry.strip():
                disabled_cmds.append(entry.strip())

    raw_cmd_settings = settings.get("command_settings", {})
    command_settings: dict[str, dict[str, Any]] = (
        raw_cmd_settings if isinstance(raw_cmd_settings, dict) else {}
    )

    # Build live channel and role lookup maps (cogs/dashboard.py:610-611)
    raw_channels = getattr(guild, "text_channels", None)
    if raw_channels is None:
        raw_channels = getattr(guild, "channels", None) or []
    channel_map: dict[str, str] = {}
    for ch in raw_channels:
        cid = getattr(ch, "id", None)
        cname = getattr(ch, "name", None)
        if cid is not None and cname is not None:
            channel_map[str(cid)] = str(cname)

    raw_roles = getattr(guild, "roles", None) or []
    role_map: dict[str, str] = {}
    for role in raw_roles:
        rid = getattr(role, "id", None)
        rname = getattr(role, "name", None)
        if rid is not None and rname is not None and str(rname) != "@everyone":
            role_map[str(rid)] = str(rname)

    bot_cogs_dict: dict[str, Any] = {}
    if bot is not None and isinstance(getattr(bot, "cogs", None), dict):
        bot_cogs_dict = dict(bot.cogs)

    # Build ordered list of (cog_name, cog_obj). Include unloaded disabled_cogs entries
    # only if they are not wrong-case aliases of loaded or protected cogs.
    cog_entries: list[tuple[str, Any]] = list(bot_cogs_dict.items())
    seen_cog_names = {name for name, _ in cog_entries}
    seen_cog_names_lower = {name.lower() for name, _ in cog_entries}
    for dcog in disabled_cogs:
        if dcog in seen_cog_names or dcog.lower() in seen_cog_names_lower:
            continue
        if dcog.lower() in PROTECTED_COGS_LOWER and dcog not in PROTECTED_COG_CANONICAL_NAMES:
            continue
        cog_entries.append((dcog, None))
        seen_cog_names.add(dcog)
        seen_cog_names_lower.add(dcog.lower())

    modules_view: list[dict[str, Any]] = []
    for cog_name, cog_obj in cog_entries:
        cog_name_str = str(cog_name).strip()
        if not cog_name_str:
            continue
        cog_lower = cog_name_str.lower()
        # Exact, case-sensitive match against disabled_cogs (bot.py:110, cogs/core.py:745, 803)
        is_disabled = cog_name_str in disabled_cogs
        # Case-insensitive match ONLY for identifying protected cogs to hide when enabled
        is_protected = cog_lower in PROTECTED_COGS_LOWER

        # Protected-cog recovery rule:
        # Hide protected cogs (core, database, dashboard, owner, jishaku) when enabled,
        # but ALWAYS show them with a "Protected (Re-enable Only)" badge if disabled!
        if is_protected and not is_disabled:
            continue

        cmds_dict = _extract_cog_commands(cog_obj)
        commands_view: list[dict[str, Any]] = []
        for cmd_name, cmd_meta in cmds_dict.items():
            raw_override = command_settings.get(cmd_name)
            override = raw_override if isinstance(raw_override, dict) else {}

            raw_dis_subs = override.get("disabled_subcommands", [])
            dis_subs_set = (
                {str(s).strip() for s in raw_dis_subs if isinstance(s, str)}
                if isinstance(raw_dis_subs, list)
                else set()
            )

            all_subs: list[str] = list(cmd_meta.get("subs", []))
            for s in dis_subs_set:
                if s and s not in all_subs:
                    all_subs.append(s)

            subcommands_view = [
                {"name": sub_name, "is_disabled": sub_name in dis_subs_set}
                for sub_name in all_subs
            ]

            allowed_ch_badges = _resolve_id_badges(
                override.get("allowed_channels", []), channel_map, "channel"
            )
            ignored_ch_badges = _resolve_id_badges(
                override.get("ignored_channels", []), channel_map, "channel"
            )
            allowed_role_badges = _resolve_id_badges(
                override.get("allowed_roles", []), role_map, "role"
            )
            ignored_role_badges = _resolve_id_badges(
                override.get("ignored_roles", []), role_map, "role"
            )

            commands_view.append(
                {
                    "name": cmd_name,
                    "description": str(cmd_meta.get("desc") or "No description"),
                    "subcommands": subcommands_view,
                    "allowed_channels": allowed_ch_badges,
                    "ignored_channels": ignored_ch_badges,
                    "allowed_roles": allowed_role_badges,
                    "ignored_roles": ignored_role_badges,
                    "allowed_channel_ids": [b["id"] for b in allowed_ch_badges],
                    "ignored_channel_ids": [b["id"] for b in ignored_ch_badges],
                    "allowed_role_ids": [b["id"] for b in allowed_role_badges],
                    "ignored_role_ids": [b["id"] for b in ignored_role_badges],
                }
            )

        modules_view.append(
            {
                "name": cog_name_str,
                "is_disabled": is_disabled,
                "is_protected": is_protected,
                "commands": commands_view,
            }
        )

    guild_channels = [{"id": cid, "name": cname} for cid, cname in channel_map.items()]
    guild_roles = [{"id": rid, "name": rname} for rid, rname in role_map.items()]

    context.update(
        {
            "modules": modules_view,
            "disabled_cogs": disabled_cogs,
            "disabled_cmds": disabled_cmds,
            "dashboard_rev": _extract_dashboard_rev(settings),
            "can_edit_modules": "modules.edit" in access.granted_permissions,
            "can_edit_commands": "commands.edit" in access.granted_permissions,
            "guild_channels": guild_channels,
            "guild_roles": guild_roles,
        }
    )
    return aiohttp_jinja2.render_template("modules.html", request, context)


async def handle_owner_panel(request: web.Request) -> web.Response:
    """
    GET /owner_panel
    - Bot Owner only (resolve_bot_owner_id; redirects to /login when unauthenticated,
      returns 403 when authenticated non-owner).
    - Displays both `bot.global_lockdown` (bot.py:33, 82) and `bot.dashboard_maintenance`
      (cogs/dashboard.py:43, 122) separately read-only, noting legacy /owner_panel
      (cogs/dashboard.py:1761-1881) showed neither.
    - Displays loaded extensions (bot.extensions, cogs/dashboard.py:1762),
      recent `db.visit_logs` (cogs/dashboard.py:1777), and `db.ip_bans` (cogs/dashboard.py:2024)
      with every IP normalized via `normalize_ip` (webdash/security.py:92) and escaped via Jinja2.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return _redirect("/login")

    try:
        user_id = int(session_doc.get("discord_id", 0))
    except (ValueError, TypeError):
        return _redirect("/login")

    bot_owner_id = await resolve_bot_owner_id(request)
    if bot_owner_id is None or user_id != int(bot_owner_id):
        return render_error_page(
            request,
            status_code=403,
            title="Access Denied",
            message="Bot Owner access is required to view the Owner Panel.",
        )

    bot = request.app.get(BOT_KEY)
    global_lockdown = bool(getattr(bot, "global_lockdown", False)) if bot is not None else False
    dashboard_maintenance = (
        bool(getattr(bot, "dashboard_maintenance", False)) if bot is not None else False
    )

    raw_guilds = getattr(bot, "guilds", None) if bot is not None else None
    guild_count = len(raw_guilds) if isinstance(raw_guilds, (list, tuple, set)) else 0

    raw_cogs = getattr(bot, "cogs", None) if bot is not None else None
    cog_count = len(raw_cogs) if isinstance(raw_cogs, dict) else 0

    raw_extensions = getattr(bot, "extensions", None) if bot is not None else None
    extensions_list: list[dict[str, str]] = []
    if isinstance(raw_extensions, dict):
        for ext_key in list(raw_extensions.keys()):
            ext_str = str(ext_key)
            short_str = ext_str[5:] if ext_str.startswith("cogs.") else ext_str
            extensions_list.append({"full_name": ext_str, "short_name": short_str})

    db = get_ready_db(bot)
    visit_logs: list[dict[str, Any]] = []
    if db is not None and getattr(db, "visit_logs", None) is not None:
        try:
            visit_cursor = (
                db.visit_logs.find()
                .sort("last_visit", -1)
                .limit(OWNER_VISIT_LOGS_LIMIT)
            )
            raw_visits = await _fetch_cursor_docs(
                visit_cursor,
                limit=OWNER_VISIT_LOGS_LIMIT,
                timeout=LOGS_DB_TIMEOUT_SECONDS,
            )
            for doc in raw_visits:
                norm_ip = normalize_ip(str(doc.get("ip", "")))
                last_user = str(doc.get("last_user") or "Guest")
                raw_hits = doc.get("hits", 1)
                try:
                    hits_int = int(raw_hits)
                except (ValueError, TypeError):
                    hits_int = 1
                visit_logs.append(
                    {
                        "ip": norm_ip,
                        "last_user": last_user,
                        "hits": hits_int,
                    }
                )
        except Exception:
            logger.exception("Failed to query db.visit_logs in handle_owner_panel")

    ip_bans: list[dict[str, str]] = []
    if db is not None and getattr(db, "ip_bans", None) is not None:
        try:
            bans_cursor = db.ip_bans.find().limit(LOGS_QUERY_LIMIT)
            raw_bans = await _fetch_cursor_docs(
                bans_cursor,
                limit=LOGS_QUERY_LIMIT,
                timeout=LOGS_DB_TIMEOUT_SECONDS,
            )
            for doc in raw_bans:
                norm_ip = normalize_ip(str(doc.get("ip", "")))
                ip_bans.append({"ip": norm_ip})
        except Exception:
            logger.exception("Failed to query db.ip_bans in handle_owner_panel")

    context = {
        "bot_avatar": get_bot_avatar_url(bot),
        "csrf_token": str(session_doc.get("csrf_token") or ""),
        "current_ip": normalize_ip(request.get(CLIENT_IP_KEY, "invalid")),
        "guild_count": guild_count,
        "cog_count": cog_count,
        "global_lockdown": global_lockdown,
        "dashboard_maintenance": dashboard_maintenance,
        "extensions": extensions_list,
        "visit_logs": visit_logs,
        "ip_bans": ip_bans,
    }
    return aiohttp_jinja2.render_template("owner_panel.html", request, context)


async def handle_owner_action(request: web.Request) -> web.Response:
    """
    POST /api/owner/action
    Checkpoint 3C Batch 3 Bot Owner endpoint (`reload_cog` only):
    - Requires active session (401) and Bot Owner identity via `resolve_bot_owner_id` (403).
    - Restricts `cog` to `^[a-z0-9_]{1,32}$` (after stripping optional `"cogs."` prefix).
    - Rejects `"dashboard"` and `"database"` (instructs the operator to use the Discord owner command).
    - Verifies `"cogs.<name>" in bot.extensions` before calling `await bot.reload_extension(ext_name)`.
    - Records an `AuditLogger` entry with `guild_id=0`, `module="owner"`, `action="reload_cog"`,
      and non-empty `changes={"cog": {"old": ext_name, "new": ext_name}}`.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return web.json_response({"error": "Authentication required."}, status=401)

    try:
        user_id = int(session_doc.get("discord_id"))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid session."}, status=401)

    bot_owner_id = await resolve_bot_owner_id(request)
    if bot_owner_id is None or user_id != int(bot_owner_id):
        return web.json_response(
            {"error": "Forbidden: Bot Owner access required."},
            status=403,
        )

    try:
        raw_bytes = await request.read()
    except Exception:
        return web.json_response({"error": "Failed to read request body."}, status=400)

    if not raw_bytes or len(raw_bytes) > MAX_JSON_BODY_BYTES:
        return web.json_response(
            {"error": "Request body must be a non-empty JSON object within 64 KiB."},
            status=400,
        )

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object."},
            status=400,
        )

    allowed_keys = {"action", "cog", "csrf_token"}
    if any(k not in allowed_keys for k in body):
        return web.json_response(
            {"error": "Unexpected keys in owner action payload."},
            status=400,
        )

    action = body.get("action")
    if not isinstance(action, str) or action != "reload_cog":
        return web.json_response(
            {"error": "Unsupported owner action. Only 'reload_cog' is permitted."},
            status=400,
        )

    raw_cog = body.get("cog")
    if not isinstance(raw_cog, str):
        return web.json_response(
            {"error": "Field 'cog' must be a string."},
            status=400,
        )

    short_cog = raw_cog[5:] if raw_cog.startswith("cogs.") else raw_cog
    if short_cog in DISALLOWED_WEB_RELOAD_COGS:
        return web.json_response(
            {
                "error": (
                    f"Extension 'cogs.{short_cog}' cannot be reloaded from the web dashboard. "
                    "Please use the Discord owner command instead."
                )
            },
            status=400,
        )

    if not OWNER_RELOAD_COG_RE.fullmatch(short_cog):
        return web.json_response(
            {"error": "Invalid cog name format (must match ^[a-z0-9_]{1,32}$)."},
            status=400,
        )

    ext_name = f"cogs.{short_cog}"
    bot = request.app.get(BOT_KEY)
    raw_extensions = getattr(bot, "extensions", None) if bot is not None else None
    if not isinstance(raw_extensions, dict) or ext_name not in raw_extensions:
        return web.json_response(
            {"error": f"Extension '{ext_name}' is not currently loaded."},
            status=400,
        )

    reload_fn = getattr(bot, "reload_extension", None)
    if not callable(reload_fn):
        return web.json_response(
            {"error": "Bot does not support extension reloading."},
            status=500,
        )

    reload_task: asyncio.Task[Any] = asyncio.create_task(reload_fn(ext_name))

    def _log_late_reload_error(done_task: asyncio.Task[Any]) -> None:
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            logger.error(
                "Background reload of %s failed after HTTP timeout",
                ext_name,
                exc_info=exc,
            )

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.shield(reload_task),
            timeout=OWNER_RELOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        timed_out = True
        reload_task.add_done_callback(_log_late_reload_error)
    except Exception:
        logger.exception("Failed to reload extension %s via handle_owner_action", ext_name)
        return web.json_response(
            {"error": f"Failed to reload extension '{ext_name}'."},
            status=500,
        )

    audit_logger: AuditLogger | None = request.app.get(AUDIT_LOGGER_KEY)
    if audit_logger is not None:
        actor_name = str(session_doc.get("username") or user_id)
        await audit_logger.record(
            guild_id=0,
            actor_id=user_id,
            actor_name=actor_name,
            module="owner",
            action="reload_cog",
            changes={"cog": {"old": ext_name, "new": ext_name}},
            before_snapshot={"cog": ext_name},
            client_ip=request.get(CLIENT_IP_KEY, "invalid"),
        )

    if timed_out:
        return web.json_response(
            {"status": "still running", "reloaded": ext_name},
            status=202,
        )

    return web.json_response(
        {"status": "ok", "reloaded": ext_name},
        status=200,
    )


async def handle_owner_ip_action(request: web.Request) -> web.Response:
    """
    POST /api/owner/ip_action
    Checkpoint 3C Batch 3 Bot Owner endpoint (`ban` / `unban` on `db.ip_bans`):
    - Requires active session (401) and Bot Owner identity via `resolve_bot_owner_id` (403).
    - Validates IP via `normalize_ip(raw_ip)` and rejects `'invalid'` with 400.
    - Self-ban protection: `ban` refuses when `target_ip == normalize_ip(request[CLIENT_IP_KEY])`.
    - Performs a STRICT read on `db.ip_bans` before any write; returns 503 if DB is unavailable
      or the read/write fails.
    - Skips the audit row and returns a clear message (`status="noop"`) when nothing changes
      (banning an already-banned IP, or unbanning an IP that is not banned).
    - Immediately updates and refreshes `IpBanCache` on both `ban` and `unban`.
    - Records an `AuditLogger` entry with `guild_id=0`, `module="owner"`, `action="ban_ip"` /
      `"unban_ip"`, and non-empty `changes` when the ban state changes.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return web.json_response({"error": "Authentication required."}, status=401)

    try:
        user_id = int(session_doc.get("discord_id"))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid session."}, status=401)

    bot_owner_id = await resolve_bot_owner_id(request)
    if bot_owner_id is None or user_id != int(bot_owner_id):
        return web.json_response(
            {"error": "Forbidden: Bot Owner access required."},
            status=403,
        )

    try:
        raw_bytes = await request.read()
    except Exception:
        return web.json_response({"error": "Failed to read request body."}, status=400)

    if not raw_bytes or len(raw_bytes) > MAX_JSON_BODY_BYTES:
        return web.json_response(
            {"error": "Request body must be a non-empty JSON object within 64 KiB."},
            status=400,
        )

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object."},
            status=400,
        )

    allowed_keys = {"action", "ip", "csrf_token"}
    if any(k not in allowed_keys for k in body):
        return web.json_response(
            {"error": "Unexpected keys in IP action payload."},
            status=400,
        )

    action = body.get("action")
    if not isinstance(action, str) or action not in ("ban", "unban"):
        return web.json_response(
            {"error": "Field 'action' must be 'ban' or 'unban'."},
            status=400,
        )

    raw_ip = body.get("ip")
    if not isinstance(raw_ip, str):
        return web.json_response(
            {"error": "Field 'ip' must be a string."},
            status=400,
        )

    target_ip = normalize_ip(raw_ip)
    if target_ip == "invalid":
        return web.json_response(
            {"error": "Invalid IPv4 or IPv6 address."},
            status=400,
        )

    requester_ip = normalize_ip(request.get(CLIENT_IP_KEY, "invalid"))
    if action == "ban" and requester_ip != "invalid" and target_ip == requester_ip:
        return web.json_response(
            {"error": "Cannot ban your own current IP address."},
            status=400,
        )

    bot = request.app.get(BOT_KEY)
    db = get_ready_db(bot)
    if db is None or getattr(db, "ip_bans", None) is None:
        return web.json_response(
            {"error": "Database temporarily unavailable, please retry"},
            status=503,
        )

    # Strict pre-write read on db.ip_bans: distinguish missing document (None) from read failure (503)
    find_one_fn = getattr(db.ip_bans, "find_one", None)
    if not callable(find_one_fn):
        return web.json_response(
            {"error": "Database temporarily unavailable, please retry"},
            status=503,
        )
    try:
        existing_ban = await asyncio.wait_for(
            find_one_fn({"ip": target_ip}),
            timeout=LOGS_DB_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Strict read on db.ip_bans failed for ip=%s", target_ip)
        return web.json_response(
            {"error": "Database temporarily unavailable, please retry"},
            status=503,
        )

    ip_ban_cache: IpBanCache | None = request.app.get(IP_BAN_CACHE_KEY)

    if action == "ban":
        if existing_ban is not None:
            if ip_ban_cache is not None:
                ip_ban_cache.add_local(target_ip)
            return web.json_response(
                {
                    "status": "noop",
                    "action": "ban",
                    "ip": target_ip,
                    "message": f"IP {target_ip} is already banned.",
                },
                status=200,
            )

        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        try:
            await asyncio.wait_for(
                db.ip_bans.update_one(
                    {"ip": target_ip},
                    {"$set": {"ip": target_ip, "banned_at": now_ts}},
                    upsert=True,
                ),
                timeout=LOGS_DB_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to write IP ban for ip=%s", target_ip)
            return web.json_response(
                {"error": "Database temporarily unavailable, please retry"},
                status=503,
            )

        if ip_ban_cache is not None:
            ip_ban_cache.add_local(target_ip)
            await ip_ban_cache.refresh(force=True)

        audit_action = "ban_ip"
        audit_changes: dict[str, dict[str, Any]] = {
            "ip": {"old": None, "new": target_ip},
        }
        before_snap: dict[str, Any] = {}
    else:
        if existing_ban is None:
            if ip_ban_cache is not None:
                ip_ban_cache.remove_local(target_ip)
            return web.json_response(
                {
                    "status": "noop",
                    "action": "unban",
                    "ip": target_ip,
                    "message": f"IP {target_ip} is not currently banned.",
                },
                status=200,
            )

        try:
            del_res = await asyncio.wait_for(
                db.ip_bans.delete_one({"ip": target_ip}),
                timeout=LOGS_DB_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to delete IP ban for ip=%s", target_ip)
            return web.json_response(
                {"error": "Database temporarily unavailable, please retry"},
                status=503,
            )

        if ip_ban_cache is not None:
            ip_ban_cache.remove_local(target_ip)
            await ip_ban_cache.refresh(force=True)

        deleted_count = getattr(del_res, "deleted_count", 1)
        if isinstance(deleted_count, int) and deleted_count == 0:
            return web.json_response(
                {
                    "status": "noop",
                    "action": "unban",
                    "ip": target_ip,
                    "message": f"IP {target_ip} is not currently banned.",
                },
                status=200,
            )

        audit_action = "unban_ip"
        audit_changes = {
            "ip": {"old": target_ip, "new": None},
        }
        before_snap = {"ip": target_ip}

    audit_logger: AuditLogger | None = request.app.get(AUDIT_LOGGER_KEY)
    if audit_logger is not None:
        actor_name = str(session_doc.get("username") or user_id)
        await audit_logger.record(
            guild_id=0,
            actor_id=user_id,
            actor_name=actor_name,
            module="owner",
            action=audit_action,
            changes=audit_changes,
            before_snapshot=before_snap,
            client_ip=request.get(CLIENT_IP_KEY, "invalid"),
        )

    return web.json_response(
        {"status": "ok", "action": action, "ip": target_ip},
        status=200,
    )


def _invalidate_guild_caches(request: web.Request, guild_id: int) -> None:
    """
    Invalidate both `GuildConfigCache` (webdash/cache.py:153-163, which also invokes
    `ai_cog._invalidate_guild_cfg(guild_id)`) and fallback directly to the AI cog's
    `_invalidate_guild_cfg(guild_id)` (cogs/ai.py:589-590) if `cfg_cache` is absent.
    """
    cfg_cache: GuildConfigCache | None = request.app.get(GUILD_CONFIG_CACHE_KEY)
    if cfg_cache is not None:
        cfg_cache.invalidate(int(guild_id))
        return

    bot = request.app.get(BOT_KEY)
    get_cog_fn = getattr(bot, "get_cog", None) if bot is not None else None
    if callable(get_cog_fn):
        try:
            ai_cog = get_cog_fn("AI")
            inv_fn = getattr(ai_cog, "_invalidate_guild_cfg", None) if ai_cog is not None else None
            if callable(inv_fn):
                inv_fn(int(guild_id))
        except Exception:
            logger.warning("Failed to call ai_cog._invalidate_guild_cfg(%s)", guild_id, exc_info=True)


async def _apply_guild_settings_mutation(
    request: web.Request,
    *,
    guild_id: int,
    user_id: int,
    actor_name: str,
    expected_rev: int,
    before_doc: dict[str, Any] | None,
    updates: dict[str, Any],
    audit_module: str,
    audit_action: str,
) -> web.Response:
    """
    Execute a conditional optimistic-concurrency write on `db.guild_settings` using
    `dashboard_rev` (missing = 0), `$set` only whitelisted keys, `$inc` `dashboard_rev` by 1,
    invalidate `GuildConfigCache` + `ai_cog._invalidate_guild_cfg`, and record an
    `AuditLogger` entry with a non-empty `changes` map.
    """
    bot = request.app.get(BOT_KEY)
    db = get_ready_db(bot)
    if db is None or getattr(db, "guild_settings", None) is None:
        return web.json_response(
            {"error": "Database temporarily unavailable, please retry"},
            status=503,
        )

    current_rev = _extract_dashboard_rev(before_doc)
    if current_rev != expected_rev:
        return web.json_response(
            {"error": "Someone else changed these settings, please reload the page."},
            status=409,
        )

    if expected_rev == 0:
        filter_query: dict[str, Any] = {
            "guild_id": int(guild_id),
            "$or": [
                {"dashboard_rev": 0},
                {"dashboard_rev": {"$exists": False}},
            ],
        }
    else:
        filter_query = {
            "guild_id": int(guild_id),
            "dashboard_rev": int(expected_rev),
        }

    set_payload = dict(updates)
    upsert_flag = False
    if before_doc is None and expected_rev == 0:
        set_payload["guild_id"] = int(guild_id)
        upsert_flag = True

    update_doc: dict[str, Any] = {
        "$set": set_payload,
        "$inc": {"dashboard_rev": 1},
    }

    try:
        res = await asyncio.wait_for(
            db.guild_settings.update_one(filter_query, update_doc, upsert=upsert_flag),
            timeout=LOGS_DB_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if "duplicate key" in str(exc).lower() or "E11000" in str(exc):
            return web.json_response(
                {"error": "Someone else changed these settings, please reload the page."},
                status=409,
            )
        logger.exception("Failed to update guild_settings for guild_id=%s", guild_id)
        return web.json_response(
            {"error": "Database write failed."},
            status=503,
        )

    matched = int(getattr(res, "matched_count", 0) or 0)
    upserted = getattr(res, "upserted_id", None) is not None
    if matched == 0 and not upserted:
        return web.json_response(
            {"error": "Someone else changed these settings, please reload the page."},
            status=409,
        )

    new_rev = expected_rev + 1
    _invalidate_guild_caches(request, int(guild_id))

    audit_logger: AuditLogger | None = request.app.get(AUDIT_LOGGER_KEY)
    if audit_logger is not None:
        before_map = copy.deepcopy(before_doc) if isinstance(before_doc, dict) else {}
        after_map = copy.deepcopy(before_map)
        after_map.update(copy.deepcopy(updates))
        changes = compute_diff(before_map, after_map, keys=list(updates.keys()))
        if not changes:
            changes = {
                k: {
                    "old": copy.deepcopy(before_map.get(k)),
                    "new": copy.deepcopy(v),
                }
                for k, v in updates.items()
            }
        await audit_logger.record(
            guild_id=int(guild_id),
            actor_id=int(user_id),
            actor_name=actor_name,
            module=audit_module,
            action=audit_action,
            changes=changes,
            before_snapshot=before_map,
            client_ip=request.get(CLIENT_IP_KEY, "invalid"),
        )

    return web.json_response(
        {
            "status": "ok",
            "dashboard_rev": new_rev,
        },
        status=200,
    )


async def handle_api_settings(request: web.Request) -> web.Response:
    """
    POST /api/settings/{guild_id}
    Checkpoint 3C Batch 1 mutation endpoint supporting:
    1. `type == "update_automod"` (requires `automod.edit`, webdash/permissions.py:34):
       writes `banned_words` (`list[str]` of lowercased strings, max 500 entries of <=64 chars,
       read at bot.py:191-193, cogs/ai.py:593-595, cogs/admin.py:907; refuses save if stored
       list has >500 entries so display cap cannot cause truncation).
    2. `type == "update_ai_model"` (requires `ai.edit`, webdash/permissions.py:47):
       writes `default_ai_model` (`str` in {"auto", "deepseek", "groq", "gemini"},
       read at cogs/ai.py:77-91, 291-299, 401-411, 657, cogs/admin.py:256, 842).
    3. `type == "toggle"` with `module == "toggleMisc"` (requires `settings.edit`, webdash/permissions.py:17):
       writes `misc_enabled` (`bool`, read at cogs/misc.py:174, 538, cogs/admin.py:246).
    4. `type == "toggle_lockdown"` (requires `lockdown.execute`, webdash/permissions.py:38):
       returns HTTP 501 Not Implemented with zero DB writes.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        user_id = int(session_doc["discord_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Unauthorized"}, status=401)

    actor_name = str(session_doc.get("username") or "User")

    raw_guild_id = request.match_info.get("guild_id", "")
    try:
        guild_id = int(raw_guild_id)
        if guild_id <= 0:
            raise ValueError("non-positive guild_id")
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid server ID."}, status=400)

    content_len = request.content_length
    if content_len is not None and content_len > MAX_JSON_BODY_BYTES:
        return web.json_response({"error": "Payload too large."}, status=400)

    try:
        raw_bytes = await request.read()
    except Exception:
        return web.json_response({"error": "Failed to read request body."}, status=400)

    if len(raw_bytes) > MAX_JSON_BODY_BYTES:
        return web.json_response({"error": "Payload too large."}, status=400)

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return web.json_response({"error": "Invalid JSON payload."}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "JSON payload must be an object."}, status=400)

    action_type = body.get("type")
    if not isinstance(action_type, str) or action_type not in (
        "update_automod",
        "update_ai_model",
        "toggle",
        "toggle_lockdown",
    ):
        return web.json_response({"error": "Unsupported or missing settings action type."}, status=400)

    # 1. Lockdown -> 501 Not Implemented
    if action_type == "toggle_lockdown":
        access = await resolve_guild_access(request, guild_id, user_id, "lockdown.execute")
        if access.guild is None:
            return web.json_response({"error": "Bot is not in this server."}, status=404)
        if not access.allowed:
            return web.json_response(
                {"error": "Forbidden: Missing lockdown.execute permission."},
                status=403,
            )
        return web.json_response(
            {
                "error": "Lockdown execution is disabled in Checkpoint 3C pending safe permission backup/restore parity."
            },
            status=501,
        )

    # Validate dashboard_rev for all write actions
    raw_rev = body.get("dashboard_rev")
    if not isinstance(raw_rev, int) or isinstance(raw_rev, bool) or raw_rev < 0:
        return web.json_response(
            {"error": "Invalid or missing dashboard_rev (must be a non-negative integer)."},
            status=400,
        )
    expected_rev = int(raw_rev)

    # 2. AutoMod banned_words
    if action_type == "update_automod":
        allowed_keys = {"type", "banned_words", "dashboard_rev", "csrf_token"}
        if any(k not in allowed_keys for k in body):
            return web.json_response({"error": "Unexpected fields in update_automod payload."}, status=400)

        access = await resolve_guild_access(
            request,
            guild_id,
            user_id,
            "automod.edit",
            fresh_settings=True,
            strict_settings_read=True,
        )
        if access.guild is None:
            return web.json_response({"error": "Bot is not in this server."}, status=404)
        if access.settings_read_failed or access.status_code == 503:
            return web.json_response(
                {"error": "Database temporarily unavailable, please retry"},
                status=503,
            )
        if not access.allowed:
            return web.json_response(
                {"error": "Forbidden: Missing automod.edit permission."},
                status=403,
            )

        raw_words = body.get("banned_words")
        if not isinstance(raw_words, list):
            return web.json_response({"error": "banned_words must be a list of strings."}, status=400)
        if len(raw_words) > MAX_BANNED_WORDS_ENTRIES:
            return web.json_response(
                {"error": f"banned_words cannot exceed {MAX_BANNED_WORDS_ENTRIES} entries."},
                status=400,
            )

        cleaned_words: list[str] = []
        seen_words: set[str] = set()
        for item in raw_words:
            if not isinstance(item, str):
                return web.json_response(
                    {"error": "Every banned_words entry must be a string."},
                    status=400,
                )
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                return web.json_response(
                    {"error": "Banned words cannot contain control characters."},
                    status=400,
                )
            cleaned = item.strip().lower()
            if not cleaned:
                return web.json_response(
                    {"error": "Banned words cannot contain empty entries."},
                    status=400,
                )
            if len(cleaned) > MAX_BANNED_WORD_LENGTH:
                return web.json_response(
                    {"error": f"Each banned word must be at most {MAX_BANNED_WORD_LENGTH} characters."},
                    status=400,
                )
            if cleaned not in seen_words:
                seen_words.add(cleaned)
                cleaned_words.append(cleaned)

        before_doc = access.settings_doc
        if isinstance(before_doc, dict):
            stored_raw = before_doc.get("banned_words", [])
            if isinstance(stored_raw, list):
                stored_non_empty = [
                    str(x).strip()
                    for x in stored_raw
                    if isinstance(x, (str, int, float)) and str(x).strip()
                ]
                if len(stored_non_empty) > MAX_BANNED_WORDS_DISPLAY:
                    return web.json_response(
                        {
                            "error": "Stored banned_words exceeds 500 entries; web editing is disabled so saving cannot truncate unseen entries."
                        },
                        status=400,
                    )

        return await _apply_guild_settings_mutation(
            request,
            guild_id=guild_id,
            user_id=user_id,
            actor_name=actor_name,
            expected_rev=expected_rev,
            before_doc=before_doc,
            updates={"banned_words": cleaned_words},
            audit_module="automod",
            audit_action="update_banned_words",
        )

    # 3. Default AI Model
    if action_type == "update_ai_model":
        allowed_keys = {"type", "model", "dashboard_rev", "csrf_token"}
        if any(k not in allowed_keys for k in body):
            return web.json_response(
                {"error": "Unexpected fields in update_ai_model payload."},
                status=400,
            )

        access = await resolve_guild_access(
            request,
            guild_id,
            user_id,
            "ai.edit",
            fresh_settings=True,
            strict_settings_read=True,
        )
        if access.guild is None:
            return web.json_response({"error": "Bot is not in this server."}, status=404)
        if access.settings_read_failed or access.status_code == 503:
            return web.json_response(
                {"error": "Database temporarily unavailable, please retry"},
                status=503,
            )
        if not access.allowed:
            return web.json_response(
                {"error": "Forbidden: Missing ai.edit permission."},
                status=403,
            )

        model_val = body.get("model")
        if not isinstance(model_val, str) or model_val not in VALID_AI_MODELS:
            return web.json_response(
                {"error": "Invalid AI model. Allowed values: auto, deepseek, groq, gemini."},
                status=400,
            )

        before_doc = access.settings_doc
        return await _apply_guild_settings_mutation(
            request,
            guild_id=guild_id,
            user_id=user_id,
            actor_name=actor_name,
            expected_rev=expected_rev,
            before_doc=before_doc,
            updates={"default_ai_model": model_val},
            audit_module="ai",
            audit_action="update_ai_model",
        )

    # 4. Toggle Misc (`module == "toggleMisc"`)
    allowed_keys = {"type", "module", "enabled", "dashboard_rev", "csrf_token"}
    if any(k not in allowed_keys for k in body):
        return web.json_response({"error": "Unexpected fields in toggle payload."}, status=400)

    module_name = body.get("module")
    if not isinstance(module_name, str) or module_name != "toggleMisc":
        return web.json_response({"error": "Unsupported toggle module."}, status=400)

    access = await resolve_guild_access(
        request,
        guild_id,
        user_id,
        "settings.edit",
        fresh_settings=True,
        strict_settings_read=True,
    )
    if access.guild is None:
        return web.json_response({"error": "Bot is not in this server."}, status=404)
    if access.settings_read_failed or access.status_code == 503:
        return web.json_response(
            {"error": "Database temporarily unavailable, please retry"},
            status=503,
        )
    if not access.allowed:
        return web.json_response(
            {"error": "Forbidden: Missing settings.edit permission."},
            status=403,
        )

    enabled_val = body.get("enabled")
    if not isinstance(enabled_val, bool):
        return web.json_response({"error": "enabled must be a boolean."}, status=400)

    before_doc = access.settings_doc
    return await _apply_guild_settings_mutation(
        request,
        guild_id=guild_id,
        user_id=user_id,
        actor_name=actor_name,
        expected_rev=expected_rev,
        before_doc=before_doc,
        updates={"misc_enabled": enabled_val},
        audit_module="misc",
        audit_action="toggle_misc",
    )


async def handle_api_modules(request: web.Request) -> web.Response:
    """
    POST /api/modules/{guild_id}
    Checkpoint 3C Batch 2 mutation endpoint supporting:
    1. `disabled_cogs` (requires `modules.edit`, webdash/permissions.py:22):
       - `list[str]` of exact-case loaded cog names (`bot.cogs`, read at bot.py:107, 110
         and cogs/core.py:745, 803) or already-stored non-protected disabled_cogs entries.
       - Protected cogs (`core`, `database`, `dashboard`, `owner`, `jishaku` —
         `PROTECTED_COGS_LOWER`) can NEVER be added to `disabled_cogs` (only kept if
         already present in canonical form or removed to re-enable).
    2. `command_settings` (requires `commands.edit`, webdash/permissions.py:24):
       - Per-command merge into stored `command_settings` (`dict[str, dict[str, list[str]]]`,
         read at cogs/core.py:749-777, 806-829) — never replaces the whole dict.
       - Commands absent from the payload keep their stored settings (including commands
         from protected/hidden cogs and stale/unloaded ones).
       - An entry in the payload whose 5 lists (`disabled_subcommands`, `allowed_channels`,
         `ignored_channels`, `allowed_roles`, `ignored_roles`) are all empty removes ONLY
         that command from `command_settings`.
       - Channel IDs and Role IDs are validated against live guild channels/roles (excluding
         `@everyone`) and stored as STRINGS (`cogs/core.py:761-764, 815-818`).
       - Never writes a non-dict.
    3. Enforces strict fresh MongoDB read (`503` on read failure before any write),
       `dashboard_rev` optimistic concurrency (`409` on mismatch), `GuildConfigCache` +
       `ai_cog._invalidate_guild_cfg` invalidation, and `AuditLogger.record` with non-empty `changes`.
    """
    session_doc = request.get(SESSION_DATA_KEY)
    if not session_doc:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        user_id = int(session_doc["discord_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Unauthorized"}, status=401)

    actor_name = str(session_doc.get("username") or "User")

    raw_guild_id = request.match_info.get("guild_id", "")
    try:
        guild_id = int(raw_guild_id)
        if guild_id <= 0:
            raise ValueError("non-positive guild_id")
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid server ID."}, status=400)

    content_len = request.content_length
    if content_len is not None and content_len > MAX_JSON_BODY_BYTES:
        return web.json_response({"error": "Payload too large."}, status=400)

    try:
        raw_bytes = await request.read()
    except Exception:
        return web.json_response({"error": "Failed to read request body."}, status=400)

    if len(raw_bytes) > MAX_JSON_BODY_BYTES:
        return web.json_response({"error": "Payload too large."}, status=400)

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return web.json_response({"error": "Invalid JSON payload."}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "JSON payload must be an object."}, status=400)

    allowed_top_keys = {"disabled_cogs", "command_settings", "dashboard_rev", "csrf_token"}
    if any(k not in allowed_top_keys for k in body):
        return web.json_response(
            {"error": "Unexpected fields in modules payload."},
            status=400,
        )

    has_disabled_cogs = "disabled_cogs" in body
    has_command_settings = "command_settings" in body
    if not has_disabled_cogs and not has_command_settings:
        return web.json_response(
            {"error": "Payload must include disabled_cogs and/or command_settings."},
            status=400,
        )

    raw_rev = body.get("dashboard_rev")
    if not isinstance(raw_rev, int) or isinstance(raw_rev, bool) or raw_rev < 0:
        return web.json_response(
            {"error": "Invalid or missing dashboard_rev (must be a non-negative integer)."},
            status=400,
        )
    expected_rev = int(raw_rev)

    primary_perm = "modules.edit" if has_disabled_cogs else "commands.edit"
    access = await resolve_guild_access(
        request,
        guild_id,
        user_id,
        primary_perm,
        fresh_settings=True,
        strict_settings_read=True,
    )
    if access.guild is None:
        return web.json_response({"error": "Bot is not in this server."}, status=404)
    if access.settings_read_failed or access.status_code == 503:
        return web.json_response(
            {"error": "Database temporarily unavailable, please retry"},
            status=503,
        )
    if has_disabled_cogs and "modules.edit" not in access.granted_permissions:
        return web.json_response(
            {"error": "Forbidden: Missing modules.edit permission."},
            status=403,
        )
    if has_command_settings and "commands.edit" not in access.granted_permissions:
        return web.json_response(
            {"error": "Forbidden: Missing commands.edit permission."},
            status=403,
        )

    before_doc = access.settings_doc
    guild = access.guild
    bot = request.app.get(BOT_KEY)
    bot_cogs_dict: dict[str, Any] = {}
    if bot is not None and isinstance(getattr(bot, "cogs", None), dict):
        bot_cogs_dict = dict(bot.cogs)

    updates: dict[str, Any] = {}

    # 1. Validate `disabled_cogs`
    if has_disabled_cogs:
        raw_disabled_cogs = body.get("disabled_cogs")
        if not isinstance(raw_disabled_cogs, list):
            return web.json_response(
                {"error": "disabled_cogs must be a list of strings."},
                status=400,
            )
        if len(raw_disabled_cogs) > MAX_DISABLED_COGS_ENTRIES:
            return web.json_response(
                {"error": f"disabled_cogs cannot exceed {MAX_DISABLED_COGS_ENTRIES} entries."},
                status=400,
            )

        loaded_cog_names: set[str] = {
            str(k).strip() for k in bot_cogs_dict.keys() if str(k).strip()
        }
        loaded_cog_names_lower: set[str] = {n.lower() for n in loaded_cog_names}

        stored_disabled_raw = (
            before_doc.get("disabled_cogs", []) if isinstance(before_doc, dict) else []
        )
        existing_disabled_set: set[str] = (
            {str(x).strip() for x in stored_disabled_raw if isinstance(x, str) and x.strip()}
            if isinstance(stored_disabled_raw, list)
            else set()
        )

        cleaned_disabled_cogs: list[str] = []
        seen_disabled: set[str] = set()
        for item in raw_disabled_cogs:
            if not isinstance(item, str):
                return web.json_response(
                    {"error": "Every disabled_cogs entry must be a string."},
                    status=400,
                )
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                return web.json_response(
                    {"error": "Cog names cannot contain control characters."},
                    status=400,
                )
            cleaned_cog = item.strip()
            if not cleaned_cog or len(cleaned_cog) > 64:
                return web.json_response(
                    {"error": "Each cog name must be 1 to 64 characters."},
                    status=400,
                )

            # Protected cogs can NEVER be added to disabled_cogs
            if cleaned_cog.lower() in PROTECTED_COGS_LOWER:
                if (
                    cleaned_cog not in existing_disabled_set
                    or cleaned_cog not in PROTECTED_COG_CANONICAL_NAMES
                ):
                    return web.json_response(
                        {"error": f"Protected module '{cleaned_cog}' cannot be added to disabled_cogs."},
                        status=400,
                    )
            elif cleaned_cog in loaded_cog_names:
                pass
            elif (
                cleaned_cog in existing_disabled_set
                and cleaned_cog.lower() not in loaded_cog_names_lower
            ):
                pass
            else:
                return web.json_response(
                    {
                        "error": f"Unknown or wrong-case module '{cleaned_cog}' (module names are exact-case)."
                    },
                    status=400,
                )

            if cleaned_cog not in seen_disabled:
                seen_disabled.add(cleaned_cog)
                cleaned_disabled_cogs.append(cleaned_cog)

        updates["disabled_cogs"] = cleaned_disabled_cogs

    # 2. Validate & per-command merge `command_settings`
    if has_command_settings:
        raw_cmd_settings = body.get("command_settings")
        if not isinstance(raw_cmd_settings, dict):
            return web.json_response(
                {"error": "command_settings must be a JSON object."},
                status=400,
            )
        if len(raw_cmd_settings) > MAX_COMMAND_SETTINGS_ENTRIES:
            return web.json_response(
                {"error": f"command_settings cannot exceed {MAX_COMMAND_SETTINGS_ENTRIES} commands."},
                status=400,
            )

        # Build configurable command -> valid subcommands catalog from non-protected loaded cogs
        configurable_commands: dict[str, set[str]] = {}
        for cog_name, cog_obj in bot_cogs_dict.items():
            cog_name_str = str(cog_name).strip()
            if not cog_name_str or cog_name_str.lower() in PROTECTED_COGS_LOWER:
                continue
            cmds_dict = _extract_cog_commands(cog_obj)
            for cname, cmeta in cmds_dict.items():
                subs_list = cmeta.get("subs", [])
                configurable_commands.setdefault(cname, set()).update(
                    str(s).strip()
                    for s in subs_list
                    if isinstance(s, str) and str(s).strip()
                )

        # Build live guild channel IDs and role IDs (excluding @everyone) as STRINGS
        raw_channels = getattr(guild, "text_channels", None)
        if raw_channels is None:
            raw_channels = getattr(guild, "channels", None) or []
        valid_channel_ids: set[str] = set()
        for ch in raw_channels:
            cid = getattr(ch, "id", None)
            cname = getattr(ch, "name", None)
            if cid is not None and cname is not None:
                valid_channel_ids.add(str(cid).strip())

        raw_roles = getattr(guild, "roles", None) or []
        valid_role_ids: set[str] = set()
        for role in raw_roles:
            rid = getattr(role, "id", None)
            rname = getattr(role, "name", None)
            if (
                rid is not None
                and rname is not None
                and str(rname) != "@everyone"
                and str(rid).strip() != str(guild_id)
            ):
                valid_role_ids.add(str(rid).strip())

        # Start from stored command_settings so commands absent from the payload
        # (including protected/hidden cog commands and stale ones) keep their stored settings!
        stored_cmd_raw = (
            before_doc.get("command_settings", {}) if isinstance(before_doc, dict) else {}
        )
        merged_cmd_settings: dict[str, dict[str, Any]] = {}
        if isinstance(stored_cmd_raw, dict):
            for existing_k, existing_v in stored_cmd_raw.items():
                if (
                    isinstance(existing_k, str)
                    and existing_k.strip()
                    and isinstance(existing_v, dict)
                ):
                    merged_cmd_settings[existing_k.strip()] = copy.deepcopy(existing_v)

        for raw_cmd_name, raw_cmd_cfg in raw_cmd_settings.items():
            if not isinstance(raw_cmd_name, str):
                return web.json_response(
                    {"error": "Command names in command_settings must be strings."},
                    status=400,
                )
            cmd_name = raw_cmd_name.strip()
            if not cmd_name or cmd_name not in configurable_commands:
                return web.json_response(
                    {"error": f"Unknown or protected command: '{raw_cmd_name}'."},
                    status=400,
                )
            if not isinstance(raw_cmd_cfg, dict):
                return web.json_response(
                    {"error": f"Settings for command '{cmd_name}' must be a JSON object."},
                    status=400,
                )
            if not raw_cmd_cfg or any(k not in VALID_COMMAND_SETTING_KEYS for k in raw_cmd_cfg):
                return web.json_response(
                    {"error": f"Invalid or unsupported keys in command_settings for '{cmd_name}'."},
                    status=400,
                )

            existing_entry = merged_cmd_settings.get(cmd_name)
            existing_dict = existing_entry if isinstance(existing_entry, dict) else {}

            valid_subs_for_cmd = set(configurable_commands.get(cmd_name, set()))
            # Also permit keeping an already-stored disabled_subcommand on this command
            prev_dis_subs = existing_dict.get("disabled_subcommands", [])
            if isinstance(prev_dis_subs, list):
                for s in prev_dis_subs:
                    if isinstance(s, str) and s.strip():
                        valid_subs_for_cmd.add(s.strip())

            normalized_entry: dict[str, list[str]] = {}
            for list_key in VALID_COMMAND_SETTING_KEYS:
                if list_key in raw_cmd_cfg:
                    raw_list = raw_cmd_cfg[list_key]
                    if not isinstance(raw_list, list):
                        return web.json_response(
                            {"error": f"'{list_key}' for command '{cmd_name}' must be a list of strings."},
                            status=400,
                        )
                    if len(raw_list) > MAX_COMMAND_LIST_ITEMS:
                        return web.json_response(
                            {
                                "error": f"'{list_key}' for command '{cmd_name}' cannot exceed {MAX_COMMAND_LIST_ITEMS} items."
                            },
                            status=400,
                        )
                    cleaned_items: list[str] = []
                    seen_items: set[str] = set()
                    for item in raw_list:
                        if not isinstance(item, str):
                            return web.json_response(
                                {
                                    "error": f"Every entry in '{list_key}' for command '{cmd_name}' must be a string."
                                },
                                status=400,
                            )
                        if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                            return web.json_response(
                                {
                                    "error": f"Entries in '{list_key}' for command '{cmd_name}' cannot contain control characters."
                                },
                                status=400,
                            )
                        cleaned_str = item.strip()
                        if not cleaned_str or len(cleaned_str) > 64:
                            return web.json_response(
                                {
                                    "error": f"Entries in '{list_key}' for command '{cmd_name}' must be 1 to 64 characters."
                                },
                                status=400,
                            )
                        if list_key == "disabled_subcommands":
                            if cleaned_str not in valid_subs_for_cmd:
                                return web.json_response(
                                    {
                                        "error": f"Unknown subcommand '{cleaned_str}' for command '{cmd_name}'."
                                    },
                                    status=400,
                                )
                        elif list_key in ("allowed_channels", "ignored_channels"):
                            if cleaned_str not in valid_channel_ids:
                                return web.json_response(
                                    {
                                        "error": f"Channel ID '{cleaned_str}' does not belong to this server."
                                    },
                                    status=400,
                                )
                        elif list_key in ("allowed_roles", "ignored_roles"):
                            if cleaned_str not in valid_role_ids:
                                return web.json_response(
                                    {
                                        "error": f"Role ID '{cleaned_str}' does not belong to this server or is @everyone."
                                    },
                                    status=400,
                                )
                        if cleaned_str not in seen_items:
                            seen_items.add(cleaned_str)
                            cleaned_items.append(cleaned_str)
                    normalized_entry[list_key] = cleaned_items
                else:
                    prev_list = existing_dict.get(list_key, [])
                    if isinstance(prev_list, list):
                        normalized_entry[list_key] = [
                            str(x).strip()
                            for x in prev_list
                            if isinstance(x, (str, int))
                            and not isinstance(x, bool)
                            and str(x).strip()
                        ]
                    else:
                        normalized_entry[list_key] = []

            # Amendment #4: an entry whose lists are all empty removes ONLY that command
            if all(len(v) == 0 for v in normalized_entry.values()):
                merged_cmd_settings.pop(cmd_name, None)
            else:
                merged_cmd_settings[cmd_name] = normalized_entry

        updates["command_settings"] = merged_cmd_settings

    if has_disabled_cogs and has_command_settings:
        audit_action = "update_modules_and_commands"
    elif has_disabled_cogs:
        audit_action = "update_disabled_cogs"
    else:
        audit_action = "update_command_settings"

    return await _apply_guild_settings_mutation(
        request,
        guild_id=guild_id,
        user_id=user_id,
        actor_name=actor_name,
        expected_rev=expected_rev,
        before_doc=before_doc,
        updates=updates,
        audit_module="modules",
        audit_action=audit_action,
    )


def register_routes(
    app: web.Application,
    *,
    enable_debug_ip: bool | None = None,
) -> None:
    """Register Checkpoint 3A + 3B + 3C-1 + 3C-2 + 3C-3 routes on `app`."""
    debug_ip_enabled = (
        enable_debug_ip
        if enable_debug_ip is not None
        else os.getenv("WEBDASH_DEBUG_IP", "0").strip().lower() in ("1", "true", "yes")
    )

    routes = [
        web.get("/health", handle_health),
        web.get("/", handle_home),
        web.get("/servers", handle_servers),
        web.get("/login", handle_login),
        web.get("/callback", handle_callback),
        web.post("/logout", handle_logout),
        web.get("/manage/{guild_id}", handle_guild_overview),
        # Checkpoint 3B (Batches 1, 2 & 3) Jinja2 routes
        web.get("/modules/{guild_id}", handle_guild_modules),
        web.get("/automod/{guild_id}", handle_guild_automod),
        web.get("/lockdown/{guild_id}", handle_guild_lockdown),
        web.get("/misc/{guild_id}", handle_guild_misc),
        web.get("/wizard/{guild_id}", handle_guild_wizard),
        web.get("/logs/{guild_id}", handle_guild_logs),
        web.get("/owner_panel", handle_owner_panel),
        # Checkpoint 3C Batch 1, Batch 2 & Batch 3 mutation routes
        web.post("/api/settings/{guild_id}", handle_api_settings),
        web.post("/api/modules/{guild_id}", handle_api_modules),
        web.post("/api/owner/action", handle_owner_action),
        web.post("/api/owner/ip_action", handle_owner_ip_action),
    ]
    if debug_ip_enabled:
        routes.append(web.get("/debug/ip", handle_debug_ip))

    app.add_routes(routes)



