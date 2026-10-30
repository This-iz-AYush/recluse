# Recluse Bot — Modular Web Dashboard (`webdash`) Implementation Plan & Current State

## 1. Executive Summary & Runtime Constraints

This document captures the final architecture, verified codebase audit, current implementation state (**Phase A1 Complete: Checkpoints 3A, 3B, and 3C**), configuration reference, schema format, and remaining roadmap (**Phase A2 Command-Check Consolidation & Permission Matrix UI**, **Checkpoint 3D Expanded Wired Fields**, **Phase B**, and **Later Cog Wire-Up**) for Recluse's modular web dashboard (`webdash/`).

- **Target Environment**: Render Free Tier (`512 MB` RAM, `0.1 vCPU`, ephemeral disk, Python `3.14`, MongoDB Atlas via `motor`) fronted by a Cloudflare Worker (`workers.dev` -> `recluse-1.onrender.com`).
- **Stack**: `discord.py>=2.4.0`, `aiohttp>=3.11.0`, `aiohttp-jinja2>=1.6`, `jinja2>=3.1.0` (`autoescape=True`), `motor>=3.3.2`. No Quart/Flask/FastAPI (`quart` and `quart-cors` removed from `requirements.txt` after repo-wide grep confirmed zero imports); no Node/npm build pipeline.
- **Instant Zero-Downtime Rollback**: Controlled by `WEBDASH_ENABLED=1` (default `"0"`).
  - `cogs/dashboard.py` imports only `from webdash import is_webdash_enabled` (pure Python stdlib) at module top level and lazily imports `webdash.app` / `webdash.router` inside the `if is_webdash_enabled():` branch.
  - If `WEBDASH_ENABLED` is unset/`"0"` (or if `webdash` initialization fails), `cogs/dashboard.py` runs the legacy dashboard code path untouched, and legacy `db.sessions`, `db.visit_logs`, `db.ip_bans`, and `db.guild_settings` remain intact.

---

## 2. Current Implementation State (Phase A1 Complete — Checkpoints 3A, 3B & 3C)

### 2.1 Completed Modules & Files
| File | Status | Responsibility |
| :--- | :--- | :--- |
| `webdash/__init__.py` | **Done (Piece 1 + 3A)** | `is_webdash_enabled()` flag reader; `get_ready_db(bot)` helper that checks `getattr(bot, "db_ready", False)` and logs `"webdash: bot.db_ready is False, sessions disabled"` once if `False`. |
| `webdash/security.py` | **Done (Piece 1 + 2 + 3A + 3C-3)** | `safe_compare` (`utf-8` + `errors="surrogateescape"` + `secrets.compare_digest`), `normalize_ip`, `resolve_client_ip_details` (`ORIGIN_SHARED_SECRET` + right-most `X-Forwarded-For` via `TRUSTED_PROXY_HOPS=1`), `REQUIRE_ORIGIN_SHARED_SECRET` enforcement, trusted-IP-only `IpBanCache` enforcement (never blocking `GET /health` or unauthenticated `GET /`, failing open on error), one-time missing/empty secret warnings/errors, `is_plausible_session_token` (`^[A-Za-z0-9_-]{43}$`), dual-pool `InMemoryRateLimiter` (`_auth_buckets` / `_anon_buckets` + `pre_session` limit before DB lookup), CSRF verification, and `apply_security_headers` (including on raised `web.HTTPException` responses like `404` and `405`). |
| `webdash/auth.py` | **Done (Piece 1 + 2)** | `SessionManager` on `db.webdash_sessions`: SHA-256 `session_id_hash` (`secrets.token_urlsafe(32)` raw cookie `recluse_sid`), timezone-aware UTC `expires_at` (`TTL` index `expireAfterSeconds=0`), `last_seen` write-throttled to `300s`, `verify_oauth_state`, session rotation on login. |
| `webdash/permissions.py` | **Done (Piece 1)** | `PERMISSION_CATALOG` (35 nodes), `PRESETS` (`Admin`, `ModManager`, `Support`, `Viewer`, `ManageGuildDefault`), `expand_entries` (handles bare strings and skips non-`str` items), deterministic `evaluate_permission` with non-lockable-out Bot Owner, Server Owner, and Discord Administrator (`0x8`). |
| `webdash/cache.py` | **Done (Piece 1 + 3C-3)** | `ShortTTLCache` (`OrderedDict` LRU + TTL), `GuildConfigCache` (`max_size=200`, `ttl_seconds=120.0`, `db_timeout=2.0s`, fails open on DB error/timeout, invalidates `AI._invalidate_guild_cfg(guild_id)`), and `IpBanCache` (`max_size=2000`, `ttl_seconds=60.0`, `db_timeout=2.0s`, single-flight `asyncio.Lock` refresh serving the previous set while refreshing, fails open with backoff on DB error). |
| `webdash/audit.py` | **Done (Piece 1)** | `AuditLogger` on `db.dashboard_audit` (`created_at` 30-day TTL index `expireAfterSeconds=2592000`, compound index `[("guild_id", 1), ("created_at", -1)]`, field-level `compute_diff` + `before_snapshot`). |
| `webdash/app.py` | **Done (Piece 2 + 3A + 3C-3)** | `create_webdash_app(bot, ...)` wiring `aiohttp-jinja2` (`autoescape=True`), `IpBanCache`, security middleware, caches, and routes. |
| `webdash/router.py` | **Done (Piece 2 + 3A + 3B + 3C)** | Read routes (`GET /health`, `GET /debug/ip`, `GET /`, `GET /servers`, `GET /login`, `GET /callback`, `POST /logout`, `GET /manage/{guild_id}`, `GET /modules/{guild_id}`, `GET /automod/{guild_id}`, `GET /lockdown/{guild_id}`, `GET /misc/{guild_id}`, `GET /wizard/{guild_id}`, `GET /logs/{guild_id}`, `GET /owner_panel`) and mutation routes (`POST /api/settings/{guild_id}`, `POST /api/modules/{guild_id}`, `POST /api/owner/action`, `POST /api/owner/ip_action`) with strict pre-write reads (`503` on read failure), `dashboard_rev` optimistic concurrency (`409` on conflict), cache invalidation, and non-empty audit trail rows. |
| `webdash/templates/` | **Done (Piece 2 + 3A + 3B + 3C)** | `base.html` (responsive `hidden md:flex` sidebar, `POST /logout` form, `<meta name="csrf-token">`, `getCsrfToken()`, `{% block scripts %}`), `landing.html`, `servers.html`, `overview.html`, `modules.html`, `automod.html`, `lockdown.html`, `misc.html`, `wizard.html`, `logs.html`, `owner_panel.html`, `error.html`, `not_available.html`. |
| `cogs/database.py` | **Done (Applied by User)** | Sets `self.bot.db_ready = False` in `__init__`, configures `serverSelectionTimeoutMS=5000, connectTimeoutMS=5000`, and sets `self.bot.db_ready = True` after `ping`. |
| `cogs/dashboard.py` | **Done (Checkpoint 3A)** | Lazy `WEBDASH_ENABLED=1` wiring in `Dashboard.__init__` with automatic fallback to legacy on import/init error; `Dashboard.start_server` starts HTTP immediately and polls `bot.db_ready` every `2s` for up to `120s` in a background task to run `ensure_indexes()`. |
| `requirements.txt` | **Done (Post-3C Cleanup)** | Removed unused `quart` and `quart-cors` after repo-wide grep confirmed zero references across `bot.py`, `cogs/`, and `webdash/`. |
| `tests/` | **Done (67/67 Passing)** | `tests/test_webdash_core.py`, `tests/test_webdash_routes.py`, and legacy `tests/test_dashboard_routes.py`. |

---

## 3. Environment Variables Reference

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `WEBDASH_ENABLED` | `"0"` | Set to `"1"` (`"true"`, `"yes"`, `"on"`) to serve the new `webdash` app from `cogs/dashboard.py`. Set to `"0"` or unset for immediate rollback to legacy dashboard. |
| `ORIGIN_SHARED_SECRET` | `""` (unset) | Shared secret validated via `safe_compare` against the `X-Recluse-Origin-Secret` header sent by the Cloudflare Worker. When valid, `webdash` trusts `X-Recluse-Client-IP` (`trusted_source="origin_header"`). When unset, logs a one-time startup warning because Cloudflare Worker overwrites `X-Forwarded-For` with `CF-Connecting-IP` (so Render's right-most XFF hop is Cloudflare's egress IP). |
| `REQUIRE_ORIGIN_SHARED_SECRET` | `"0"` | When `"1"`, rejects any request lacking a valid `X-Recluse-Origin-Secret` with HTTP `403`, **except** `GET/HEAD /health` and unauthenticated `GET/HEAD /` (so direct-to-origin keep-alive probes still get `200`). Logs one startup `ERROR` if enabled while `ORIGIN_SHARED_SECRET` is empty. |
| `TRUSTED_PROXY_HOPS` | `"1"` | Number of right-most `X-Forwarded-For` hops to step back when `ORIGIN_SHARED_SECRET` is not used (`hops[-effective_hops]`, falling back to `hops[-1]`, never `hops[0]`). |
| `WEBDASH_DEBUG_IP` | `"0"` | When `"1"`, registers the Bot-Owner-only diagnostic endpoint `GET /debug/ip`. |
| `DISCORD_CLIENT_ID` | `""` | Discord application Client ID for OAuth2 login and bot invite links. |
| `DISCORD_CLIENT_SECRET` | `""` | Discord application Client Secret for `/callback` token exchange. |
| `REDIRECT_URI` | `""` | Discord OAuth2 callback URL (`https://<domain>/callback`). |
| `OWNER_ID` | `""` | Optional explicit Bot Owner Discord user ID override (otherwise resolved from `bot.owner_id` or `bot.application_info()`). |
| `PORT` | `"8080"` | HTTP listening port bound by `Dashboard.start_server()`. |

---

## 4. MongoDB Collections & Indexes (`recluse_db`)

### 4.1 New Collections Created by `webdash/`
1. **`db.webdash_sessions`** (Managed by `webdash/auth.py` `SessionManager`):
   - **Indexes**:
     - `("session_id_hash", 1)` — `unique=True`
     - `("expires_at", 1)` — `expireAfterSeconds=0` (MongoDB native TTL index on timezone-aware UTC `datetime`)
     - `("discord_id", 1)`
   - **Document Schema**:
     ```json
     {
       "session_id_hash": "<64-char sha256 hex of raw recluse_sid cookie>",
       "discord_id": 123456789012345678,
       "username": "Username",
       "avatar": "avatar_hash_or_null",
       "csrf_token": "<43-char token_urlsafe(32)>",
       "guilds": [{"id": "999", "name": "Server", "icon": null, "owner": true, "permissions": 8}],
       "created_at": "2026-10-27T00:00:00+00:00 (UTC datetime)",
       "last_seen": "2026-10-27T00:05:00+00:00 (UTC datetime, throttled >= 300s)",
       "expires_at": "2026-10-28T00:00:00+00:00 (UTC datetime, +86400s)",
       "ip_norm": "203.0.113.10",
       "ua_hash": "<16-char sha256 prefix>"
     }
     ```
2. **`db.dashboard_audit`** (Managed by `webdash/audit.py:19, 82-89, 129-139` `AuditLogger`):
   - **Indexes**:
     - `[("guild_id", 1), ("created_at", -1)]`
     - `("created_at", 1)` — `expireAfterSeconds=2592000` (30-day TTL: `30 * 86400` seconds)
   - **Document Schema**:
     ```json
     {
       "guild_id": 999888777,
       "actor_id": 123456789012345678,
       "actor_name": "AdminUser",
       "module": "automod",
       "action": "update",
       "changes": {"automod_enabled": {"old": false, "new": true}},
       "before_snapshot": {"automod_enabled": false},
       "ip_norm": "203.0.113.10",
       "created_at": "2026-10-27T00:00:00+00:00 (UTC datetime)"
     }
     ```
3. **`db.lockdown_snapshots`** *(Planned for Phase B Lockdown v2)*:
   - Will store pre-lockdown channel permission overwrite snapshots per `(guild_id, channel_id)` before modifying `@everyone` permissions.

### 4.2 Existing Collections Read/Written by Cogs & Dashboard
- **`db.guild_settings`**: Per-guild flat settings document (`{"guild_id": int, ...}`). Cached via `GuildConfigCache` (`max_size=200`, `ttl_seconds=120.0`).
- **`db.antinuke_config`**: Anti-Nuke configuration per guild (`{"guild_id": int, ...}`).
- **`db.auto_responses`**: Auto-responder trigger documents per guild (`{"guild_id": int, "keyword": str, ...}`).
- **`db.reaction_roles`**: Reaction role mappings (`{"guild_id": int, "message_id": int, "emoji": str, "role_id": int}`).
- **`db.custom_commands`**: Custom text commands (`{"guild_id": int, "name": str, "response": str, "created_by": int}`).
- **`db.security_logs`**: Bot/security event logs (`{"guild_id": int, "timestamp": float, "action": str, "user_id": int | str, "content": str}`, written at `bot.py:203-208, 262-267`, `cogs/ai.py:817-828`, indexed at `cogs/database.py:49`).
- **`db.sessions`, `db.visit_logs`, `db.ip_bans`**: Legacy dashboard collections — **left untouched** for `WEBDASH_ENABLED=0` compatibility.

---

## 5. Permission Catalog & RBAC Evaluation (`webdash/permissions.py`)

### 5.1 Permission Nodes (35 Total)
| Category | Permission Nodes |
| :--- | :--- |
| **Overview & General** | `overview.view`, `settings.view`, `settings.edit`, `settings.export`, `settings.import` |
| **Modules & Commands** | `modules.view`, `modules.edit`, `commands.view`, `commands.edit` |
| **Welcome & Reaction Roles** | `welcome.view`, `welcome.edit`, `reactionroles.view`, `reactionroles.edit` |
| **Moderation & AutoMod** | `moderation.view`, `moderation.edit`, `automod.view`, `automod.edit` |
| **Security & Lockdown** | `antinuke.view`, `antinuke.edit`, `lockdown.execute` |
| **Auto-Responder** | `autoresponder.view`, `autoresponder.edit` |
| **Leveling & XP** | `leveling.view`, `leveling.edit` |
| **AI Chat** | `ai.view`, `ai.edit` |
| **Tickets** | `tickets.view`, `tickets.edit` |
| **Birthdays** | `birthdays.view`, `birthdays.edit` |
| **Giveaways** | `giveaways.view`, `giveaways.edit`, `giveaways.reroll` |
| **Reminders** | `reminders.view`, `reminders.edit` |
| **Logs & Audit** | `logging.view`, `audit.view`, `audit.restore` |
| **Access Control** | `permissions.manage` |

### 5.2 Built-In Presets
- `Admin`: `("*",)`
- `ModManager`: `("overview.view", "moderation.*", "automod.*", "autoresponder.*", "tickets.*", "giveaways.*", "birthdays.*", "reminders.*", "logging.view", "audit.view")`
- `Support`: `("overview.view", "tickets.*", "moderation.view", "logging.view")`
- `Viewer`: `("*.view",)`
- `ManageGuildDefault`: All 35 nodes **except** `antinuke.edit` and `permissions.manage`.

### 5.3 Evaluation Order (`evaluate_permission`)
1. **Bot Owner** (`user_id == bot_owner_id`) -> **ALLOW** (even if not in guild).
2. **Guild Membership Check**: If `not is_member` -> **DENY**.
3. **Non-Lockable-Out Guild Principals**:
   - **Discord Server Owner** (`user_id == guild_owner_id`) -> **ALLOW**.
   - **Discord Administrator** (`discord_permissions & 0x8` or `is_administrator=True`) -> **ALLOW**.
   - *(Neither can be locked out by `user_denies` or `role_mappings` denies).*
4. **Explicit User Denies** (`dashboard_permissions.user_denies[str(user_id)]`) -> If matched, **DENY**.
5. **Explicit User Overrides** (`dashboard_permissions.user_overrides[str(user_id)]`) -> If matched, **ALLOW**.
6. **Discord Role Mappings** (`dashboard_permissions.role_mappings[str(role_id)]`) across all of the member's live Discord roles:
   - If any role matches `deny` -> **DENY**.
   - If any role matches `allow` -> **ALLOW**.
7. **Discord Manage Server (`0x20`)**: Evaluated against `dashboard_permissions.manage_guild_permissions` (defaults to `PRESETS["ManageGuildDefault"]`).
8. **Fallback**: **DENY**.

---

## 6. Module Schema Format & Verified Field Registry

### 6.1 Declarative Schema Format (`webdash/schema.py` & `webdash/modules/` — Phase B)
All module schemas live under `webdash/modules/` (never inside `cogs/`). Each schema maps directly to the **existing flat MongoDB keys** read by the bot's cogs:

```python
@dataclasses.dataclass(frozen=True)
class FieldSchema:
    key: str                       # Exact flat key in MongoDB (e.g. "automod_enabled")
    label: str
    field_type: str                # "bool" | "int" | "float" | "text" | "textarea" | "select"
                                   # | "string_list" | "channel_select" | "channel_multi_select"
                                   # | "category_select" | "role_select" | "user_multi_select"
    default: Any
    cog_citations: tuple[str, ...] # Verified "file:line" citations where cogs read this key
    description: str = ""
    min_value: int | float | None = None
    max_value: int | float | None = None
    max_length: int | None = None
    max_items: int | None = None
    options: tuple[tuple[str, str], ...] = ()
    nullable: bool = False

@dataclasses.dataclass(frozen=True)
class ModuleSchema:
    slug: str                      # e.g. "automod"
    title: str
    icon: str
    collection: str                # "guild_settings" | "antinuke_config" | ...
    view_permission: str           # e.g. "automod.view"
    edit_permission: str           # e.g. "automod.edit"
    fields: tuple[FieldSchema, ...]
```

### 6.2 Verified Wired Fields & Defaults (Re-read and Verified in Current Codebase)
| Module | Collection | Flat Key | Type | Default | Verified Cog Readers & Writers (`file:line`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Welcome & Leave** | `guild_settings` | `welcome_enabled` | `bool` | `True` | Read: `cogs/admin.py:243, 983, 1014`<br>Write: `cogs/admin.py:300, 365` |
| | `guild_settings` | `welcome_channel` | `channel_select` | `None` | Read: `cogs/admin.py:232, 985, 1016`<br>Write: `cogs/admin.py:143, 361` |
| | `guild_settings` | `welcome_message` | `textarea` | `"👋 Welcome to **{server}**, {user}! You're member #{count}."` | Read: `cogs/admin.py:991-997` (replaces `{user}`, `{server}`, `{count}`, `{name}`)<br>Write: `cogs/admin.py:354, 362` |
| | `guild_settings` | `leave_message` | `textarea` | `"👋 **{user}** has left the server. Goodbye!"` | Read: `cogs/admin.py:1022-1027` (replaces `{user}`, `{name}`, `{server}`)<br>Write: `cogs/admin.py:355, 363` |
| | `guild_settings` | `welcome_dm` | `bool` | `False` | Read: `cogs/admin.py:1005`<br>Write: `cogs/admin.py:364` |
| **Auto-Role & Reaction Roles** | `guild_settings` | `auto_role` | `role_select` | `None` | Read: `cogs/admin.py:236, 974`<br>Write: `cogs/admin.py:386, 394` |
| | `guild_settings` | `auto_role_bots` | `bool` | `False` | Read: `cogs/admin.py:976`<br>Write: `cogs/admin.py:395` |
| | `reaction_roles` | `message_id`, `emoji`, `role_id` | CRUD docs | — | Read: `cogs/admin.py:431, 492`<br>Write: `cogs/admin.py:459, 476` |
| **Moderation & Logs** | `guild_settings` | `mod_enabled` | `bool` | `True` | Read: `cogs/moderation.py:23`, `cogs/admin.py:241`<br>Write: `cogs/admin.py:298` |
| | `guild_settings` | `log_channel` | `channel_select` | `None` | Read: `cogs/admin.py:231, 1040, 1066, 1091, 1109`<br>Write: `cogs/admin.py:118, 329` |
| | `guild_settings` | `log_event_filter` | `select` (`all\|mod\|messages\|members`) | `"all"` | Read: `cogs/admin.py:1041, 1067, 1092, 1110`<br>Write: `cogs/admin.py:331` |
| **AutoMod & Anti-Spam** | `guild_settings` | `automod_enabled` | `bool` | `False` | Read: `bot.py:189`, `cogs/ai.py:591`, `cogs/admin.py:247, 843`<br>Write: `cogs/admin.py:304, 556, 825` |
| | `guild_settings` | `banned_words` | `string_list` | `[]` | Read: `bot.py:191`, `cogs/ai.py:593`, `cogs/admin.py:907`, `cogs/dashboard.py:1243`<br>Write: `cogs/admin.py:917, 927, 931`, `cogs/dashboard.py:1932-1936` |
| | `guild_settings` | `antispam_threshold` | `int` (`2..20`) | `5` | Read: `bot.py:219`, `cogs/admin.py:263`<br>Write: `cogs/admin.py:557` |
| | `guild_settings` | `antispam_window` | `int` (`2..30`) | `5` | Read: `bot.py:220`, `cogs/admin.py:264`<br>Write: `cogs/admin.py:558` |
| | `guild_settings` | `antispam_action` | `select` (`mute\|kick\|ban\|warn`) | `"mute"` | Read: `bot.py:221`, `cogs/admin.py:265`<br>Write: `cogs/admin.py:559` |
| | `guild_settings` | `antispam_mute_mins` | `int` (`1..1440`) | `10` | Read: `bot.py:222`<br>Write: `cogs/admin.py:560` |
| **Anti-Nuke** | `antinuke_config` | `enabled` | `bool` | `False` | Read: `cogs/antinuke.py:450, 538, 592, 639, 835`<br>Write: `cogs/antinuke.py:696` |
| | `antinuke_config` | `threshold` | `int` (`3..50`) | `10` | Read: `cogs/antinuke.py:457, 836`<br>Write: `cogs/antinuke.py:752` |
| | `antinuke_config` | `window` | `int` (`3..30`) | `10` | Read: `cogs/antinuke.py:458, 837`<br>Write: `cogs/antinuke.py:753` |
| | `antinuke_config` | `action` | `select` (`ban\|kick\|strip`) | `"ban"` | Read: `cogs/antinuke.py:249, 838`<br>Write: `cogs/antinuke.py:754` |
| | `antinuke_config` | `log_channel` | `channel_select` | `None` | Read: `cogs/antinuke.py:250, 662, 840, 915`<br>Write: `cogs/antinuke.py:758` |
| | `antinuke_config` | `bot_join_protection` | `bool` | `False` | Read: `cogs/antinuke.py:641, 857`<br>Write: `cogs/antinuke.py:755` |
| | `antinuke_config` | `whitelist` | `user_multi_select` | `[]` | Read: `cogs/antinuke.py:198, 792, 839`<br>Write: `cogs/antinuke.py:816, 822` |
| **Auto-Responder** | `guild_settings` | `autoresponse_enabled` | `bool` | `True` | Read: `cogs/autoresponse.py:81` |
| | `auto_responses` | `keyword`, `response`, `match_mode`, `response_type`, `cooldown`, `case_sensitive`, `delete_trigger`, `enabled` | CRUD docs | — | Read: `cogs/autoresponse.py:35-58, 87-128, 240, 267`<br>Write: `cogs/autoresponse.py:200-213, 227, 288, 305` |
| **Leveling** | `guild_settings` | `leveling_enabled` | `bool` | `True` | Read: `cogs/leveling.py:367, 428`, `cogs/admin.py:242`<br>Write: `cogs/admin.py:299, 596` |
| | `guild_settings` | `xp_cooldown` | `int` (`10..600`) | `60` | Read: `cogs/leveling.py:376`<br>Write: `cogs/admin.py:598` |
| | `guild_settings` | `levelup_channel` | `channel_select` | `None` | Read: `cogs/leveling.py:334`<br>Write: `cogs/admin.py:599` |
| | `guild_settings` | `levelup_message` | `textarea` | `"🎉 GG {mention}, you reached **Level {level}**!"` | Read: `cogs/leveling.py:336`<br>Write: `cogs/admin.py:600` |
| | `guild_settings` | `xp_multiplier` | `float` (`0.5..5.0`) | `1.0` | Read: `cogs/leveling.py:388, 431`<br>Write: `cogs/admin.py:601` |
| **AI Chat** | `guild_settings` | `ai_enabled` | `bool` | `True` | Read: `cogs/ai.py:607`, `cogs/admin.py:240, 841`<br>Write: `cogs/admin.py:297, 823` |
| | `guild_settings` | `default_ai_model` | `select` (`auto\|deepseek\|groq\|gemini`) | `"auto"` | Read: `cogs/ai.py:77-91, 291-299, 401-411, 657, 743-754`, `cogs/admin.py:256, 842`<br>Write: `cogs/admin.py:821`, `cogs/dashboard.py:1942-1946` |
| | `guild_settings` | `ai_allowed_channels` | `channel_multi_select` | `[]` | Read: `cogs/ai.py:613`, `cogs/admin.py:831, 845`<br>Write: `cogs/admin.py:828, 834` |
| **Tickets** | `guild_settings` | `ticket_category` | `category_select` | `None` | Read: `cogs/tickets.py:152`, `cogs/admin.py:233`<br>Write: `cogs/admin.py:671` |
| | `guild_settings` | `ticket_support_role` | `role_select` | `None` | Read: `cogs/tickets.py:47, 66, 153`, `cogs/admin.py:237`<br>Write: `cogs/admin.py:672` |
| | `guild_settings` | `ticket_log_channel` | `channel_select` | `None` | Read: `cogs/tickets.py:107`<br>Write: `cogs/admin.py:673` |
| **Starboard** | `guild_settings` | `starboard_channel` | `channel_select` | `None` | Read: `cogs/admin.py:234, 1129`<br>Write: `cogs/admin.py:710, 713` |
| | `guild_settings` | `starboard_threshold` | `int` (`1..50`) | `3` | Read: `cogs/admin.py:1130`<br>Write: `cogs/admin.py:714` |
| | `guild_settings` | `starboard_emoji` | `text` | `"⭐"` | Read: `cogs/admin.py:1131`<br>Write: `cogs/admin.py:715` |
| **Birthdays** | `guild_settings` | `birthday_channel` | `channel_select` | `None` | Read: `cogs/birthdays.py:110`<br>Write: `cogs/birthdays.py:172` |
| | `guild_settings` | `birthday_role` | `role_select` | `None` | Read: `cogs/birthdays.py:67, 74, 111`<br>Write: `cogs/birthdays.py:174` |
| **Modules, Commands & Misc** | `guild_settings` | `disabled_cogs` | `string_list` | `[]` | Read: `bot.py:107`, `cogs/core.py:745, 803`, `cogs/dashboard.py:606`<br>Write: `cogs/dashboard.py:1090` |
| | `guild_settings` | `command_settings` | `dict` (`disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`) | `{}` | Read: `cogs/core.py:749-777, 806-829`, `cogs/dashboard.py:607`<br>Write: `cogs/dashboard.py:1091` |
| | `guild_settings` | `disabled_cmds` | `string_list` (read-only) | `[]` | Read: `bot.py:108, 113` |
| | `guild_settings` | `misc_enabled` | `bool` | `True` | Read: `cogs/misc.py:174, 538`, `cogs/admin.py:246`, `cogs/dashboard.py:1663`<br>Write: `cogs/admin.py:303`, `cogs/dashboard.py:1953-1957` |
| | `guild_settings` | `sports_enabled` | `bool` | `True` | Read: `cogs/sports.py:35`, `cogs/admin.py:244`<br>Write: `cogs/admin.py:301` |
| | `custom_commands` | `name`, `response`, `created_by` | CRUD docs | — | Read: `cogs/admin.py:748, 783`<br>Write: `cogs/admin.py:761, 767` |

### 6.3 Planned, Not Wired Fields (Excluded from UI Until Later Per-Cog Wire-Up Phase)
These fields are **not rendered in the UI** until the separate later wire-up phase (one cog per change, defaults equal to today's hardcoded behavior, requiring explicit approval before editing any cog):
- `xp_per_message`: written at `cogs/admin.py:597`, not read at `cogs/leveling.py:382` (`random.randint(15, 25)`).
- `db.level_roles` `"type"` mismatch: `cogs/admin.py:641` writes `{guild_id, level, role_id}` without `"type"`, while `cogs/leveling.py:347` queries `{"guild_id": guild.id, "level": level, "type": xp_type}`.
- `tickets_enabled`: written at `cogs/admin.py:674`, not checked in `cogs/tickets.py:146`.
- `antispam_log_channel`: written at `cogs/admin.py:563`, not read in `bot.py:218-269`.
- `anime_enabled`: written at `cogs/admin.py:245, 302`, not read in `cogs/anime.py` (unverified).
- `birthdays`: custom celebration message / server timezone (`cogs/birthdays.py:25-31, 54` hardcodes `_BIRTHDAY_MESSAGES` and IST `UTC+5:30`).
- `moderation`: `dm_on_punish`, `mute_role_id` (`cogs/moderation.py` uses Discord timeout and always attempts DM).
- `ai`: `per_channel_models`, `system_prompt_custom` (not read in `cogs/ai.py`).
- `giveaways` / `reminders`: custom limits/roles (hardcoded in `cogs/giveaways.py` and `cogs/reminders.py`).

---

## 7. Key Architectural Decisions & Known Leftovers

1. **No `ENFORCE_COMMAND_SETTINGS` Flag**:
   - `cogs/core.py:718-778` (`Core.bot_check`) and `cogs/core.py:780-833` (`Core.global_slash_check`, chaining `self._original_interaction_check` at `cogs/core.py:707-708, 714, 831-832`) already enforce `command_settings` live in production.
2. **Dual-Pool Rate Limiter & Pre-Session Guard**:
   - Rate-limit keys use `user:<discord_id>` or `sid:<session_id_hash[:16]>` **only** after `session_doc` is validated in MongoDB.
   - Unauthenticated requests are keyed by trusted client IP (`ip:<client_ip>`) or `shared:<scope>` in a separate `_anon_buckets` `OrderedDict`, so junk-cookie floods or IP rotation can never evict authenticated users' buckets (`_auth_buckets`).
   - `is_plausible_session_token` (`^[A-Za-z0-9_-]{43}$`) and the `pre_session` rate limit run **before** `session_loader` to bound MongoDB queries under cookie floods.
3. **Completed & Remaining Items**:
   - **Legacy `cogs/dashboard.py` Inline `onclick` / DOM ID XSS**: Replaced in **Checkpoint 3B (Batch 3)** & **Checkpoint 3C** using Jinja2 `autoescape=True` and `data-*` event delegation.
   - **Legacy `quart` / `quart-cors` in `requirements.txt`**: Removed after repo-wide grep across `bot.py`, `cogs/`, and `webdash/` confirmed zero imports.
   - **`cogs/info.py` Unused `cog_check` (lines 46–55)**: References undefined `interaction` on a slash-only cog; user will delete lines 46–55 in a separate commit.
   - **Duplicate Uncached MongoDB Queries in `bot.py` & `cogs/core.py`**: Scheduled for **Phase A2**.
   - **Lockdown**: Disabled (`501`) until **Phase B** implements `db.lockdown_snapshots` channel overwrite backup and restore.

---

## 8. Completed Phase A1 Checkpoints (3B & 3C) and Remaining Roadmap (Phase A2, Checkpoint 3D, Phase B)

### 8.1 Checkpoint 3B — Read-Only Port of Existing Guild & Owner Pages onto Jinja2 (Done & Verified)
Ported all read views of the existing dashboard pages from `cogs/dashboard.py` into `webdash/router.py` and `webdash/templates/` with `autoescape=True`, zero inline `on*=` event handlers, and live `resolve_guild_access` permission checks:
- **Batch 1 (`automod`, `lockdown`, `misc`)**: `GET /automod/{guild_id}` (`automod.view`, capped at 500 displayed words), `GET /lockdown/{guild_id}` (`antinuke.view`, disabled rebuild notice), `GET /misc/{guild_id}` (`settings.view`, `misc_enabled`).
- **Batch 2 (`wizard`, `logs`)**: `GET /wizard/{guild_id}` (`settings.view`, 4 real AI models `auto`, `deepseek`, `groq`, `gemini`), `GET /logs/{guild_id}` (`logging.view` for `security_logs` truncated at 300 chars; `dashboard_audit` gated behind `audit.view`; `"Could not load logs (database unavailable)"` on query error/timeout).
- **Batch 3 (`modules`, `owner_panel` + Batch 0 Exact-Case Fix)**: `GET /modules/{guild_id}` (`modules.view`, exact-case `cog_name_str in disabled_cogs`, protected-cog recovery badge, 5 wired `command_settings` keys, read-only `disabled_cmds`), `GET /owner_panel` (Bot Owner only, separate read-only `bot.global_lockdown` and `bot.dashboard_maintenance` cards, normalized & escaped IPs).

### 8.2 Checkpoint 3C — Mutation Endpoints, Strict Reads, Concurrency, Cache Invalidation & Audit Trail (Done & Verified)
1. **Strict Pre-Write Reads (`503` on Read Failure) & Optimistic Concurrency (`dashboard_rev`)**:
   - All guild mutation endpoints call `resolve_guild_access(request, guild_id, ..., fresh_settings=True)`, which uses `_fetch_fresh_guild_settings_strict` to distinguish a genuinely missing document (`None` -> upsert path with `expected_rev == 0`) from a MongoDB read error/timeout (returns HTTP `503` `"Database temporarily unavailable, please retry"` **before** any write).
   - Conditional updates check `dashboard_rev` (missing = `0`), `$set` only whitelisted fields, `$inc` `dashboard_rev` by `1`, and return HTTP `409` on revision mismatch.
2. **Batch 3C-1 (`POST /api/settings/{guild_id}`)**:
   - `update_automod` (requires `automod.edit`): validates `banned_words` (list of strings, max 500 entries of `<=64` chars, lowercased & deduplicated); refuses save (`400`) and locks the editor read-only if the stored list has `>500` entries so display truncation can never occur.
   - `update_ai_model` (requires `ai.edit`): validates `model` in `{"auto", "deepseek", "groq", "gemini"}` and writes `default_ai_model` (`wizard.html` save controls shown only when `can_edit_ai` is true).
   - `toggle` with `module == "toggleMisc"` (requires `settings.edit`): validates `enabled` (`bool`) and writes `misc_enabled`.
   - `toggle_lockdown` (requires `lockdown.execute`): returns HTTP `501` `{"error": "Lockdown is being rebuilt and is temporarily disabled"}`.
3. **Batch 3C-2 (`POST /api/modules/{guild_id}`)**:
   - `disabled_cogs` (requires `modules.edit`): exact-case loaded cog names; protected cogs (`core`, `database`, `dashboard`, `owner`, `jishaku`) can never be added to `disabled_cogs`, while allowing removal if previously stored.
   - `command_settings` (requires `commands.edit`): per-command merge onto stored `command_settings` (never replaces the whole dict; commands absent from payload—including protected-cog and stale commands—keep their stored settings; an entry whose 5 lists are all empty removes only that command; channel and role IDs are validated against live `guild` and stored as **strings** matching `cogs/core.py:761-764`).
4. **Batch 3C-3 (`POST /api/owner/action`, `POST /api/owner/ip_action` & `IpBanCache`)**:
   - `POST /api/owner/action` (`reload_cog`, Bot Owner only): restricts `cog` to `^[a-z0-9_]{1,32}$`, rejects `dashboard` and `database` (instructing the operator to use the Discord owner command), verifies `"cogs.<name>" in bot.extensions`, runs `bot.reload_extension(ext_name)` as a task wrapped in `asyncio.shield(task)` so a slow reload (`>10s`) returns `202 {"status": "still running"}` **without cancelling** the reload, and writes a non-empty audit row.
   - `POST /api/owner/ip_action` (`ban` / `unban`, Bot Owner only): validates IP with `normalize_ip` (rejecting `'invalid'`), refuses banning the requester's own current IP (`request[CLIENT_IP_KEY]`), performs a strict pre-write `db.ip_bans.find_one` (`503` on failure), skips the audit row and returns `200 {"status": "noop", "message": ...}` when nothing changes (already banned or not banned), immediately updates & refreshes `IpBanCache`, and writes a non-empty audit row on change.
   - `IpBanCache` (`webdash/cache.py:171-320`): single-flight `asyncio.Lock` refresh (`2.0s` timeout), serves the previous set while refreshing, fails open on DB error, enforced in `create_security_middleware` against the **trusted** client IP only and never blocking `GET /health` or unauthenticated `GET /`.

### 8.3 Remaining Roadmap

#### 1. Phase A2 — Command-Check Consolidation & Permission Matrix UI
- **Command-Check Consolidation (`bot.py` & `cogs/core.py`)**:
  - Consolidate duplicate uncached MongoDB reads across `bot.py` (`_core_security_check`, `on_message`) and `cogs/core.py` (`Core.bot_check` at `cogs/core.py:718-778` and `Core.global_slash_check` at `cogs/core.py:780-833`) into one shared access-check helper backed by `bot.guild_config` (`GuildConfigCache`) and short-TTL caches for `global_blacklist` and `co_owners`.
  - Preserve `self._original_interaction_check` chaining (`cogs/core.py:707-708, 714, 831-832`) and provide clear ephemeral/short-TTL user feedback when a command or module is disabled or restricted.
- **Permission Matrix UI (`permissions.manage`)**:
  - Dedicated dashboard page (`GET /permissions/{guild_id}` and `POST /api/permissions/{guild_id}`, gated behind `permissions.manage`) for viewing and editing `dashboard_permissions` in `db.guild_settings`:
    - Assign built-in presets (`Admin`, `ModManager`, `Support`, `Viewer`) or custom `allow`/`deny` node lists to Discord roles (`role_mappings`).
    - Configure `user_overrides`, `user_denies`, and `manage_guild_permissions` (`ManageGuildDefault`), while enforcing in both UI and backend that Bot Owner, Server Owner, and Discord Administrator (`0x8`) can never be locked out and `permissions.manage` cannot be self-revoked by non-owners.

#### 2. Checkpoint 3D — Expanded Wired-Field Scope
Expand the existing guild pages to expose the additional settings that cogs already read in production (using the same `dashboard_rev` concurrency, strict pre-write reads, `GuildConfigCache` invalidation, and `AuditLogger` trail):
- **`/automod/{guild_id}`**: Interactive `automod_enabled` toggle (`bot.py:189`, `cogs/ai.py:591`, `cogs/admin.py:247, 843`, default `False`), plus anti-spam thresholds (`antispam_threshold`, `antispam_window`, `antispam_action`, `antispam_mute_mins`, read at `bot.py:219-222`).
- **`/wizard/{guild_id}`**: Expand the guided quick-setup wizard to configure wired setup fields (`welcome_enabled`, `welcome_channel`, `log_channel`, `auto_role`, `automod_enabled`, `leveling_enabled`, `ai_enabled`, `default_ai_model`), validating channel/role snowflakes against live `guild.channels` / `guild.roles`.
- **`/misc/{guild_id}`**: Add `ai_enabled` (`cogs/ai.py:607`), `default_ai_model` (`cogs/ai.py:657`), and `sports_enabled` (`cogs/sports.py:35`, `cogs/admin.py:244`) toggles/selectors alongside `misc_enabled`.

#### 3. Phase B & Later Per-Cog Wire-Up Phase
- **Phase B**: Declarative schema engine (`webdash/schema.py` + `webdash/modules/*.py`), full module editors for all wired fields in §6.2 (Welcome, Auto-Role & Reaction Roles, Moderation & Logs, Anti-Nuke, Auto-Responder, Leveling, Tickets, Starboard, Birthdays), Audit Log viewer with one-click revision rollback (`audit.restore`), JSON export/import (`settings.export` / `settings.import`), and Lockdown v2 (`db.lockdown_snapshots`).
- **Later Wire-Up Phase**: One cog per change for the unwired fields in §6.3, with defaults matching current hardcoded behavior and explicit approval before touching any cog.
