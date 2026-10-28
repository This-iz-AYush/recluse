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
import dataclasses
import datetime
import json
import logging
import math
import os
import urllib.parse
from typing import Any, Awaitable, Callable

import aiohttp
import aiohttp_jinja2
from aiohttp import web

from webdash import get_ready_db
from webdash.audit import AuditLogger
from webdash.auth import (
    DEFAULT_SESSION_TTL_SECONDS,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SessionManager,
    generate_oauth_state,
    verify_oauth_state,
)
from webdash.cache import GuildConfigCache, ShortTTLCache
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
OAUTH_CLIENT_KEY = _AppKey("oauth_client", Any)

_MISSING_MEMBER_SENTINEL = object()

DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"

MAX_BANNED_WORDS_DISPLAY = 500
MAX_SECURITY_LOG_CONTENT_CHARS = 300
LOGS_QUERY_LIMIT = 50
LOGS_DB_TIMEOUT_SECONDS = 2.0
OWNER_VISIT_LOGS_LIMIT = 30

# Protected system cogs excluded from normal disabling in /modules/{guild_id}
# (cogs/dashboard.py:620 excludes dashboard, jishaku, database, owner; core added per 3B spec).
# If a guild already has one of these in disabled_cogs, it is still rendered with a
# "Protected (Re-enable Only)" badge so an admin can switch it back on.
PROTECTED_COGS_LOWER: frozenset[str] = frozenset(
    {"core", "database", "dashboard", "owner", "jishaku"}
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


async def resolve_guild_access(
    request: web.Request,
    guild_id: int,
    user_id: int,
    required_permission: str,
) -> GuildAccessContext:
    """
    Evaluate whether `user_id` has `required_permission` in `guild_id` using
    live guild membership, live member roles/permissions, and `GuildConfigCache`.
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

    cfg_cache: GuildConfigCache | None = request.app.get(GUILD_CONFIG_CACHE_KEY)
    settings: dict[str, Any] = {}
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

    access = await resolve_guild_access(request, guild_id, user_id, required_permission)
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
    - Requires `automod.view` (webdash/permissions.py:33).
    - Read-only Jinja2 port matching legacy cogs/dashboard.py:1243-1285 parity.
    - Reads `banned_words` (bot.py:191, cogs/ai.py:593, cogs/admin.py:907, default=[])
      and `automod_enabled` (bot.py:189, cogs/ai.py:591, cogs/admin.py:247, 843, default=False).
    - Caps displayed `banned_words` at MAX_BANNED_WORDS_DISPLAY (500) with a "+N more" note.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="automod.view",
        active_tab="automod",
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

    context.update(
        {
            "automod_enabled": bool(settings.get("automod_enabled", False)),
            "banned_words": displayed_words,
            "total_banned_words": total_banned_words,
            "extra_banned_words_count": extra_count,
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
    - Requires `settings.view` (webdash/permissions.py:16).
    - Read-only Jinja2 port matching legacy cogs/dashboard.py:1663-1708 parity
      (exposes `misc_enabled` read at cogs/misc.py:174, 538, cogs/admin.py:246, default=True).
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="settings.view",
        active_tab="misc",
    )
    if err_resp is not None or access is None:
        return err_resp  # type: ignore[return-value]

    settings = access.settings
    context.update(
        {
            "misc_enabled": bool(settings.get("misc_enabled", True)),
        }
    )
    return aiohttp_jinja2.render_template("misc.html", request, context)


async def handle_guild_wizard(request: web.Request) -> web.Response:
    """
    GET /wizard/{guild_id}
    - Requires `settings.view` (webdash/permissions.py:16).
    - Read-only Jinja2 port matching legacy cogs/dashboard.py:1535-1606 parity (AI-only),
      updated to the 4 real AI models wired in cogs/ai.py:77-91, 291-299, 401-411, 657, 743-754
      (`auto`, `deepseek`, `groq`, `gemini`).
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="settings.view",
        active_tab="wizard",
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
    - Reads `disabled_cogs` (bot.py:107, cogs/core.py:745, 803, cogs/dashboard.py:606),
      `command_settings` (cogs/core.py:749-777, 806-829, cogs/dashboard.py:607), and
      read-only `disabled_cmds` (bot.py:108, 113).
    - Protected-cog recovery rule: excludes `core`, `database`, `dashboard`, `owner`, `jishaku`
      when enabled; if a server already has one of them in `disabled_cogs` (legacy UI allowed
      disabling `Core`), still shows that cog with a clear "Protected (Re-enable Only)" badge.
    - Exposes ONLY the 5 `command_settings` keys that `cogs/core.py:753-764, 808-818` reads:
      `disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`.
    """
    err_resp, access, context = await _prepare_guild_page(
        request,
        required_permission="modules.view",
        active_tab="modules",
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
    disabled_cogs_lower = {c.lower() for c in disabled_cogs}

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

    # Build ordered list of (cog_name, cog_obj), ensuring any disabled cog in disabled_cogs
    # (including protected ones like Core) is included even if not in bot.cogs.
    cog_entries: list[tuple[str, Any]] = list(bot_cogs_dict.items())
    seen_cog_names_lower = {name.lower() for name, _ in cog_entries}
    for dcog in disabled_cogs:
        if dcog.lower() not in seen_cog_names_lower:
            cog_entries.append((dcog, None))
            seen_cog_names_lower.add(dcog.lower())

    modules_view: list[dict[str, Any]] = []
    for cog_name, cog_obj in cog_entries:
        cog_name_str = str(cog_name).strip()
        if not cog_name_str:
            continue
        cog_lower = cog_name_str.lower()
        is_disabled = (cog_name_str in disabled_cogs) or (cog_lower in disabled_cogs_lower)
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

            commands_view.append(
                {
                    "name": cmd_name,
                    "description": str(cmd_meta.get("desc") or "No description"),
                    "subcommands": subcommands_view,
                    "allowed_channels": _resolve_id_badges(
                        override.get("allowed_channels", []), channel_map, "channel"
                    ),
                    "ignored_channels": _resolve_id_badges(
                        override.get("ignored_channels", []), channel_map, "channel"
                    ),
                    "allowed_roles": _resolve_id_badges(
                        override.get("allowed_roles", []), role_map, "role"
                    ),
                    "ignored_roles": _resolve_id_badges(
                        override.get("ignored_roles", []), role_map, "role"
                    ),
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

    context.update(
        {
            "modules": modules_view,
            "disabled_cogs": disabled_cogs,
            "disabled_cmds": disabled_cmds,
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
        "guild_count": guild_count,
        "cog_count": cog_count,
        "global_lockdown": global_lockdown,
        "dashboard_maintenance": dashboard_maintenance,
        "extensions": extensions_list,
        "visit_logs": visit_logs,
        "ip_bans": ip_bans,
    }
    return aiohttp_jinja2.render_template("owner_panel.html", request, context)


def register_routes(
    app: web.Application,
    *,
    enable_debug_ip: bool | None = None,
) -> None:
    """Register Checkpoint 3A + 3B (Batches 1, 2 & 3) routes on `app`."""
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
        # Checkpoint 3B (Batches 1, 2 & 3) read-only Jinja2 routes
        web.get("/modules/{guild_id}", handle_guild_modules),
        web.get("/automod/{guild_id}", handle_guild_automod),
        web.get("/lockdown/{guild_id}", handle_guild_lockdown),
        web.get("/misc/{guild_id}", handle_guild_misc),
        web.get("/wizard/{guild_id}", handle_guild_wizard),
        web.get("/logs/{guild_id}", handle_guild_logs),
        web.get("/owner_panel", handle_owner_panel),
    ]
    if debug_ip_enabled:
        routes.append(web.get("/debug/ip", handle_debug_ip))

    app.add_routes(routes)


