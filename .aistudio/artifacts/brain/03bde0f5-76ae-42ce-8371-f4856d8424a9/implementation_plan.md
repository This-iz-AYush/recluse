# Recluse Bot — Modular Web Dashboard Architecture & Implementation Plan

## Executive Summary
This plan defines the architecture and phased rollout to transform Recluse's legacy `cogs/dashboard.py` into a **schema-driven, modular web control panel** (similar to MEE6, Dyno, and Carl-bot) while staying strictly within **Render Free Tier** constraints (**512MB RAM, 0.1 vCPU, ephemeral disk, Python 3.14, MongoDB via Motor**) behind a **Cloudflare Worker (`workers.dev` -> Render)**.

- **Stack**: `discord.py` 2.x, `aiohttp`, `aiohttp-jinja2` (`autoescape=True`), `motor` (async MongoDB). No Quart/Flask/FastAPI; no Node/npm build pipeline.
- **Zero-Downtime Rollback**: `WEBDASH_ENABLED=1` environment flag controls whether `cogs/dashboard.py` loads the new `webdash/` package or runs the legacy dashboard code. Legacy `db.sessions` and its indexes are left untouched so flipping `WEBDASH_ENABLED=0` rolls back cleanly.
- **Keep-Alive Fast Path**: Unauthenticated `GET /` and `GET /health` bypass session/database lookups so `cron-job.org` probes stay fast and lightweight.

---

## 1. Codebase Audit Findings & Approved Decisions (Verified in Current Session)

### 1.1 `command_settings` Live Enforcement (`bot.py` & `cogs/core.py`)
- **Verified Finding**: While `bot.py:98-116` (`_dashboard_module_check`) only checks `disabled_cogs` and `disabled_cmds`, **`cogs/core.py` already enforces `command_settings` live in production** via:
  - `cogs/core.py:718-778` (`Core.bot_check`) for prefix/hybrid commands.
  - `cogs/core.py:780-833` (`Core.global_slash_check`), which chains `self._original_interaction_check` (`cogs/core.py:707-708, 714, 831-832`).
- **Current Issues in `bot.py` & `cogs/core.py`**:
  1. **Duplicate Uncached MongoDB Reads**:
     - `bot.py:86, 93` (`_core_security_check`) queries `db.global_blacklist`.
     - `bot.py:103` (`_dashboard_module_check`) queries `db.guild_settings`.
     - `cogs/core.py:725, 741` (`bot_check`) queries `db.global_blacklist` and `db.guild_settings` again.
     - `cogs/core.py:787, 801` (`global_slash_check`) queries `db.global_blacklist` and `db.guild_settings` again.
     - `bot.py:186` (`on_message`) queries `db.guild_settings` on every message.
  2. **Swallowed Feedback on Blocked Commands**:
     - `cogs/core.py:746, 754, 768, 770, 774, 776` raises `commands.CheckFailure("...")` and `cogs/core.py:804, 809, 821, 823, 826, 828` raises `app_commands.CheckFailure("...")`, which `bot.py:355` (`on_command_error`) and `bot.py:386` (`on_app_command_error`) silently ignore (`return`).
  3. **Legacy `disabled_cmds`**:
     - Enforced in `bot.py:108, 113-114`, neither read nor written by `cogs/dashboard.py`.
- **Approved Decision**:
  - Drop `ENFORCE_COMMAND_SETTINGS`.
  - In **Phase A2**, consolidate all command/module/blacklist checks into **one shared access-check helper** backed by bounded in-memory TTL caches (`bot.guild_config` for `guild_settings`, plus short-TTL cache for `global_blacklist` and `co_owners`), invalidated directly by dashboard saves and owner blacklist commands (see §4).

### 1.2 Database Readiness & `db_ready` Guard (`cogs/database.py`)
- **Verified Finding**: `cogs/database.py:10, 12-23` schedules `self.bot.loop.create_task(self.init_db())`, which sets `self.bot.mongo_client` and `self.bot.db = self.bot.mongo_client['recluse_db']` (`cogs/database.py:19-20`) before `await self.bot.mongo_client.admin.command('ping')` (`cogs/database.py:23`). Existing cogs guard with `hasattr(self.bot, "db")`, so setting `bot.db = None` would cause `AttributeError`s in legacy cogs.
- **Approved Decision**:
  - You will apply a minimal `db_ready` patch to `cogs/database.py` directly.
  - All new code in `webdash/` checks `getattr(bot, "db_ready", False)` (and verifies `getattr(bot, "db", None) is not None`), uses short timeouts (`asyncio.wait_for(..., timeout=2.0)`), and **fails OPEN** on DB errors for bot command/message checks (and fails closed with clean `503`/`401` in dashboard auth/mutations).

### 1.3 `cogs/info.py` Unused `cog_check`
- **Verified Finding**: `cogs/info.py:46-54` defines `cog_check` referencing undefined `interaction`, while `Info` only has the `/lookup` `app_commands.Group` (`cogs/info.py:44`).
- **Approved Decision**: You will delete lines 46–55 in `cogs/info.py` directly.

### 1.4 Known Phase A0 Leftovers to Fix in Phase A1
1. **Client IP Resolution & Direct-Origin Protection**: `cogs/dashboard.py:89-92` reads `raw_ip.split(',')[0].strip()` (spoofable left-most entry). In Phase A1, `get_client_ip(request)` implements optional `ORIGIN_SHARED_SECRET` (`X-Recluse-Origin-Secret` + `X-Recluse-Client-IP`) and right-most `X-Forwarded-For` resolution via `TRUSTED_PROXY_HOPS` (default `1`), never falling back to the left-most entry (see §2.3).
2. **Inline `onclick` & `innerHTML` Interpolation in `/modules/{guild_id}`**: `cogs/dashboard.py:641, 655-706, 718-738` interpolates `cog_name` and `cmd_name` into inline `onclick` attributes and DOM IDs. Fixed in Phase A1 Piece 3 via Jinja2 `autoescape=True` and `data-*` event delegation.
3. **Python 3.14 Deprecations**: Use `datetime.datetime.now(datetime.timezone.utc)` and typed request/state access everywhere in `webdash/`.
4. **Lockdown**: Keep `toggle_lockdown` returning HTTP `501` (`cogs/dashboard.py:1940-1941`) until Phase B's Lockdown v2 (`db.lockdown_snapshots`).

---

## 2. Core Architecture (`webdash/`)

### 2.1 File & Directory Structure
Module schemas live strictly in `webdash/modules/`, never inside `cogs/`.

```text
/
├── bot.py
├── cogs/
│   ├── dashboard.py           # Entrypoint cog: routes to webdash/ when WEBDASH_ENABLED=1, else legacy
│   └── ...                    # Existing cogs untouched during Phase A1
├── webdash/
│   ├── __init__.py
│   ├── security.py            # normalize_ip, get_client_ip (ORIGIN_SHARED_SECRET + right-most XFF
│   │                          # via TRUSTED_PROXY_HOPS=1), InMemoryRateLimiter, CSRF, security headers
│   ├── auth.py                # Discord OAuth2 state, SessionManager (db.webdash_sessions, SHA-256
│   │                          # session_id_hash, UTC datetime expires_at TTL, throttled last_seen)
│   ├── permissions.py         # Permission catalog, RBAC resolver (Owner/BotOwner/Discord Admin ->
│   │                          # user_denies -> user_overrides -> role_mappings -> Manage Guild -> Deny)
│   ├── cache.py               # GuildConfigCache (LRU max 200, TTL 120s, short timeout, fail-open)
│   ├── audit.py               # AuditLogger (db.dashboard_audit, 30-day TTL, field-level diffs)
│   ├── app.py                 # Piece 2: registers middlewares, Jinja2 environment, route tables
│   ├── router.py              # Piece 2 & 3: UI & API routes (/health, /, /debug/ip, /manage, etc.)
│   ├── schema.py              # Phase B: Declarative Field/Section/ModuleSchema & validator
│   ├── modules/               # Phase B: Schema definitions citing exact legacy flat keys + file:line
│   └── templates/             # Piece 2 & 3: Jinja2 templates (autoescape=True)
└── tests/
    ├── test_dashboard_routes.py
    └── test_webdash_core.py   # Piece 1 unit tests
```

### 2.2 Session Management (`db.webdash_sessions`)
Leaves legacy `db.sessions` and its indexes (`cogs/database.py:54-55`) untouched so `WEBDASH_ENABLED=0` rolls back cleanly.

- **Cookie**: `recluse_sid` (`HttpOnly`, `Secure` when HTTPS, `SameSite=Lax`, path `/`, max-age `86400`).
- **Token Generation**: `raw_token = secrets.token_urlsafe(32)`. Only `session_id_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()` is persisted in MongoDB.
- **Document Structure in `db.webdash_sessions`**:
  - `session_id_hash`: 64-char SHA-256 hex digest (`unique=True` index)
  - `discord_id`: `int` (indexed)
  - `username`: `str`
  - `avatar`: `str | None`
  - `csrf_token`: `secrets.token_urlsafe(32)`
  - `guilds`: filtered list of `{id, name, icon, owner, permissions}`
  - `created_at`: timezone-aware UTC `datetime`
  - `last_seen`: timezone-aware UTC `datetime` (throttled: updated in MongoDB only if `(now - last_seen).total_seconds() >= 300`)
  - `expires_at`: timezone-aware UTC `datetime` (`expireAfterSeconds=0` TTL index)
  - `ip_norm`: normalized client IP string
  - `ua_hash`: 16-char SHA-256 hex prefix of `User-Agent`
- **OAuth2 State Verification**: `recluse_oauth_state` cookie (`HttpOnly`, `SameSite=Lax`, max-age `300s`) verified via `secrets.compare_digest`.

### 2.3 Client IP Resolution & Rate Limiting (`webdash/security.py`)
1. **Trusted-Header Mode (`ORIGIN_SHARED_SECRET`)**:
   - If `ORIGIN_SHARED_SECRET` is configured (non-empty) and the request carries `X-Recluse-Origin-Secret` matching `ORIGIN_SHARED_SECRET` via `secrets.compare_digest`:
     - Extract `X-Recluse-Client-IP`, validate via `normalize_ip()`, and if valid (`!= "invalid"`), return it immediately.
   - If `ORIGIN_SHARED_SECRET` is unset, or `X-Recluse-Origin-Secret` is missing or does not match, **ignore `X-Recluse-Client-IP` completely**.
   - Note: If `ORIGIN_SHARED_SECRET` is set on the server, any request arriving **without** a valid `X-Recluse-Origin-Secret` bypassed the Cloudflare Worker (direct-to-Render hit); in that case, `X-Forwarded-For` hops beyond Render's own edge (`1` hop from the right) must not be trusted.
2. **Right-Most `X-Forwarded-For` Resolution (`TRUSTED_PROXY_HOPS`)**:
   - Default `TRUSTED_PROXY_HOPS = 1` (configurable via `os.getenv("TRUSTED_PROXY_HOPS", "1")`, clamped to `>= 1`).
   - Parse `X-Forwarded-For` by splitting on `,` and stripping whitespace (discarding empty tokens).
   - If `hops` is non-empty:
     - If `len(hops) >= trusted_hops`: select `hops[-trusted_hops]`.
     - If `len(hops) < trusted_hops`: fall back to the **right-most** entry `hops[-1]` (**never** `hops[0]`).
     - Normalize via `normalize_ip()`. If valid (`!= "invalid"`), return it.
   - Fall back to `normalize_ip(request.remote)`. If valid, return it; otherwise return `"invalid"`.
3. **Rate-Limiter Key Selection (`resolve_rate_limit_key`)**:
   - Purely in-memory `InMemoryRateLimiter` (`OrderedDict`, max 5,000 keys, zero MongoDB writes).
   - Priority for rate-limit key so an unresolvable IP or shared proxy never throttles all users together:
     1. Authenticated user ID (`f"user:{user_id}"`) if available.
     2. Session cookie hash (`f"sid:{sha256(cookie)[:16]}"`) if a session cookie (`recluse_sid` or `recluse_session`) is present.
     3. Normalized client IP (`f"ip:{client_ip}"`) if `client_ip != "invalid"`.
     4. Fallback shared bucket (`" bucket:unidentified"`) using the generous `unidentified` scope (`300` req / `60s`) instead of the strict per-IP limit.

### 2.4 Granular Permission Engine (`webdash/permissions.py`)

#### Permission Catalog
| Category | Permission Nodes |
| :--- | :--- |
| **Overview & General** | `overview.view`, `settings.view`, `settings.edit`, `settings.export`, `settings.import` |
| **Modules & Commands** | `modules.view`, `modules.edit`, `commands.view`, `commands.edit` |
| **Welcome & Auto-Role** | `welcome.view`, `welcome.edit`, `reactionroles.view`, `reactionroles.edit` |
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

#### Evaluation Order (Deterministic, Non-Lockable-Out Owners & Discord Administrators)
1. **Guild Membership Check**: User must be a member of the guild (or Bot Owner).
2. **Non-Lockable-Out Root Principals**:
   - **Bot Owner** (`user_id == bot_owner_id`)
   - **Discord Server Owner** (`user_id == guild_owner_id`)
   - **Discord Administrator** (`member_permissions & 0x8` / `guild_permissions.administrator == True`)
   - -> Always **ALLOW** (`*`). Cannot be denied by `user_denies` or `role_mappings` denies.
3. **`user_denies[str(user_id)]`**: If the requested node (or matching wildcard/preset) is in `user_denies`, **DENY**.
4. **`user_overrides[str(user_id)]`**: If the requested node (or matching wildcard/preset) is in `user_overrides`, **ALLOW**.
5. **Discord Role Mappings (`role_mappings`)**: Across all of the member's Discord role IDs:
   - If any role explicitly denies the node (`deny`), **DENY**.
   - If any role explicitly allows the node (`allow`), **ALLOW**.
6. **Manage Server Default Preset (`member_permissions & 0x20` / `manage_guild == True`)**:
   - Configurable via `manage_guild_preset` in `dashboard_permissions` (defaults to all nodes except `antinuke.edit` and `permissions.manage`).
7. **Default**: **DENY**.

---

## 3. Schema Registry: Verified Wired Fields vs. Planned (Unwired) Fields

### 3.1 Verified Wired Schema Fields (Re-read and Verified in Current Session)
Every citation below was verified directly against the current files in this session.

| Module (`webdash/modules/`) | Collection | Legacy Flat Key | Type | Verified Readers & Writers (`file:line`) |
| :--- | :--- | :--- | :--- | :--- |
| **Welcome & Leave** (`welcome.py`) | `guild_settings` | `welcome_enabled` | `bool` | Read: `cogs/admin.py:243, 983, 1014`<br>Write: `cogs/admin.py:300, 365` |
| | `guild_settings` | `welcome_channel` | `channel_select` | Read: `cogs/admin.py:232, 985, 1016`<br>Write: `cogs/admin.py:143, 361` |
| | `guild_settings` | `welcome_message` | `textarea` | Read: `cogs/admin.py:991`<br>Write: `cogs/admin.py:362` |
| | `guild_settings` | `leave_message` | `textarea` | Read: `cogs/admin.py:1022`<br>Write: `cogs/admin.py:363` |
| | `guild_settings` | `welcome_dm` | `bool` | Read: `cogs/admin.py:1005`<br>Write: `cogs/admin.py:364` |
| **Auto-Role & Reaction Roles** (`autorole.py`) | `guild_settings` | `auto_role` | `role_select` | Read: `cogs/admin.py:236, 974`<br>Write: `cogs/admin.py:386, 394` |
| | `guild_settings` | `auto_role_bots` | `bool` | Read: `cogs/admin.py:976`<br>Write: `cogs/admin.py:395` |
| | `reaction_roles` | `message_id`, `emoji`, `role_id` | CRUD documents | Read: `cogs/admin.py:431, 492`<br>Write: `cogs/admin.py:459, 476` |
| **Moderation & Server Logs** (`moderation.py`) | `guild_settings` | `mod_enabled` | `bool` | Read: `cogs/moderation.py:23`, `cogs/admin.py:241`<br>Write: `cogs/admin.py:298` |
| | `guild_settings` | `log_channel` | `channel_select` | Read: `cogs/admin.py:231, 1040, 1066, 1091, 1109`<br>Write: `cogs/admin.py:118, 329` |
| | `guild_settings` | `log_event_filter` | `select` (`all\|mod\|messages\|members`) | Read: `cogs/admin.py:1041, 1067, 1092, 1110`<br>Write: `cogs/admin.py:331` |
| **AutoMod & Anti-Spam** (`automod.py`) | `guild_settings` | `automod_enabled` | `bool` | Read: `bot.py:189`, `cogs/ai.py:591`, `cogs/admin.py:247, 843`, `cogs/dashboard.py:1127`<br>Write: `cogs/admin.py:304, 556, 825` |
| | `guild_settings` | `banned_words` | `string_list` | Read: `bot.py:191`, `cogs/ai.py:593`, `cogs/admin.py:907`, `cogs/dashboard.py:1223`<br>Write: `cogs/admin.py:917, 927, 931`, `cogs/dashboard.py:1914` |
| | `guild_settings` | `antispam_threshold` | `int` (2..20) | Read: `bot.py:219`, `cogs/admin.py:263`<br>Write: `cogs/admin.py:557` |
| | `guild_settings` | `antispam_window` | `int` (2..30) | Read: `bot.py:220`, `cogs/admin.py:264`<br>Write: `cogs/admin.py:558` |
| | `guild_settings` | `antispam_action` | `select` (`mute\|kick\|ban\|warn`) | Read: `bot.py:221`, `cogs/admin.py:265`<br>Write: `cogs/admin.py:559` |
| | `guild_settings` | `antispam_mute_mins` | `int` (1..1440) | Read: `bot.py:222`<br>Write: `cogs/admin.py:560` |
| **Anti-Nuke** (`antinuke.py`) | `antinuke_config` | `enabled` | `bool` | Read: `cogs/antinuke.py:450, 538, 592, 639, 835`<br>Write: `cogs/antinuke.py:696` |
| | `antinuke_config` | `threshold` | `int` (3..50) | Read: `cogs/antinuke.py:457, 836`<br>Write: `cogs/antinuke.py:752` |
| | `antinuke_config` | `window` | `int` (3..30) | Read: `cogs/antinuke.py:458, 837`<br>Write: `cogs/antinuke.py:753` |
| | `antinuke_config` | `action` | `select` (`ban\|kick\|strip`) | Read: `cogs/antinuke.py:249, 838`<br>Write: `cogs/antinuke.py:754` |
| | `antinuke_config` | `log_channel` | `channel_select` | Read: `cogs/antinuke.py:250, 662, 840, 915`<br>Write: `cogs/antinuke.py:758` |
| | `antinuke_config` | `bot_join_protection` | `bool` | Read: `cogs/antinuke.py:641, 857`<br>Write: `cogs/antinuke.py:755` |
| | `antinuke_config` | `whitelist` | `user_multi_select` | Read: `cogs/antinuke.py:198, 792, 839`<br>Write: `cogs/antinuke.py:816, 822` |
| **Auto-Responder** (`autoresponse.py`) | `guild_settings` | `autoresponse_enabled` | `bool` | Read: `cogs/autoresponse.py:81` |
| | `auto_responses` | `keyword`, `response`, `match_mode`, `response_type`, `cooldown`, `case_sensitive`, `delete_trigger`, `enabled` | CRUD documents | Read: `cogs/autoresponse.py:35-58, 87-128, 240, 267`<br>Write: `cogs/autoresponse.py:200-213, 227, 288, 305` |
| **Leveling** (`leveling.py`) | `guild_settings` | `leveling_enabled` | `bool` | Read: `cogs/leveling.py:367, 428`, `cogs/admin.py:242`<br>Write: `cogs/admin.py:299, 596` |
| | `guild_settings` | `xp_cooldown` | `int` (10..600) | Read: `cogs/leveling.py:376`<br>Write: `cogs/admin.py:598` |
| | `guild_settings` | `levelup_channel` | `channel_select` | Read: `cogs/leveling.py:334`<br>Write: `cogs/admin.py:599` |
| | `guild_settings` | `levelup_message` | `textarea` | Read: `cogs/leveling.py:336`<br>Write: `cogs/admin.py:600` |
| | `guild_settings` | `xp_multiplier` | `float` (0.5..5.0) | Read: `cogs/leveling.py:388, 431`<br>Write: `cogs/admin.py:601` |
| **AI Chat** (`ai_chat.py`) | `guild_settings` | `ai_enabled` | `bool` | Read: `cogs/ai.py:607`, `cogs/admin.py:240, 841`<br>Write: `cogs/admin.py:297, 823` |
| | `guild_settings` | `default_ai_model` | `select` (`auto\|deepseek\|groq\|gemini`) | Read: `cogs/ai.py:77-91, 657`, `cogs/admin.py:256, 842`, `cogs/dashboard.py:1131, 1515`<br>Write: `cogs/admin.py:821`, `cogs/dashboard.py:1924` |
| | `guild_settings` | `ai_allowed_channels` | `channel_multi_select` | Read: `cogs/ai.py:613`, `cogs/admin.py:831, 845`<br>Write: `cogs/admin.py:828, 834` |
| **Tickets** (`tickets.py`) | `guild_settings` | `ticket_category` | `category_select` | Read: `cogs/tickets.py:152`, `cogs/admin.py:233`<br>Write: `cogs/admin.py:671` |
| | `guild_settings` | `ticket_support_role` | `role_select` | Read: `cogs/tickets.py:47, 66, 153`, `cogs/admin.py:237`<br>Write: `cogs/admin.py:672` |
| | `guild_settings` | `ticket_log_channel` | `channel_select` | Read: `cogs/tickets.py:107`<br>Write: `cogs/admin.py:673` |
| **Starboard** (`starboard.py`) | `guild_settings` | `starboard_channel` | `channel_select` | Read: `cogs/admin.py:234, 1129`<br>Write: `cogs/admin.py:710, 713` |
| | `guild_settings` | `starboard_threshold` | `int` (1..50) | Read: `cogs/admin.py:1130`<br>Write: `cogs/admin.py:714` |
| | `guild_settings` | `starboard_emoji` | `string` | Read: `cogs/admin.py:1131`<br>Write: `cogs/admin.py:715` |
| **Birthdays** (`birthdays.py`) | `guild_settings` | `birthday_channel` | `channel_select` | Read: `cogs/birthdays.py:110`<br>Write: `cogs/birthdays.py:172` |
| | `guild_settings` | `birthday_role` | `role_select` | Read: `cogs/birthdays.py:67, 74, 111`<br>Write: `cogs/birthdays.py:174` |
| **Modules, Custom Commands & Misc** (`misc_modules.py`) | `guild_settings` | `disabled_cogs` | `string_list` | Read: `bot.py:107`, `cogs/core.py:745, 803`, `cogs/dashboard.py:586`<br>Write: `cogs/dashboard.py:1070` |
| | `guild_settings` | `command_settings` | `dict` | Read: `cogs/core.py:749, 806`, `cogs/dashboard.py:587`<br>Write: `cogs/dashboard.py:1071` |
| | `guild_settings` | `disabled_cmds` | `string_list` (read-only in UI) | Read: `bot.py:108, 113` |
| | `guild_settings` | `misc_enabled` | `bool` | Read: `cogs/misc.py:174, 538`, `cogs/admin.py:246`, `cogs/dashboard.py:1643`<br>Write: `cogs/admin.py:303`, `cogs/dashboard.py:1935` |
| | `guild_settings` | `sports_enabled` | `bool` | Read: `cogs/sports.py:35`, `cogs/admin.py:244`<br>Write: `cogs/admin.py:301` |
| | `custom_commands` | `name`, `response`, `created_by` | CRUD documents | Read: `cogs/admin.py:748, 783`<br>Write: `cogs/admin.py:761, 767` |

### 3.2 Planned, Not Wired Fields (Excluded from UI Until Later Wire-Up Phase)
These fields are **not rendered in the UI** until the separate per-cog wire-up phase (1 cog per change, defaults matching today's hardcoded behavior, requiring your approval before editing any cog):
- `xp_per_message`: written at `cogs/admin.py:597`, not read at `cogs/leveling.py:382` (`random.randint(15, 25)`).
- `db.level_roles` `"type"` mismatch: `cogs/admin.py:641` writes `{guild_id, level, role_id}` without `"type"`, while `cogs/leveling.py:347` queries `{"guild_id": guild.id, "level": level, "type": xp_type}`.
- `tickets_enabled`: written at `cogs/admin.py:674`, not checked in `cogs/tickets.py:146`.
- `antispam_log_channel`: written at `cogs/admin.py:563`, not read in `bot.py:218-269`.
- `anime_enabled`: written at `cogs/admin.py:245, 302`, not read in `cogs/anime.py` (unverified in this session).
- `birthdays`: custom celebration message / server timezone (`cogs/birthdays.py:25-31, 54` hardcodes `_BIRTHDAY_MESSAGES` and IST `UTC+5:30`).
- `moderation`: `dm_on_punish`, `mute_role_id` (`cogs/moderation.py` uses Discord timeout and always attempts DM).
- `ai`: `per_channel_models`, `system_prompt_custom` (not read in `cogs/ai.py`).
- `giveaways` / `reminders`: custom limits/roles (hardcoded in `cogs/giveaways.py` and `cogs/reminders.py`).

---

## 4. Phase A2 Consolidation Design (Do Not Code in A1)
- One shared access-check helper plus bounded TTL caches (`bot.guild_config` for `guild_settings`, and a short-TTL cache for `global_blacklist` and `co_owners`), invalidated directly by dashboard saves and owner blacklist commands.
- Replaces the ~5 sequential MongoDB reads per command (`bot.py` `_core_security_check` and `_dashboard_module_check`, `cogs/core.py` `bot_check` and `global_slash_check`, `cogs/core.py` `co_owners`) and the per-message `guild_settings` read at `bot.py:186` with cached reads, keeping current behavior identical.
- Preserves `cogs/core.py`'s chaining to `self._original_interaction_check` (`cogs/core.py:707, 714, 831`), including reload safety.
- Adds user feedback for blocked commands (`bot.py:355` `on_command_error` with short `delete_after`, `bot.py:386` `on_app_command_error` with `ephemeral=True`).
- Keeps current bypass rules (Administrators skip channel/role rules but not module/subcommand disables; bot owner skips nothing).
- Keeps enforcing `disabled_cmds`, shows it read-only in the UI, and adds an approved one-time migration into `command_settings`.

---

## 5. Phase A1 Piece 1 Scope (Executing Now)
- **New files only** (zero existing files modified):
  - `webdash/__init__.py`
  - `webdash/security.py`
  - `webdash/auth.py`
  - `webdash/permissions.py`
  - `webdash/cache.py`
  - `webdash/audit.py`
  - `tests/test_webdash_core.py`
