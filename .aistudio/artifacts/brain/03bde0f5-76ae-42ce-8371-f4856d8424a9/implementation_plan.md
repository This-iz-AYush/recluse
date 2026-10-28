# Recluse Modular Web Dashboard — Checkpoints 3B, 3C, 3D & Phase A2 Plan

This plan covers the incremental completion of Recluse's `webdash` control panel on Render Free Tier (`512 MB` RAM, Python `3.14`, Motor/MongoDB behind Cloudflare Worker), starting with **Checkpoint 3B** (read-only Jinja2 ports in 3 reviewable batches matching legacy parity), followed by **Checkpoint 3C** (CSRF-protected mutation APIs with cache invalidation and audit logging), **Checkpoint 3D** (expanded wired-field scope), **Quart dependency cleanup**, and **Phase A2** (consolidating duplicate command/module/blacklist checks).

### User Review & Critical Decisions

> [!IMPORTANT]
> All four Phase 1 clarifications and your two follow-up notes are locked into this plan. Once you click **Proceed**, execution will start immediately with **Checkpoint 3B — Batch 1 (`automod` + `lockdown` + `misc` + `docs/webdash_plan.md` 3D split)**.

- **Confirmed Decision 1 — 3B Delivery Slicing**: Deliver Checkpoint 3B in three small, independently testable batches, adding each page to `build_sidebar_nav` (`webdash/router.py:339-352`) only as its handler and template land:
  1. **Batch 1**: `/automod/{guild_id}`, `/lockdown/{guild_id}`, `/misc/{guild_id}` (plus updating `docs/webdash_plan.md` to move expanded wired fields into a new **Checkpoint 3D** section and fix verified audit/citation mismatches).
  2. **Batch 2**: `/wizard/{guild_id}`, `/logs/{guild_id}`.
  3. **Batch 3**: `/modules/{guild_id}`, `/owner_panel`.
- **Confirmed Decision 2 — Protected Cogs & Recovery in `/modules/{guild_id}`**: Exclude `core`, `database`, `dashboard`, `owner`, and `jishaku` from normal disabling (`cogs/dashboard.py:620` + `core`). However, if a guild already has any of those 5 protected cogs stored in `disabled_cogs` (because the legacy UI allowed disabling `Core`), **still render that cog in `/modules/{guild_id}` with a clear `Protected` badge** so the server admin can switch it back on—never leaving a disabled cog with no way to re-enable it.
- **Confirmed Decision 3 — `/modules/{guild_id}` Command Controls**: Expose only the keys `cogs/core.py:749-777, 806-829` actually reads (`disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`) and render `disabled_cmds` (`bot.py:108, 113`) as a read-only badge list when non-empty. Do not expose an unwired per-command `disabled` boolean inside `command_settings`.
- **Confirmed Decision 4 — Legacy Parity First (3B & 3C) vs. Expanded Wired Scope (3D)**: Checkpoints **3B** and **3C** match the legacy `cogs/dashboard.py` field scope (`banned_words` in `/automod`, AI-only in `/wizard` using real `cogs/ai.py:77-87, 657` models `auto`/`deepseek`/`groq`/`gemini`, `misc_enabled`-only in `/misc`) so `WEBDASH_ENABLED=1` is a drop-in replacement. The expanded wired-field scope (`automod_enabled` toggle on `/automod`, `welcome_enabled`/`welcome_channel`/`log_channel`/`auto_role`/`automod_enabled`/`leveling_enabled`/`ai_enabled` on `/wizard`, and `ai_enabled`/`default_ai_model`/`sports_enabled` on `/misc`) moves under a new **Checkpoint 3D** heading in `docs/webdash_plan.md` after 3C is live and verified.

---

### 1. Overview & Core Concept

- **What It Does**: Ports the remaining guild management and Bot Owner views from inline HTML strings in `cogs/dashboard.py` into `aiohttp-jinja2` templates with `autoescape=True`, strict CSP compatibility (zero inline `on*=` attributes, zero local variables named `html`), RBAC permission checks (`webdash/permissions.py`), bounded in-memory caching (`GuildConfigCache`), and field-level audit logging (`AuditLogger`).
- **Target Audience / Persona**: Discord server owners, administrators, delegated moderators (`Manage Server` or RBAC role mappings), and the Recluse Bot Owner.
- **Key Value**: Full feature parity with the legacy dashboard so `WEBDASH_ENABLED=1` can remain enabled in production for all guilds, while retaining instant zero-downtime rollback (`WEBDASH_ENABLED=0`) and keeping legacy `cogs/dashboard.py` byte-for-byte untouched.

---

### 2. User Experience & Visual Design

- **Key User Flows**:
  - **Batch 1 (`automod`, `lockdown`, `misc`) — Done**:
    - **AutoMod (`GET /automod/{guild_id}`, requires `automod.view`)**: Displays the server's active `banned_words` list (`bot.py:191`, `cogs/ai.py:593`, `cogs/admin.py:907`, `cogs/dashboard.py:1243`, defaulting to `[]` when unset) in a read-only textarea and active filter badge pills (capped at 500 displayed entries with a `+N more` note when >500 words exist), with a clear read-only status notice until 3C lands.
    - **Lockdown (`GET /lockdown/{guild_id}`, requires `antinuke.view`)**: Displays the Emergency Protocols panel with the `Disabled: being rebuilt` banner and disabled action button (`cogs/dashboard.py:1374-1376`), matching the overview status card (`webdash/templates/overview.html:47`).
    - **Misc (`GET /misc/{guild_id}`, requires `settings.view`)**: Displays the `misc_enabled` (`cogs/misc.py:174, 538`, `cogs/admin.py:246`, `cogs/dashboard.py:1663`, default `True`) status toggle card for `/afk`, `/whois`, and `/serverinfo`.
  - **Batch 2 (`wizard`, `logs`) — Done**:
    - **Setup Wizard (`GET /wizard/{guild_id}`, requires `settings.view`)**: Matches legacy parity (`cogs/dashboard.py:1535-1606`) by displaying the Default AI Core selector, updated to the 4 real models wired in `cogs/ai.py:77-87, 91, 291-299, 401-411, 657, 743-754` (`auto`, `deepseek`, `groq`, `gemini`).
    - **Security & Audit Logs (`GET /logs/{guild_id}`, requires `logging.view`)**: Displays recent bot security events from `db.security_logs` (sorted by float `timestamp` descending per `cogs/dashboard.py:1425`, `bot.py:208, 266`) for all users with `logging.view`, and displays the `db.dashboard_audit` configuration change history section (sorted by UTC `created_at` descending per `webdash/audit.py:83, 138`) **only** to users with `audit.view` (`webdash/permissions.py:63`).
  - **Batch 3 (`modules`, `owner_panel`) — Next**:
    - **Modules & Commands (`GET /modules/{guild_id}`, requires `modules.view`)**: Accordion cards for loaded cogs (hiding `core`, `database`, `dashboard`, `owner`, `jishaku` unless currently present in `disabled_cogs`, in which case shown with a `Protected — Re-enable Only` badge), per-command channel/role multi-selects and subcommand switches (`cogs/core.py:749-777, 806-829`), and a read-only badge list for legacy `disabled_cmds` (`bot.py:108, 113`). All interactions use `data-*` attributes and `addEventListener` delegation.
    - **Owner Panel (`GET /owner_panel`, Bot Owner only)**: Displays loaded extensions (`bot.extensions`), both distinct maintenance flags read-only (`bot.global_lockdown` at `bot.py:33, 82` which blocks commands for non-owners, and `bot.dashboard_maintenance` at `cogs/dashboard.py:43, 122` which blocks dashboard access for non-owners, explicitly noting that the legacy panel showed neither), recent `db.visit_logs` (`cogs/dashboard.py:1777`), and active `db.ip_bans` (`cogs/dashboard.py:2024`).
- **Visual Identity & Theme**:
  - Inherits `webdash/templates/base.html:14-93`: deep obsidian `#09090e` canvas, translucent `#14151c/80` glass cards with `border-white/10`, responsive `hidden md:flex` sidebar (`webdash/templates/base.html:23`), and role badges (`Bot Owner`, `Server Owner`, `Administrator`).
- **Interactive Feedback & Motion**:
  - In Checkpoint 3B (read-only), form controls clearly indicate read-only preview mode (or disabled save button) until Checkpoint 3C enables live mutations. In Checkpoint 3C, save buttons use spinner/checkmark state transitions and inline toast banners (never `window.alert` or `window.confirm`).

---

### 3. Key Product Decisions & Trade-Offs

- **Decision 1 — Strict Legacy Parity in 3B/3C, Expanded Wired Scope in 3D**:
  - *Chosen Approach*: Keep `/automod`, `/wizard`, and `/misc` in 3B/3C focused on the exact settings legacy `cogs/dashboard.py` managed (replacing obsolete `nexusify`/`sarvam` model names with the real `auto`/`deepseek`/`groq`/`gemini` choices from `cogs/ai.py:77-87`), and schedule the additional wired fields in a dedicated **Checkpoint 3D**.
  - *Why*: Minimizes diff surface while validating templates, RBAC, CSRF, cache invalidation, and audit logging end-to-end.
- **Decision 2 — Protected Cog Recovery Path in `/modules/{guild_id}`**:
  - *Chosen Approach*: Normally filter out `{"core", "database", "dashboard", "owner", "jishaku"}` (case-insensitive). If any of those cog names is currently in the guild's `disabled_cogs` array (`bot.py:107`, `cogs/core.py:745, 803`), render the card with a `Protected` badge so it can be re-enabled in 3C, while 3C's `POST /api/modules/{guild_id}` validator forbids adding any protected cog to `disabled_cogs` (only allowing removal from `disabled_cogs`).
  - *Why*: Prevents servers that previously disabled `Core` in the legacy UI (`cogs/dashboard.py:620`) from being permanently stuck with `Core` disabled.
- **Decision 3 — Correcting `docs/webdash_plan.md` Mismatches**:
  - *Chosen Approach*: Update `docs/webdash_plan.md` in Batch 1 so its documentation matches verified reality:
    1. `db.dashboard_audit` uses `created_at` (with index `[("guild_id", 1), ("created_at", -1)]` and TTL `expireAfterSeconds=2592000` in `webdash/audit.py:19, 83-88, 138`), not `timestamp`/`expires_at`.
    2. `default_ai_model` citations point to `cogs/ai.py:77-91, 291-299, 401-411, 657, 743-754` (not `678-683`).
    3. `welcome_message` and `leave_message` defaults match `cogs/admin.py:354-355, 991-997, 1022-1027`.
    4. `command_settings` documents only the 5 keys read by `cogs/core.py:749-777, 806-829`.
    5. Section 8 splits into **8.1 Checkpoint 3B**, **8.2 Checkpoint 3C**, and **8.3 Checkpoint 3D (Expanded Wired-Field Scope)**.

---

### 4. Technical Architecture & Data Strategy *(Technical Reference)*

#### Architecture & Component Diagram

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                   Cloudflare Worker -> Render (Port 8080)                    │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                 webdash Security & Session Middlewares                       │
│  • Client IP resolution (ORIGIN_SHARED_SECRET / right-most X-Forwarded-For)  │
│  • Dual-pool InMemoryRateLimiter (pre_session / read / mutation / owner)     │
│  • SessionManager (db.webdash_sessions) + CSRF token check on POST/PUT/DEL   │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
        ┌───────────────────────────────┴───────────────────────────────┐
        ▼                                                               ▼
┌───────────────────────────────────────┐     ┌────────────────────────────────┐
│   Checkpoint 3B Read-Only Views       │     │  Checkpoint 3C Mutation APIs   │
│                                       │     │                                │
│ • Batch 1:                            │     │ • POST /api/settings/{gid}     │
│   - GET /automod/{gid} (automod.view) │     │   - update_automod             │
│   - GET /lockdown/{gid} (antinuke.vw) │     │   - update_ai_model            │
│   - GET /misc/{gid} (settings.view)   │     │   - toggle (toggleMisc)        │
│ • Batch 2:                            │     │   - toggle_lockdown (-> 501)   │
│   - GET /wizard/{gid} (settings.view) │     │ • POST /api/modules/{gid}      │
│   - GET /logs/{gid} (logging.view)    │     │   - disabled_cogs (allows      │
│ • Batch 3:                            │     │     removing protected cogs,   │
│   - GET /modules/{gid} (modules.view) │     │     blocks adding them)        │
│   - GET /owner_panel (Bot Owner)      │     │   - command_settings           │
│                                       │     │ • POST /api/owner_action       │
│   All rendered via aiohttp-jinja2     │     │ • POST /api/ip_action          │
│   with autoescape=True & data-* JS    │     │                                │
└───────────────────┬───────────────────┘     └────────────────┬───────────────┘
                    │                                          │
                    ▼                                          ▼
┌───────────────────────────────────────┐     ┌────────────────────────────────┐
│        GuildConfigCache & DB          │◄────┤ Cache Invalidation & Auditing  │
│ • GuildConfigCache (TTL 120s, 2.0s TO)│     │ • cfg_cache.invalidate(gid)    │
│ • db.guild_settings                   │     │ • AI._invalidate_guild_cfg(gid)│
│ • db.security_logs (timestamp: float) │     │ • AuditLogger.record(...) ->   │
│ • db.dashboard_audit (created_at: UTC)│     │   db.dashboard_audit           │
└───────────────────────────────────────┘     └────────────────────────────────┘
```

#### Data Model & State
- **`db.guild_settings`**:
  - Batch 1 reads `banned_words` (`list[str]`, default `[]` — `bot.py:191`, `cogs/ai.py:593`, `cogs/admin.py:907`), `automod_enabled` (`bool`, default `False` — `bot.py:189`, `cogs/ai.py:591`, `cogs/admin.py:247, 843`), and `misc_enabled` (`bool`, default `True` — `cogs/misc.py:174, 538`, `cogs/admin.py:246`, `cogs/dashboard.py:1663`).
  - Batch 2 reads `default_ai_model` (`"auto" | "deepseek" | "groq" | "gemini"`, default `"auto"` — `cogs/ai.py:77-87, 657`).
  - Batch 3 reads `disabled_cogs` (`list[str]`, default `[]` — `bot.py:107`, `cogs/core.py:745, 803`), `disabled_cmds` (`list[str]`, default `[]`, read-only — `bot.py:108, 113`), and `command_settings` (`dict[str, dict]`, keys `disabled_subcommands`, `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles` — `cogs/core.py:749-777, 806-829`).
- **`db.security_logs` & `db.dashboard_audit`**:
  - Queried in Batch 2 (`GET /logs/{guild_id}`) via `get_ready_db(bot)` with `asyncio.wait_for(..., timeout=2.0)`, bounded to 50 documents each, failing open to empty lists on timeout or when `db_ready` is `False`.
- **`db.visit_logs` & `db.ip_bans`**:
  - Queried in Batch 3 (`GET /owner_panel`) via `get_ready_db(bot)` with `asyncio.wait_for(..., timeout=2.0)`, bounded to 30 documents each, failing open to empty lists.

#### Interactive Component & State Mapping
- **Sidebar Progressive Disclosure (`build_sidebar_nav` in `webdash/router.py:339-352`)**:
  - Batch 1 adds:
    - `("automod", f"/automod/{guild_id}", "fa-shield-halved", "AutoMod", "automod.view")`
    - `("lockdown", f"/lockdown/{guild_id}", "fa-lock", "Lockdown", "antinuke.view")`
    - `("misc", f"/misc/{guild_id}", "fa-sliders", "Utilities & Misc", "settings.view")`
  - Batch 2 adds:
    - `("wizard", f"/wizard/{guild_id}", "fa-wand-magic-sparkles", "Setup Wizard", "settings.view")`
    - `("logs", f"/logs/{guild_id}", "fa-scroll", "Audit Logs", "logging.view")`
  - Batch 3 adds:
    - `("modules", f"/modules/{guild_id}", "fa-cubes", "Modules & Commands", "modules.view")`
- **Route & Test Lockstep Updates**:
  - In each batch, only the routes in that batch are switched from `_make_guild_not_available_handler` (`webdash/router.py:1144-1168`) to their real Jinja2 handlers; remaining unported routes continue returning `501` until their batch lands.
  - `tests/test_webdash_routes.py:634-709` (including renaming local variable `html` at line `650` to `page_text`) is updated in each batch to test `200` (authorized), `403` (unauthorized), `404` (bot not in guild), and `302` (logged out) for the newly landed routes while keeping `501` assertions for still-unported routes.
