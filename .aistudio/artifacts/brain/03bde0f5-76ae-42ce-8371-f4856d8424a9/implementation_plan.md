# Implementation Plan — AI Chat Toggle (`wizard.html`) & AutoMod Toggle (`automod.html`)

Interactive toggle switches for **AI Chat** (`ai_enabled`) on the Setup Wizard page and **AutoMod** (`automod_enabled`) on the AutoMod page, backed by `POST /api/settings/{guild_id}` with strict boolean validation, optimistic concurrency (`dashboard_rev`), immediate cache invalidation, and audit logging.

## User Review & Critical Decisions

> [!IMPORTANT]
> Both the **AI Chat toggle** (`toggleAiChat` → `ai_enabled`, default `True`) and the optional **AutoMod toggle** (`toggleAutomod` → `automod_enabled`, default `False`) are included in this plan. Neither `bot.py` nor any file under `cogs/` will be modified.

- **Exact `ai_enabled` Behavior Verified from `cogs/ai.py` & `cogs/admin.py`**:
  - **`cogs/ai.py:607-609` (`AI.on_message`)**: After ignoring bots (`line 571`), globally blacklisted users (`line 575`), running the AI-cog banned-word check if `automod_enabled` is truthy (`lines 591-599`), and checking `if self.bot.user not in message.mentions: return` (`lines 603-605`), `AI.on_message` checks `if not cfg.get("ai_enabled", True): return` (`lines 607-609`). Missing `ai_enabled` defaults to `True`.
  - **Slash commands in `cogs/ai.py` do NOT check `ai_enabled`**:
    - `/choose_ai` (`cogs/ai.py:399-423`) — sets `self._model_pref[interaction.user.id]`; never reads `ai_enabled`.
    - `/clear_memory` (`cogs/ai.py:427-437`) — clears `self._memory[interaction.user.id]`; never reads `ai_enabled`.
    - `/imagine` (`cogs/ai.py:441-523`) — checks `self._blacklisted` (`line 445`) and generates images via Pollinations.ai; never reads `ai_enabled`.
    - `/describe` (`cogs/ai.py:527-563`) — analyzes an attached image via `self._gemini`; never reads `ai_enabled`.
  - **`cogs/admin.py` readers & writers of `ai_enabled`**:
    - `cogs/admin.py:66-77` (`_save_settings`) — updates `db.guild_settings` and calls `ai_cog._invalidate_guild_cfg(guild_id)` (`lines 75-77`, matching `cogs/ai.py:243-269`).
    - `cogs/admin.py:240` (`/config`) — displays `"AI": cfg.get("ai_enabled", True)`.
    - `cogs/admin.py:278, 297, 306` (`/modules`) — maps choice `"ai"` ("AI Chat") to `"ai_enabled"`.
    - `cogs/admin.py:797, 812, 822-823, 837, 841` (`/aiconfig`) — updates `update["ai_enabled"] = ai_enabled` and displays `"✅" if cfg.get("ai_enabled", True) else "❌"`.
  - **UI Copy for `wizard.html`**:
    > *"Turning AI Chat off stops `@Recluse` mention replies in channels, but does not disable slash commands like `/imagine`, `/describe`, `/choose_ai`, or `/clear_memory` (disable the `AI` module under Modules & Commands to block all AI commands)."*
- **Exact `automod_enabled` Behavior Verified from `bot.py`, `cogs/ai.py`, & `cogs/admin.py`**:
  - **`bot.py:189` (`Recluse.on_message`)**: Checks `if cfg.get("automod_enabled"):` (default `False` when missing) to gate both banned-word deletion/strikes (`bot.py:191-216`) and anti-spam rate tracking/punishments (`bot.py:219-268`).
  - **`cogs/ai.py:591` (`AI.on_message`)**: Checks `if cfg.get("automod_enabled") and message.guild:` (cached in `AI._guild_cfg`, invalidated via `AI._invalidate_guild_cfg` at `cogs/ai.py:267-269`) to gate `AI._automod_strike` on `banned_words` (`cogs/ai.py:593-599`).
  - **`cogs/admin.py:247, 285, 304, 556, 798, 824-825, 843`**: Reads `cfg.get("automod_enabled", False)` in `/config` (`line 247`) and `/aiconfig` (`line 843`), toggles `"automod_enabled"` in `/modules` (`lines 285, 304`) and `/aiconfig` (`lines 824-825`), and sets `"automod_enabled": True` in `/antispam` (`line 556`).

---

## 1. Overview & Core Concept

- **What It Does**:
  1. Converts the read-only `AI Chat: Enabled/Disabled` badge on `/wizard/{guild_id}` into an interactive toggle control paired with the live status badge and accurate scope explanation.
  2. Converts the read-only `AutoMod: Active/Disabled` badge on `/automod/{guild_id}` into an interactive toggle control paired with the live status badge.
- **Target Audience / Persona**: Server owners, Discord administrators, and delegated staff holding `ai.edit` or `automod.edit` dashboard permissions.
- **Key Value**: Lets administrators immediately pause or resume `@Recluse` AI chat responses (`ai_enabled`) or server-wide AutoMod filtering (`automod_enabled`) from the web dashboard with optimistic concurrency protection and full audit visibility.

---

## 2. User Experience & Visual Design

- **Key User Flows**:
  1. **Setup Wizard (`/wizard/{guild_id}`)**:
     - Header displays the `AI Chat: Enabled` / `AI Chat: Disabled` badge (`#aiChatStatusBadge`) alongside the `#toggleAiChat` switch (enabled when `can_edit_ai` is `True`, disabled/read-only otherwise).
     - Below the header description, the accurate explanation sentence clarifies that turning AI Chat off stops `@Recluse` mention replies while `/imagine`, `/describe`, `/choose_ai`, and `/clear_memory` remain available unless the `AI` module is disabled under Modules & Commands.
     - Toggling `#toggleAiChat` sends `POST /api/settings/{guild_id}` with `{"type": "toggle", "module": "toggleAiChat", "enabled": <bool>, "dashboard_rev": N}`, updates `data-dashboard-rev` and `#aiChatStatusBadge` live on `200 OK`, or reverts the checkbox and displays the error in `#wizardStatus` on `409`/`503`/network failure.
  2. **AutoMod (`/automod/{guild_id}`)**:
     - Header displays the `AutoMod: Active` / `AutoMod: Disabled` badge (`#automodStatusBadge`) alongside the `#toggleAutomod` switch (enabled when `can_toggle_automod` / `automod.edit` is granted, disabled/read-only otherwise).
     - Toggling `#toggleAutomod` sends `POST /api/settings/{guild_id}` with `{"type": "toggle", "module": "toggleAutomod", "enabled": <bool>, "dashboard_rev": N}`, updates `data-dashboard-rev` and `#automodStatusBadge` live on `200 OK`, or reverts the checkbox and displays the error in `#automodStatus` on `409`/`503`/network failure.
- **Visual Identity & Theme**:
  - Matches the existing glassmorphism dark theme (`bg-[#14151c]/80`, `border-white/10`) and the exact toggle switch styling used in `misc.html` (`w-14 h-7` pill switch with `peer-checked:bg-blue-600/80`).
  - Zero inline event handlers (`on*=`); all listeners attach in `DOMContentLoaded` inside `{% block scripts %}`.

---

## 3. Key Product Decisions & Trade-Offs

- **Decision 1: Separate Permission Checks per Toggle Module in `POST /api/settings/{guild_id}`**
  - *Chosen Approach*: Dispatch `type == "toggle"` on `module`:
    - `"toggleMisc"` → requires `settings.edit`, writes `{"misc_enabled": enabled}`, audit `module="misc"`, `action="toggle_misc"`.
    - `"toggleAiChat"` → requires `ai.edit`, writes `{"ai_enabled": enabled}`, audit `module="ai"`, `action="toggle_ai_chat"`.
    - `"toggleAutomod"` → requires `automod.edit`, writes `{"automod_enabled": enabled}`, audit `module="automod"`, `action="toggle_automod"`.
  - *Why*: Enforces least-privilege RBAC so a user granted only `automod.edit` cannot toggle `ai_enabled` or `misc_enabled`, and a user granted only `ai.edit` cannot toggle `automod_enabled` or `misc_enabled`.
- **Decision 2: Strict Single-Key Delta Writes & Immediate AI Cog Cache Invalidation**
  - *Chosen Approach*: Reuse `_apply_guild_settings_mutation` so only the single target key (`{"ai_enabled": enabled}` or `{"automod_enabled": enabled}`) is `$set` alongside `$inc: {"dashboard_rev": 1}`, followed by `_invalidate_guild_caches(request, guild_id)` which invalidates `GuildConfigCache` and calls `ai_cog._invalidate_guild_cfg(guild_id)`.
  - *Why*: `cogs/ai.py` caches `guild_settings` in `self._guild_cfg[guild_id]` (`cogs/ai.py:249-263`) and reads both `ai_enabled` (`line 607`) and `automod_enabled` (`line 591`) from that cache on every message; calling `_invalidate_guild_cfg(guild_id)` makes both toggles take effect on the very next Discord message.

---

## 4. Technical Architecture & Data Strategy *(Technical Reference)*

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Browser (wizard.html / automod.html)                 │
│  • #toggleAiChat  ──► POST /api/settings/{gid} {type:"toggle",           │
│                       module:"toggleAiChat", enabled:bool, dashboard_rev}│
│  • #toggleAutomod ──► POST /api/settings/{gid} {type:"toggle",           │
│                       module:"toggleAutomod", enabled:bool,dashboard_rev}│
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ X-CSRF-Token + Session Cookie
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  handle_api_settings (webdash/router.py)                 │
│  1. Validate body keys: {"type","module","enabled","dashboard_rev",...}  │
│  2. Validate module in {"toggleMisc", "toggleAiChat", "toggleAutomod"}   │
│  3. Strict bool check: isinstance(enabled, bool) (reject "true", 1, None)│
│  4. resolve_guild_access(..., perm, fresh_settings=True,                 │
│                          strict_settings_read=True) -> 403 / 404 / 503   │
│  5. _apply_guild_settings_mutation:                                      │
│     • Conditional update_one on dashboard_rev (409 on mismatch)          │
│     • $set ONLY {"ai_enabled": enabled} or {"automod_enabled": enabled}  │
│     • _invalidate_guild_caches -> GuildConfigCache + AI._invalidate_...  │
│     • AuditLogger.record (non-empty changes: {key: {old, new}})          │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Interactive Component & State Mapping**:
  - `wizard.html`: `#toggleAiChat` (`input[type="checkbox"]`) + `#aiChatStatusBadge` (`span`). Shares `data-dashboard-rev` on `#wizardRoot` with `#saveBtn` (`update_ai_model`), so toggling AI Chat and saving the default AI model in any order stays in sync without page reloads.
  - `automod.html`: `#toggleAutomod` (`input[type="checkbox"]`) + `#automodStatusBadge` (`span`). Shares `data-dashboard-rev` on `#automodRoot` with `#saveWordsBtn` (`update_automod`), and remains usable by users with `automod.edit` (`can_toggle_automod`) even when `over_display_cap` locks the `banned_words` textarea.
- **Verification & Test Plan (Python 3.14)**:
  - Add comprehensive tests covering:
    - RBAC permission matrix for `toggleAiChat` (`ai.edit` allowed `200`; `*.view` only `403`; `automod.edit` only `403`; `settings.edit` only `403`; non-member outsider `403`) and `toggleAutomod` (`automod.edit` allowed `200`; `*.view` only `403`; `ai.edit` only `403`; non-member `403`).
    - Strict boolean validation (`"true"`, `1`, `0`, `None`, `{}` all rejected with `400`).
    - Optimistic concurrency (`409` on stale `dashboard_rev`; sequential `toggleAiChat` + `update_ai_model` incrementing `dashboard_rev` `0 -> 1 -> 2`).
    - Strict pre-write read (`503` when `db.guild_settings.find_one` fails, leaving stored document untouched).
    - Single-key isolation (only `ai_enabled` or `automod_enabled` + `dashboard_rev` changes in `db.guild_settings`), non-empty `dashboard_audit` row (`changes["ai_enabled"] == {"old": True, "new": False}` even when `ai_enabled` was initially absent or unchanged), and `ai_cog._invalidate_guild_cfg(guild_id)` invocation.
    - End-to-end verification with real `AI` cog (`cogs/ai.py`) confirming `_invalidate_guild_cfg` evicts `_guild_cfg[guild_id]` and `AI.on_message` stops replying to `@bot` mentions when `ai_enabled` is toggled to `False` while `/choose_ai` and `/clear_memory` still execute.
  - Run the complete test suite on Python 3.14.10 and report the `python --version` output and final summary.
