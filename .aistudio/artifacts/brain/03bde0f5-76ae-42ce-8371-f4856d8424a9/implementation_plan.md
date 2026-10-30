# Implementation Plan — Pre-3C Fix & Checkpoint 3C (Mutation Endpoints)

## 1. Architecture & Global Rules for Checkpoint 3C

1. **Zero Cog Changes & Legacy Parity**:
   - Do **not** modify `bot.py` or any file in `cogs/` (`cogs/dashboard.py` legacy path when `WEBDASH_ENABLED=0` and lazy `webdash` imports remain untouched).
   - Ignore AI Studio web preview / dev server messages; never alter project configs for web preview.
2. **Optimistic Concurrency via `dashboard_rev`**:
   - `dashboard_rev` is an integer field on `db.guild_settings` read and mutated **exclusively** by `webdash`. Missing or non-`int` (or `bool`) values are treated as `0`.
   - **Fresh Read on Edit Pages**: `GET /automod/{guild_id}`, `GET /wizard/{guild_id}`, `GET /misc/{guild_id}`, and `GET /modules/{guild_id}` fetch `db.guild_settings.find_one({"guild_id": gid})` **directly from Mongo** (bypassing `GuildConfigCache`) so the rendered controls and `data-dashboard-rev="{{ dashboard_rev }}"` always reflect the latest persisted state. Read-only pages (`/overview/{guild_id}`, `/lockdown/{guild_id}`, `/logs/{guild_id}`) continue using `GuildConfigCache`.
   - **Delta Payloads + Conditional Write**:
     - Client sends `dashboard_rev` (must be `int` and not `bool`, `>= 0`) plus only the changed fields.
     - Server reads the current document (`before_doc = await db.guild_settings.find_one({"guild_id": gid})`) for `before` audit snapshot and validation (such as checking existing `disabled_cogs`).
     - Conditional Mongo update:
       - If `expected_rev == 0`:
         - If `before_doc is None`: `update_one({"guild_id": gid, "dashboard_rev": {"$exists": False}}, {"$set": {**updates, "guild_id": gid}, "$inc": {"dashboard_rev": 1}}, upsert=True)` (catching `DuplicateKeyError` as a `409 Conflict`).
         - If `before_doc is not None`: `update_one({"guild_id": gid, "$or": [{"dashboard_rev": 0}, {"dashboard_rev": {"$exists": False}}]}, {"$set": updates, "$inc": {"dashboard_rev": 1}}, upsert=False)`.
       - If `expected_rev > 0`:
         - `update_one({"guild_id": gid, "dashboard_rev": expected_rev}, {"$set": updates, "$inc": {"dashboard_rev": 1}}, upsert=False)`.
     - If `matched_count == 0` (and not upserted), return HTTP `409 Conflict` with `{"error": "Someone else changed these settings. Please reload the page and try again."}` and perform no audit write.
     - On success, return `{"status": "ok", "dashboard_rev": expected_rev + 1, ...}` so the client updates its local `dashboard_rev` for subsequent saves without reloading.
3. **Cache Invalidation After Every Guild Write**:
   - Call `GuildConfigCache.invalidate(gid)` (`webdash/cache.py:42`).
   - Call `ai_cog = bot.get_cog("AI")` and if `ai_cog and hasattr(ai_cog, "_invalidate_guild_cfg"): ai_cog._invalidate_guild_cfg(gid)` (`cogs/ai.py:589-590`).
4. **Audit Logging (`AuditLogger.record`)**:
   - Every successful mutation records an entry in `db.dashboard_audit` (`webdash/audit.py:58-120`) with `guild_id`, `actor_id`, `actor_tag`, `action`, `before`, `after`, and trusted `ip`.
5. **Template & JS Safety**:
   - Jinja2 `autoescape=True` stays enabled; zero inline event handlers (`on*=`); zero `window.alert` / `window.confirm` / `window.open`; never name any local variable `html`.

---

## 2. Small-Batch Slicing

### Batch 0 — Pre-3C Fix: Exact-Case `is_disabled` in `/modules/{guild_id}`

- **Files**:
  - `webdash/router.py` (around lines 1514–1585)
  - `tests/test_webdash_routes.py`
- **Changes**:
  - Remove `disabled_cogs_lower` from the `is_disabled` calculation in `GET /modules/{guild_id}`.
  - Compute `is_disabled = cog_name_str in disabled_cogs` using **exact, case-sensitive** string matching to match `bot.py:110` (`if cog_name and cog_name in disabled_cogs:`) and `cogs/core.py:745, 803` (`if cog_name and cog_name in settings.get("disabled_cogs", []):`).
  - Keep `PROTECTED_COGS_LOWER` (`webdash/router.py:57-66`) case-insensitive **only** for deciding whether a cog is protected (`is_protected = cog_lower in PROTECTED_COGS_LOWER`) and therefore hidden when `not is_disabled`.
  - Track seen cog names by exact string (`seen_cog_names: set[str]`) when appending unloaded/extra `disabled_cogs` entries (`webdash/router.py:1557-1561`) so that a bogus lowercase entry like `"core"` in `disabled_cogs` does **not** mark the loaded `"Core"` cog as disabled (and does not expose `"Core"` via the recovery rule). Also filter extra unloaded entries so any entry matching `PROTECTED_COGS_LOWER` with wrong case (like `"core"`) is not rendered as a disabled protected cog.
- **Tests (`tests/test_webdash_routes.py`)**:
  - Update fixture `disabled_cogs` from `["sports"]` to `["Sports"]` where testing a legitimately disabled cog.
  - Add `test_modules_lowercase_core_not_shown_as_disabled`: seed `disabled_cogs: ["core"]` in `db.guild_settings`, load `GET /modules/{guild_id}`, and assert `"Core"` is **not** rendered as disabled (and is hidden as an enabled protected cog).

---

### Batch 3C-1 — `POST /api/settings/{guild_id}` + Lockdown 501 + Edit Page Fresh Reads & UI Wiring

- **Files**:
  - `webdash/router.py`
  - `webdash/templates/automod.html`
  - `webdash/templates/wizard.html`
  - `webdash/templates/misc.html`
  - `tests/test_webdash_routes.py`
- **Changes**:
  1. **Fresh Mongo Reads on Edit Pages**:
     - Update `GET /automod/{guild_id}` (`webdash/router.py:1138`), `GET /misc/{guild_id}` (`webdash/router.py:1209`), and `GET /wizard/{guild_id}` (`webdash/router.py:1232`) to read `db.guild_settings.find_one({"guild_id": gid})` directly (bypassing `GuildConfigCache`) and pass `dashboard_rev` (`int`, default `0`) to the templates.
  2. **Endpoint `POST /api/settings/{guild_id}`**:
     - Requires valid session, guild presence (`bot.get_guild(gid)`), and guild membership (`member`). CSRF is already enforced by `SecurityMiddleware` (`webdash/security.py:551-579`).
     - Parses JSON object body (rejects non-dict, oversized bodies `> 64 KB`).
     - Dispatches strictly on `body.get("type")`:
       - **`"toggle_lockdown"`**:
         - Checks `PermissionService.has_permission(..., "lockdown.execute")`.
         - Returns HTTP `501 Not Implemented` (`{"error": "Lockdown execution is disabled in Checkpoint 3C pending safe permission backup/restore parity."}`) with zero DB writes.
       - **`"update_automod"`**:
         - Checks `PermissionService.has_permission(..., "automod.edit")`.
         - Validates `dashboard_rev` (`isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0`).
         - Validates `banned_words`: must be a `list` of at most `200` items; every item must be `str` (`not isinstance(..., str)` -> `400`), stripped & lowercased, `1 <= len(w) <= 64`, no control characters (`ord(c) < 32`), deduplicated preserving order.
         - Writes `{"banned_words": cleaned_words}` (exact shape read by `bot.py:191-193` and `cogs/ai.py:593-595`) with `dashboard_rev` conditional update.
         - Invalidates `GuildConfigCache.invalidate(gid)` + `ai_cog._invalidate_guild_cfg(gid)`.
         - Records `AuditLogger.record(..., action="automod.update_banned_words", before={"banned_words": ...}, after={"banned_words": ...})`.
       - **`"update_ai_model"`**:
         - Checks `PermissionService.has_permission(..., "settings.edit")`.
         - Validates `dashboard_rev` and `model` (`isinstance(model, str)` and `model in {"auto", "deepseek", "groq", "gemini"}`).
         - Writes `{"default_ai_model": model}` (exact shape read by `cogs/ai.py:77-91, 291-299, 401-411, 657`) with `dashboard_rev` conditional update.
         - Invalidates `GuildConfigCache.invalidate(gid)` + `ai_cog._invalidate_guild_cfg(gid)`.
         - Records `AuditLogger.record(..., action="settings.update_ai_model", before={"default_ai_model": ...}, after={"default_ai_model": ...})`.
       - **`"toggle"`**:
         - Validates `body.get("module") == "toggleMisc"` (any other module returns `400`).
         - Checks `PermissionService.has_permission(..., "settings.edit")`.
         - Validates `dashboard_rev` and `enabled` (`isinstance(enabled, bool)`).
         - Writes `{"misc_enabled": enabled}` (exact shape read by `cogs/misc.py:174, 538`) with `dashboard_rev` conditional update.
         - Invalidates `GuildConfigCache.invalidate(gid)` + `ai_cog._invalidate_guild_cfg(gid)`.
         - Records `AuditLogger.record(..., action="settings.toggle_misc", before={"misc_enabled": ...}, after={"misc_enabled": ...})`.
       - Any unknown `type` returns `400 Bad Request`.
  3. **Template & External Event Listener Wiring (`automod.html`, `wizard.html`, `misc.html`)**:
     - Store `data-dashboard-rev="{{ dashboard_rev }}"` and `data-guild-id="{{ guild_id_str }}"`.
     - Enable controls when `can_edit` is `True` and attach event listeners via `addEventListener` in `{% block extra_scripts %}` using `fetch` with `X-CSRF-Token: getCsrfToken()`, updating `dataset.dashboardRev` on `200` and displaying inline status banners on `200`/`400`/`409`.
- **Tests (`tests/test_webdash_routes.py`)**:
  - Happy-path + DB shape + cache invalidation + `ai_cog._invalidate_guild_cfg` + `AuditLogger` diff tests for `update_automod`, `update_ai_model`, `toggleMisc`, and `toggle_lockdown` (`501`).
  - Optimistic concurrency tests: missing `dashboard_rev` in DB (`0` -> `1`), subsequent save (`1` -> `2`), stale `dashboard_rev` returning `409 Conflict`, and verifying edit pages bypass `GuildConfigCache`.
  - Attack tests: missing/wrong CSRF (`403`), non-member outsider (`403`), member with `*.view` only (`403`), wrong types (`banned_words: "bad"`, `banned_words: [123]`, `enabled: "true"`, `dashboard_rev: True`), oversized list (`201` words, `65`-char word), invalid AI model (`"gpt-4"`), control characters/hostile strings (`"<script>"`, `"\x00"`).

---

### Batch 3C-2 — `POST /api/modules/{guild_id}` + `modules.html` Interactive Wiring

- **Files**:
  - `webdash/router.py`
  - `webdash/templates/modules.html`
  - `tests/test_webdash_routes.py`
- **Changes**:
  1. **Fresh Mongo Read on `GET /modules/{guild_id}`**:
     - Read `db.guild_settings.find_one({"guild_id": gid})` directly (bypassing `GuildConfigCache`) and pass `dashboard_rev` (`int`, default `0`) into `modules.html`.
  2. **Endpoint `POST /api/modules/{guild_id}`**:
     - Requires valid session, guild presence, and guild membership.
     - Accepts a delta JSON payload containing `dashboard_rev` (`int`, not `bool`, `>= 0`) and at least one of `disabled_cogs` or `command_settings` (rejects any unknown top-level keys).
     - **Permission enforcement per field**:
       - If `"disabled_cogs"` is in payload: requires `PermissionService.has_permission(..., "modules.edit")`.
       - If `"command_settings"` is in payload: requires `PermissionService.has_permission(..., "commands.edit")`.
     - **`disabled_cogs` Validation (Exact Shape for `bot.py:107, 110` and `cogs/core.py:745, 803`)**:
       - Must be `list[str]` (max `50` entries, deduplicated preserving order).
       - Loaded cog names set: `loaded_cogs = set(getattr(bot, "cogs", {}).keys())` (exact case).
       - Existing disabled cogs in DB: `existing_disabled = set(before_doc.get("disabled_cogs", []))` (if list of str).
       - For each `cog_name` in submitted `disabled_cogs`:
         - Must be `isinstance(cog_name, str)` and `1 <= len(cog_name) <= 64`.
         - **Protected cog rule**: if `cog_name.lower() in PROTECTED_COGS_LOWER`:
           - If `cog_name not in existing_disabled`: reject with `400 Bad Request` (*protected cogs can never be added to `disabled_cogs`, only kept if already present or removed*).
         - **Exact-case loaded cog rule**: `cog_name` must be in `loaded_cogs` or in `existing_disabled` (otherwise reject with `400 Bad Request`).
     - **`command_settings` Validation (Exact Shape for `cogs/core.py:749-777, 806-829`)**:
       - Must be `dict` (max `100` command entries).
       - Build catalog of valid top-level commands and their valid subcommand names from `bot.commands` and `bot.tree.get_commands()` (same discovery logic as `_collect_guild_commands` in `webdash/router.py:1430-1499`).
       - Build live guild channel ID set (`valid_channel_ids = {str(ch.id) for ch in guild.channels if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel))}`) and live guild role ID set (`valid_role_ids = {str(r.id) for r in guild.roles if not r.is_default()}`).
       - For each `cmd_name, cfg` in submitted `command_settings.items()`:
         - `cmd_name` must exist in the discovered top-level command catalog (`400` otherwise).
         - `cfg` must be a `dict` whose keys are a subset of `{"disabled_subcommands", "allowed_channels", "ignored_channels", "allowed_roles", "ignored_roles"}` (`400` on any extra key).
         - Each value list must be `list[str]` (max `100` items, deduplicated):
           - `disabled_subcommands`: every item must exist in that command's valid subcommands set (`400` otherwise).
           - `allowed_channels`, `ignored_channels`: every item must be in `valid_channel_ids` (`400` otherwise).
           - `allowed_roles`, `ignored_roles`: every item must be in `valid_role_ids` (`400` otherwise).
     - Writes only the changed whitelisted field(s) (`disabled_cogs` and/or `command_settings`) using the `dashboard_rev` conditional update (`409` on mismatch).
     - Invalidates `GuildConfigCache.invalidate(gid)` + `ai_cog._invalidate_guild_cfg(gid)`.
     - Records `AuditLogger.record(...)` with `before` and `after` snapshots of the modified field(s).
  3. **Template & JS Wiring (`modules.html`)**:
     - Enable module toggles when `can_edit_modules` is `True` and command override controls/save button when `can_edit_commands` is `True`.
     - Send only the modified section (`disabled_cogs` or `command_settings`) + `dashboard_rev` to `POST /api/modules/{guild_id}`, update `dataset.dashboardRev` on success, and show inline feedback.
- **Tests (`tests/test_webdash_routes.py`)**:
  - Happy-path tests for `disabled_cogs` (disabling `"Sports"`, re-enabling a previously disabled protected cog `"Core"` via recovery) and `command_settings` (subcommands, channels, roles).
  - Attack & validation tests: attempting to add `"Core"` or `"core"` to `disabled_cogs` (`400`), wrong-case `"sports"` (`400`), unloaded cog `"FakeCog"` (`400`), user with `modules.edit` trying to write `command_settings` (`403`), user with `commands.edit` trying to write `disabled_cogs` (`403`), non-existent command or subcommand (`400`), channel/role ID from another guild or `@everyone` role (`400`), extra keys inside `command_settings[cmd]` (`400`), stale `dashboard_rev` (`409`).

---

### Batch 3C-3 — Owner Actions (`POST /api/owner_action`, `POST /api/ip_action`), 60s Cached IP Ban Enforcement & Post-3C Check

- **Files**:
  - `webdash/cache.py`
  - `webdash/security.py`
  - `webdash/router.py`
  - `webdash/templates/owner_panel.html`
  - `tests/test_webdash_routes.py`
  - `tests/test_webdash_core.py`
- **Changes**:
  1. **Bounded 60s In-Memory IP Ban Cache (`IpBanCache` in `webdash/cache.py` + `SecurityMiddleware` in `webdash/security.py`)**:
     - Add `IpBanCache` holding a bounded `frozenset[str]` (up to `2,000` normalized IP strings) with `ttl_seconds = 60.0`.
     - Refresh lazily at most once per 60 seconds via `db.ip_bans.find({}, {"ip": 1}).limit(2000)`. If `db` is `None` or Mongo raises an exception, **fail open** (keep previous cached set or empty set and set retry timestamp so it never hammers Mongo).
     - Provide `IpBanCache.refresh_now(db)` / `IpBanCache.add_local(ip)` / `IpBanCache.remove_local(ip)` so `ban`/`unban` updates the in-memory set immediately.
     - In `SecurityMiddleware` (`webdash/security.py`):
       - **Never block** `GET /health` or unauthenticated `GET /` (cron pinger: if `request.method == "GET"` and `request.path == "/"` and there is no valid authenticated session cookie, bypass IP ban check).
       - For all other requests, check the trusted client IP (`get_client_ip(request)`) against `IpBanCache`. If banned, return `403 Forbidden`.
  2. **Endpoint `POST /api/owner_action`**:
     - Enforces `_is_bot_owner(bot, user_id)` (`403` for non-owners, including guild owners/admins).
     - Validates `action == "reload_cog"` and `cog` (`isinstance(cog, str)`, regex `^[a-z0-9_]{1,32}$`, and `f"cogs.{cog}" in getattr(bot, "extensions", {})`).
     - Calls `await bot.reload_extension(f"cogs.{cog}")` (`cogs/dashboard.py:1989`).
     - Records `AuditLogger.record(..., guild_id=0, action="owner.reload_cog", before={"cog": cog}, after={"cog": cog, "reloaded": True})`.
  3. **Endpoint `POST /api/ip_action`**:
     - Enforces `_is_bot_owner(bot, user_id)` (`403` for non-owners).
     - Validates `action in {"ban", "unban"}` and `ip` (`isinstance(ip, str)`, parsed and normalized via `ipaddress.ip_address(ip.strip())`).
     - On `"ban"`:
       - Refuses (`400 Bad Request`) if `parsed_ip` equals the requester's current trusted client IP (`ipaddress.ip_address(get_client_ip(request))`), or if `parsed_ip.is_loopback` / `parsed_ip.is_unspecified`.
       - Upserts `await db.ip_bans.update_one({"ip": normalized_ip}, {"$set": {"banned_at": time.time()}}, upsert=True)` (`cogs/dashboard.py:2024-2028`).
     - On `"unban"`:
       - Deletes `await db.ip_bans.delete_one({"ip": normalized_ip})` (`cogs/dashboard.py:2030`).
     - Immediately updates/refreshes `IpBanCache` and records `AuditLogger.record(..., guild_id=0, action=f"owner.ip_{action}", before=..., after=...)`.
  4. **Template & JS Wiring (`owner_panel.html`)**:
     - Enable reload buttons, IP ban form, and IP unban buttons using `addEventListener` in `{% block extra_scripts %}` with inline status banners.
  5. **Post-3C `quart` / `quart-cors` Audit**:
     - Inspect the entire repository for any imports/usages of `quart` or `quart-cors` and report before touching `requirements.txt`.
- **Tests (`tests/test_webdash_routes.py` & `tests/test_webdash_core.py`)**:
  - `IpBanCache` unit & integration tests: 60s caching (verifying zero per-request Mongo queries when cache is warm), fail-open when DB errors, banned trusted IP gets `403`, spoofed `X-Forwarded-For` from untrusted peer cannot trigger or bypass ban, `GET /health` and unauthenticated `GET /` are **never** blocked even for a banned IP, authenticated `GET /` or `/servers` is blocked for a banned IP.
  - Owner endpoints: non-owner admin rejected (`403`), missing CSRF (`403`), valid `reload_cog` calls `bot.reload_extension("cogs.sports")`, invalid/unloaded cog rejected (`400`), path traversal `"../bot"` rejected (`400`), banning own current IP rejected (`400`), invalid IP string rejected (`400`), `ban`/`unban` updates `db.ip_bans` and refreshes `IpBanCache` immediately.
