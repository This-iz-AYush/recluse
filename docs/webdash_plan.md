# Recluse Bot — Modular Web Dashboard (`webdash`) Implementation Plan & Current State

## 1. Executive Summary & Runtime Constraints

This document captures the final architecture, verified codebase audit, current implementation state (through **Phase A1 Checkpoint 3A**), configuration reference, schema format, and remaining roadmap (**Checkpoints 3B & 3C**, **Phase A2**, **Phase B**, and **Later Cog Wire-Up**) for Recluse's modular web dashboard (`webdash/`).

- **Target Environment**: Render Free Tier (`512 MB` RAM, `0.1 vCPU`, ephemeral disk, Python `3.14`, MongoDB Atlas via `motor`) fronted by a Cloudflare Worker (`workers.dev` -> `recluse-1.onrender.com`).
- **Stack**: `discord.py>=2.4.0`, `aiohttp>=3.11.0`, `aiohttp-jinja2>=1.6`, `jinja2>=3.1.0` (`autoescape=True`), `motor>=3.3.2`. No Quart/Flask/FastAPI; no Node/npm build pipeline.
- **Instant Zero-Downtime Rollback**: Controlled by `WEBDASH_ENABLED=1` (default `"0"`).
  - `cogs/dashboard.py` imports only `from webdash import is_webdash_enabled` (pure Python stdlib) at module top level and lazily imports `webdash.app` / `webdash.router` inside the `if is_webdash_enabled():` branch.
  - If `WEBDASH_ENABLED` is unset/`"0"` (or if `webdash` initialization fails), `cogs/dashboard.py` runs the legacy dashboard code path untouched, and legacy `db.sessions`, `db.visit_logs`, `db.ip_bans`, and `db.guild_settings` remain intact.

---

## 2. Current Implementation State (Done Through Phase A1 Checkpoint 3A)

### 2.1 Completed Modules & Files
| File | Status | Responsibility |
| :--- | :--- | :--- |
| `webdash/__init__.py` | **Done (Piece 1 + 3A)** | `is_webdash_enabled()` flag reader; `get_ready_db(bot)` helper that checks `getattr(bot, "db_ready", False)` and logs `"webdash: bot.db_ready is False, sessions disabled"` once if `False`. |
| `webdash/security.py` | **Done (Piece 1 + 2 + 3A)** | `safe_compare` (`utf-8` + `errors="surrogateescape"` + `secrets.compare_digest`), `normalize_ip`, `resolve_client_ip_details` (`ORIGIN_SHARED_SECRET` + right-most `X-Forwarded-For` via `TRUSTED_PROXY_HOPS=1`), `REQUIRE_ORIGIN_SHARED_SECRET` enforcement, one-time missing/empty secret warnings/errors, `is_plausible_session_token` (`^[A-Za-z0-9_-]{43}$`), dual-pool `InMemoryRateLimiter` (`_auth_buckets` / `_anon_buckets` + `pre_session` limit before DB lookup), CSRF verification, and `apply_security_headers` (including on raised `web.HTTPException` responses like `404` and `405`). |
| `webdash/auth.py` | **Done (Piece 1 + 2)** | `SessionManager` on `db.webdash_sessions`: SHA-256 `session_id_hash` (`secrets.token_urlsafe(32)` raw cookie `recluse_sid`), timezone-aware UTC `expires_at` (`TTL` index `expireAfterSeconds=0`), `last_seen` write-throttled to `300s`, `verify_oauth_state`, session rotation on login. |
| `webdash/permissions.py` | **Done (Piece 1)** | `PERMISSION_CATALOG` (35 nodes), `PRESETS` (`Admin`, `ModManager`, `Support`, `Viewer`, `ManageGuildDefault`), `expand_entries` (handles bare strings and skips non-`str` items), deterministic `evaluate_permission` with non-lockable-out Bot Owner, Server Owner, and Discord Administrator (`0x8`). |
| `webdash/cache.py` | **Done (Piece 1)** | `ShortTTLCache` (`OrderedDict` LRU + TTL) and `GuildConfigCache` (`max_size=200`, `ttl_seconds=120.0`, `db_timeout=2.0s`, fails open on DB error/timeout, invalidates `AI._invalidate_guild_cfg(guild_id)`). |
| `webdash/audit.py` | **Done (Piece 1)** | `AuditLogger` on `db.dashboard_audit` (`created_at` 30-day TTL index `expireAfterSeconds=2592000`, compound index `[("guild_id", 1), ("created_at", -1)]`, field-level `compute_diff` + `before_snapshot`). |
| `webdash/app.py` | **Done (Piece 2 + 3A)** | `create_webdash_app(bot, ...)` wiring `aiohttp-jinja2` (`autoescape=True`), middlewares, caches, and routes. |
| `webdash/router.py` | **Done (Piece 2 + 3A + 3B Batches 1, 2 & 3)** | Implemented routes: `GET /health`, `GET /debug/ip` (owner-only when `WEBDASH_DEBUG_IP=1`), `GET /`, `GET /servers`, `GET /login`, `GET /callback`, `POST /logout` (`GET /logout` removed -> `405`), `GET /manage/{guild_id}`, `GET /modules/{guild_id}` (protected-cog recovery rule, 5 wired `command_settings` keys, read-only `disabled_cmds`), `GET /automod/{guild_id}` (capped at 500 displayed entries with `+N more` note), `GET /lockdown/{guild_id}`, `GET /misc/{guild_id}`, `GET /wizard/{guild_id}`, `GET /logs/{guild_id}` (`security_logs` truncated at 300 chars with ellipsis for `logging.view`, `dashboard_audit` gated behind `audit.view`, `"Could not load logs (database unavailable)"` shown on query failure/timeout), and `GET /owner_panel` (Bot Owner only; `bot.global_lockdown` and `bot.dashboard_maintenance` shown separately read-only; normalized & escaped IPs with `data-*` attributes only). |
| `webdash/templates/` | **Done (Piece 2 + 3A + 3B Batches 1, 2 & 3)** | `base.html` (responsive `hidden md:flex` sidebar, `POST /logout` form with CSRF token), `landing.html`, `servers.html`, `overview.html` (`Disabled: being rebuilt` Lockdown card), `modules.html`, `automod.html`, `lockdown.html`, `misc.html`, `wizard.html`, `logs.html`, `owner_panel.html`, `error.html`, `not_available.html`. |
| `cogs/database.py` | **Done (Applied by User)** | Sets `self.bot.db_ready = False` in `__init__`, configures `serverSelectionTimeoutMS=5000, connectTimeoutMS=5000`, and sets `self.bot.db_ready = True` after `ping`. |
| `cogs/dashboard.py` | **Done (Checkpoint 3A)** | Lazy `WEBDASH_ENABLED=1` wiring in `Dashboard.__init__` with automatic fallback to legacy on import/init error; `Dashboard.start_server` starts HTTP immediately and polls `bot.db_ready` every `2s` for up to `120s` in a background task to run `ensure_indexes()`. |
| `tests/` | **Done (Passing on 3.14)** | `tests/test_webdash_core.py`, `tests/test_webdash_routes.py`, and legacy `tests/test_dashboard_routes.py`. |

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
3. **Known Leftovers Tracked for Subsequent Checkpoints**:
   - **Legacy `cogs/dashboard.py` Inline `onclick` / DOM ID XSS**: Legacy `/modules/{guild_id}` (`cogs/dashboard.py:641-761`) interpolates `cog_name`/`cmd_name` into inline `onclick` attributes. Replaced in **Checkpoint 3B (Batch 3)** using Jinja2 `autoescape=True` and `data-*` event delegation.
   - **Legacy `quart` / `quart-cors` in `requirements.txt`**: Kept temporarily during Phase A1; to be removed after Checkpoint 3C once `grep` confirms nothing imports them.
   - **`cogs/info.py` Unused `cog_check` (lines 46–55)**: References undefined `interaction` on a slash-only cog; user will delete lines 46–55 in a separate commit.
   - **Duplicate Uncached MongoDB Queries in `bot.py` & `cogs/core.py`**: Consolidated in **Phase A2**.
   - **Lockdown**: Disabled (`501`) until **Phase B** implements `db.lockdown_snapshots` channel overwrite backup and restore.

---

## 8. Scope for Checkpoints 3B, 3C, 3D & Subsequent Phases

### 8.1 Checkpoint 3B — Read-Only Port of Existing Guild & Owner Pages onto Jinja2 (Legacy Parity First)
Port the read views of the existing dashboard pages from `cogs/dashboard.py` into `webdash/router.py` and `webdash/templates/` (all with `autoescape=True`, zero inline `on*=` event handlers, and live `resolve_guild_access` permission checks), adding each link to `build_sidebar_nav(guild_id, granted)` only as its handler and template land, delivered in three small reviewable batches:

#### Batch 1: `automod` + `lockdown` + `misc` (Done & Verified)
1. **`GET /automod/{guild_id}` (`automod.html`, requires `automod.view`)**:
   - Matches legacy `cogs/dashboard.py:1243-1285` parity: renders the `banned_words` (`bot.py:191`, `cogs/ai.py:593`, `cogs/admin.py:907`, `cogs/dashboard.py:1243`, default `[]`) read-only comma-separated editor and active filter badge pills (capped at 500 displayed entries with a `+N more` indicator when >500 words exist), plus a read-only status indicator for `automod_enabled` (`bot.py:189`, `cogs/ai.py:591`, `cogs/admin.py:247, 843`, default `False`).
2. **`GET /lockdown/{guild_id}` (`lockdown.html`, requires `antinuke.view`)**:
   - Renders the Emergency Lockdown status panel (`cogs/dashboard.py:1365-1378`) with the `Disabled: being rebuilt` banner and disabled button explaining that lockdown is disabled until Phase B's `db.lockdown_snapshots` lands.
3. **`GET /misc/{guild_id}` (`misc.html`, requires `settings.view`)**:
   - Matches legacy `cogs/dashboard.py:1663-1708` parity: renders only the `misc_enabled` (`cogs/misc.py:174, 538`, `cogs/admin.py:246`, `cogs/dashboard.py:1663`, default `True`) status toggle card for `/afk`, `/whois`, and `/serverinfo`.

#### Batch 2: `wizard` + `logs` (Done & Verified)
4. **`GET /wizard/{guild_id}` (`wizard.html`, requires `settings.view`)**:
   - Matches legacy `cogs/dashboard.py:1535-1606` parity (AI model selector only), updated from obsolete `nexusify`/`gemini`/`sarvam` to the 4 real models wired in `cogs/ai.py:77-91, 291-299, 401-411, 657, 743-754` (`auto`, `deepseek`, `groq`, `gemini`).
5. **`GET /logs/{guild_id}` (`logs.html`, requires `logging.view`)**:
   - Queries `db.security_logs` (sorted by float `timestamp` descending per `cogs/dashboard.py:1425`, `bot.py:208, 266`) for any user with `logging.view`, truncating displayed `content` at 300 characters with an ellipsis (`…`).
   - **Permission-Gated Audit History**: Queries and renders the `db.dashboard_audit` section (sorted by UTC `created_at` descending per `webdash/audit.py:83, 138`) **ONLY** when `"audit.view" in access.granted_permissions` (`webdash/permissions.py:62-63`). Users with `logging.view` alone (such as the `Support` preset at `webdash/permissions.py:90-95`) see only `security_logs` and never `dashboard_audit`.
   - **Database Unavailable / Timeout Banner**: If the `security_logs` or `dashboard_audit` query fails or times out (2.0s timeout), renders `"Could not load logs (database unavailable)"` in that section instead of `"No security events logged yet."` / `"No configuration changes recorded yet."`.

#### Batch 3: `modules` + `owner_panel` (Done)
6. **`GET /modules/{guild_id}` (`modules.html`, requires `modules.view`)**:
   - Renders toggleable cog cards (`disabled_cogs` read at `bot.py:107`, `cogs/core.py:745, 803`) and per-command override drawers exposing only the keys `cogs/core.py:749-777, 806-829` actually reads (`disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`).
   - **Protected Cogs & Recovery Rule**: Hides protected cogs `core`, `database`, `dashboard`, `owner`, and `jishaku` (`cogs/dashboard.py:620` + `core`) from normal disabling. However, if a guild already has any of those 5 protected cogs in `disabled_cogs` (because the legacy UI allowed disabling `Core`), still renders that cog in `/modules/{guild_id}` with a clear `Protected` badge so the admin can switch it back on.
   - Displays legacy `disabled_cmds` (`bot.py:108, 113`) as a read-only badge list if non-empty.
   - Uses `data-cog`, `data-cmd`, and `addEventListener` delegation (zero inline `on*=` attributes).
7. **`GET /owner_panel` (`owner_panel.html`, Bot Owner only)**:
   - Renders loaded cog extensions (`bot.extensions`, `cogs/dashboard.py:1762`), recent `db.visit_logs` (`cogs/dashboard.py:1777`), and `db.ip_bans` (`cogs/dashboard.py:2024`).
   - **Accurate Read-Only Maintenance Flags**: Separately displays and labels both distinct flags read-only:
     1. `bot.global_lockdown` (`bot.py:33, 82` — blocks bot commands for non-owners), and
     2. `bot.dashboard_maintenance` (`cogs/dashboard.py:43, 122` — blocks web dashboard access for non-owners),
     explicitly noting in the UI and docs that the legacy `/owner_panel` (`cogs/dashboard.py:1761-1881`) showed neither flag.

### 8.2 Checkpoint 3C — Mutation Endpoints, Strict Validation, Cache Invalidation & Audit Trail (Legacy Parity)
Port the POST mutation endpoints into `webdash/router.py` matching legacy parity (all protected by the CSRF middleware, `mutation` rate-limit scope, and live `resolve_guild_access` / Bot Owner checks):

1. **`POST /api/settings/{guild_id}`**:
   - Actions matching `cogs/dashboard.py:1928-1962`:
     - `update_automod` (requires `automod.edit`): validates `banned_words` (`words` string or list, max 200 words, max 64 chars each, lowercased & deduplicated).
     - `update_ai_model` (requires `settings.edit` or `ai.edit`): validates `model` against `{"auto", "deepseek", "groq", "gemini"}` and writes `default_ai_model`.
     - `toggle` with `module == "toggleMisc"` (requires `settings.edit`): validates `enabled` (`bool`) and writes `misc_enabled`.
     - `toggle_lockdown` (requires `lockdown.execute`): returns HTTP `501` JSON `{"error": "Lockdown is being rebuilt and is temporarily disabled"}` (`cogs/dashboard.py:1960-1961`).
   - On every write:
     - Reads current cached settings for `before_snapshot`.
     - Updates `db.guild_settings` with `asyncio.wait_for(..., timeout=2.0)`.
     - Calls `cfg_cache.invalidate(guild_id)` (which also calls `AI._invalidate_guild_cfg(guild_id)` per `webdash/cache.py:177-181` and `cogs/ai.py:267-269`).
     - Records field-level diff in `db.dashboard_audit` via `AuditLogger.record(...)` (`webdash/audit.py:95-146`).
2. **`POST /api/modules/{guild_id}` (requires `modules.edit` / `commands.edit`)**:
   - Validates `disabled_cogs` against real loaded cog names: forbids adding any protected cog (`core`, `database`, `dashboard`, `owner`, `jishaku`) to `disabled_cogs`, while allowing removal of a previously disabled protected cog so a server can recover.
   - Validates `command_settings` structure (`disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`, verifying channel and role snowflakes belong to `guild`).
   - Writes to `db.guild_settings`, invalidates `GuildConfigCache`, and records an audit log entry in `db.dashboard_audit`.
3. **`POST /api/owner_action` & `POST /api/ip_action` (Bot Owner only)**:
   - Validates owner identity via `resolve_bot_owner_id(request)` and executes `reload_cog` (`cogs/dashboard.py:1985-1995`) and IP `ban`/`unban` (`cogs/dashboard.py:2023-2032`) with CSRF protection.
4. **Post-3C Dependency Cleanup**:
   - Grep the repository for `quart` and `quart_cors` / `quart-cors`; once confirmed unused, remove `quart` and `quart-cors` from `requirements.txt` in a dedicated commit.

### 8.3 Checkpoint 3D — Expanded Wired-Field Scope (After 3C Sign-Off)
Once Checkpoints 3B and 3C are live and verified with 100% legacy parity, expand the existing pages to expose the additional settings that cogs already read in production:
- **`/automod/{guild_id}`**: Add interactive `automod_enabled` toggle (`bot.py:189`, `cogs/ai.py:591`, `cogs/admin.py:247, 843`, default `False`).
- **`/wizard/{guild_id}`**: Expand the guided quick-setup wizard to configure wired setup fields (`welcome_enabled`, `welcome_channel`, `log_channel`, `auto_role`, `automod_enabled`, `leveling_enabled`, `ai_enabled`, `default_ai_model`), validating channel/role snowflakes against live `guild.channels` / `guild.roles`.
- **`/misc/{guild_id}`**: Add `ai_enabled` (`cogs/ai.py:607`), `default_ai_model` (`cogs/ai.py:657`), and `sports_enabled` (`cogs/sports.py:35`, `cogs/admin.py:244`) toggles/selectors alongside `misc_enabled`.

### 8.4 Subsequent Phases (After Phase A1 Sign-Off)
- **Phase A2**: Consolidate `bot.py` and `cogs/core.py` command/module/blacklist checks into one shared access-check helper backed by `bot.guild_config` (`GuildConfigCache`) and short-TTL caches for `global_blacklist` and `co_owners`, preserving `_original_interaction_check` (`cogs/core.py:707, 714, 831`) and adding ephemeral/short-TTL feedback on blocked commands.
- **Phase B**: Declarative schema engine (`webdash/schema.py` + `webdash/modules/*.py`), full MEE6/Dyno/Carl-bot module editors for all wired fields in §6.2, RBAC role-mapping UI (`permissions.manage`), Audit Log viewer with one-click revision rollback (`audit.restore`), JSON export/import (`settings.export` / `settings.import`), and Lockdown v2 (`db.lockdown_snapshots`).
- **Later Wire-Up Phase**: One cog per change for the unwired fields in §6.3, with defaults matching current hardcoded behavior and explicit approval before touching any cog.
