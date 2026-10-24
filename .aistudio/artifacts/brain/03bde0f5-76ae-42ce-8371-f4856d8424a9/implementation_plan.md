# Recluse All-in-One Dashboard & Governance Console

Comprehensive architecture, security overhaul, and modular dashboard implementation plan for the Recluse Discord bot on Render.com free tier.

## Critical Constraints & Directives

> [!IMPORTANT]
> - **Target Platform**: Render.com Free Tier (512MB RAM, 0.1 vCPU, ephemeral disk). No port/env hacks for AI Studio. Zero local disk writes at runtime.
> - **Technology Stack**: `aiohttp` + `aiohttp-jinja2` (autoescape=True), Alpine.js, Tailwind CDN, Font Awesome, Motor (async MongoDB). No Quart, Flask, FastAPI, Node, or npm build step.
> - **Zero Code Touch Before Approval**: Plan mode only. No repository files modified until explicit "approved" confirmation.
> - **Files Modified During Import**: **None**. All original source files remain in their original pristine state. (Note: `quart` and `quart-cors` were already present in the imported repository's `requirements.txt` and will be replaced with `aiohttp-jinja2`).

---

## 1. Executive Summary & Codebase Audit

### 1.1 Repo Findings & Cog Loading
1. **Cog Loading Order**:
   - `bot.py` uses a hardcoded priority list: `['database', 'admin', 'core', 'moderation', 'misc', 'ai', 'anime', 'sports', 'leveling', 'tickets', 'giveaways', 'owner']`.
   - Remaining cogs (`dashboard`, `antinuke`, `autoresponse`, `birthdays`, `fun`, `info`, `reminders`, `utility`) are dynamically discovered and loaded in a secondary loop (`os.listdir('./cogs')`).
   - Notice: The filename is `cogs/autoresponse.py` (not `autoresponder.py`).
2. **Command & Module Disabling Enforcement**:
   - `_dashboard_module_check` in `bot.py` checks `disabled_cogs` and `disabled_cmds`.
   - **Critical finding**: `cogs/dashboard.py` saves `command_settings` (mapping command names to `allowed_channels`, `ignored_channels`, `allowed_roles`, `ignored_roles`, and `disabled_subcommands`). However, `bot.py` **never** checks `command_settings`, channel restrictions, role restrictions, or subcommand restrictions!
   - Every message and interaction in `bot.py` hits MongoDB directly with `find_one({'guild_id': guild_id})` with zero caching, causing unnecessary database latency.
3. **Anti-Spam & Lockdown State**:
   - `_spam_tracker` is an un-evicted in-memory dictionary `{(guild_id, user_id): deque}`.
   - `bot.global_lockdown` is an in-memory boolean on the bot instance.
   - Per-guild lockdown in `cogs/dashboard.py` currently simply alters `@everyone` send_messages, overwriting permissions destructively without snapshotting previous channel-specific overwrites.

### 1.2 Phase-0 Security Vulnerabilities Confirmed & Extended
1. **Broken Object-Level Authorization (BOLA / IDOR)**:
   - Routes `/automod/{guild_id}`, `/lockdown/{guild_id}`, `/logs/{guild_id}`, `/wizard/{guild_id}`, `/misc/{guild_id}`, `POST /api/settings/{guild_id}`, and `POST /api/modules/{guild_id}` only verify that a user session exists. They do **not** verify whether the logged-in user belongs to or has admin/manage permissions in the target `{guild_id}`. Any authenticated user can modify settings or trigger a lockdown on any server the bot resides in.
2. **Persistent Cross-Site Scripting (XSS)**:
   - Server names, command descriptions, banned word lists, user tags, and telemetry payloads are interpolated directly into raw HTML string templates via Python f-strings without HTML escaping.
3. **Lack of CSRF & Weak Session Cookies**:
   - Zero CSRF token checks exist on `POST /api/settings`, `POST /api/modules`, `POST /api/ip_action`, or `POST /api/owner_action`.
   - Session cookies lack `SameSite=Lax` and `Secure` attributes.
4. **OAuth Implementation Vulnerabilities**:
   - `/login` does not pass or validate a cryptographic `state` parameter (vulnerable to OAuth CSRF/session fixation).
   - Discord access tokens are stored plaintext in the `sessions` collection.
   - User sessions are keyed by `discord_id`, meaning a new login overwrites existing sessions.
   - `/users/@me/guilds` is called via HTTP to Discord on every visit to `/`, exhausting rate limits.
5. **Client-Controlled IP Spoofing**:
   - Middleware uses `raw_ip.split(',')[0].strip()`. Because clients can forge arbitrary left-most `X-Forwarded-For` headers, anyone can bypass IP bans or ban arbitrary victims.
6. **Bypasable Security Gateway**:
   - The DDoS "Security Verification" check only verifies the presence of cookie `recluse_verified=true`, which can be forged without real rate limiting.
7. **Inefficient Owner Permission Verification**:
   - `self.bot.application_info()` is called repeatedly inside requests to verify owner clearance.

---

## 2. Technical Architecture & System Layout

```
                        ┌──────────────────────────────────────────────────┐
                        │      Render Reverse Proxy (CF / Edge)            │
                        └─────────────────────────┬────────────────────────┘
                                                  │ X-Forwarded-For: client, proxy
                                                  ▼
                        ┌──────────────────────────────────────────────────┐
                        │              aiohttp Web Application             │
                        │       (server.py / middleware.py on PORT)        │
                        ├──────────────────────────────────────────────────┤
                        │ 1. IP Resolver (TRUSTED_PROXY_HOPS from right)   │
                        │ 2. Sliding In-Memory Token Bucket Rate Limiter   │
                        │ 3. Session Auth (Hashed token + TTL Cache)       │
                        │ 4. CSRF Validator & Security Headers (CSP, etc.) │
                        │ 5. Central Permission Gatekeeper:                │
                        │    has_permission(guild, user_id, node)         │
                        └─────────────────────────┬────────────────────────┘
                                                  │
                  ┌───────────────────────────────┴───────────────────────────────┐
                  ▼                                                               ▼
    ┌───────────────────────────┐                                   ┌───────────────────────────┐
    │     HTML Page Routes      │                                   │       REST API Routes     │
    │  (aiohttp-jinja2 engine)  │                                   │ (JSON payload + CSRF req) │
    ├───────────────────────────┤                                   ├───────────────────────────┤
    │ • / (Keepalive & Picker)  │                                   │ • /health (?strict=1)     │
    │ • /dashboard/{guild_id}   │                                   │ • /api/guilds/{id}/live/* │
    │ • /modules/{guild_id}     │                                   │ • /api/guilds/{id}/mod/*  │
    │ • /permissions/{guild_id} │                                   │ • /api/guilds/{id}/perms  │
    │ • /owner (Admin System)   │                                   │ • /api/owner/*            │
    └─────────────┬─────────────┘                                   └─────────────┬─────────────┘
                  │                                                               │
                  └───────────────────────────────┬───────────────────────────────┘
                                                  │
                                                  ▼
                        ┌──────────────────────────────────────────────────┐
                        │            Bot Core Engine (In-Process)          │
                        ├──────────────────────────────────────────────────┤
                        │ • bot.guild_config (Bounded In-Memory Cache)     │
                        │ • Module Registry (cogs expose DASHBOARD_MODULE) │
                        │ • global_interaction_check & prefix check        │
                        │ • Audit Logger (Before/After Diffs & DTOs)       │
                        └─────────────────────────┬────────────────────────┘
                                                  │
                                                  ▼
                        ┌──────────────────────────────────────────────────┐
                        │             MongoDB Atlas (Motor Async)          │
                        │ • guild_settings (Config + Command Overrides)    │
                        │ • dashboard_permissions (Role/User Grants)       │
                        │ • audit_logs (TTL Index 90d)                     │
                        │ • sessions (TTL Index 7d)                        │
                        │ • ip_bans & rate_limits                          │
                        └─────────────────────────┬────────────────────────┘
```

---

## 3. Module Mapping & Schema Strategy

### 3.1 Existing Cog to Dashboard Module Mapping
| Cog Name | Dashboard Module Key | Category | Schema Fields | Primary Mongo Collection | Required Permission Nodes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `moderation.py` | `moderation` | Moderation | `mod_enabled`, `log_channel`, `dm_on_punish`, `mute_role_id` | `guild_settings`, `mod_logs`, `warnings` | `moderation.view`, `moderation.action`, `moderation.settings` |
| `antinuke.py` | `antinuke` | Security | `enabled`, `threshold`, `window`, `action`, `log_channel`, `whitelist` | `antinuke_config`, `antinuke_alerts` | `antinuke.view`, `antinuke.edit`, `antinuke.whitelist` |
| `autoresponse.py` | `autoresponder` | Engagement | `autoresponse_enabled`, list of triggers (`keyword`, `match_mode`, `response`, `cooldown`, `embed`) | `guild_settings`, `auto_responses` | `autoresponder.view`, `autoresponder.manage` |
| `leveling.py` | `leveling` | Engagement | `leveling_enabled`, `xp_per_message`, `xp_cooldown`, `multiplier`, `levelup_channel`, `levelup_message`, `roles` | `guild_settings`, `level_roles`, `user_levels` | `leveling.view`, `leveling.edit` |
| `giveaways.py` | `giveaways` | Engagement | Active giveaways list, default duration, ping role | `giveaways` | `giveaways.view`, `giveaways.manage` |
| `tickets.py` | `tickets` | Support | `tickets_enabled`, `ticket_category`, `ticket_support_role`, `ticket_log_channel`, `panel_message` | `guild_settings`, `tickets` | `tickets.view`, `tickets.manage` |
| `birthdays.py` | `birthdays` | Engagement | `birthday_channel`, `birthday_role`, `celebration_message`, `timezone` | `guild_settings`, `birthdays` | `birthdays.view`, `birthdays.edit` |
| `reminders.py` | `reminders` | Utilities | `max_reminders_per_user`, `dm_fallback` | `reminders` | `reminders.view`, `reminders.manage` |
| `ai.py` | `ai` | AI Core | `ai_enabled`, `default_ai_model`, `ai_allowed_channels`, `per_channel_models`, `system_prompt_custom` | `guild_settings` | `ai.view`, `ai.edit` |
| `admin.py` | `welcome`, `starboard`, `reaction_roles` | Server Management | `welcome_enabled`, `welcome_channel`, `welcome_message`, `leave_message`, `welcome_dm`, `auto_role`, `starboard_channel`, `starboard_threshold` | `guild_settings`, `reaction_roles`, `custom_commands` | `settings.edit`, `welcome.edit`, `reactionroles.manage` |
| `misc.py`, `utility.py`, `fun.py`, `sports.py`, `anime.py` | Module toggles & command filters | General | `enabled`, per-command allowed/ignored roles & channels | `guild_settings` | `modules.toggle`, `commands.configure` |

### 3.2 Gaps vs. MEE6 / Dyno / Carl-bot
1. **Interactive Embed Builder & Sender**: Visual live preview of Discord embeds with direct dispatch or scheduling to specified channels.
2. **Reaction / Button Roles Visual Manager**: Managing interactive button and dropdown role selectors through the web UI with channel picker and live role preview.
3. **Web Moderation Case File & Infraction History**: Searchable, paginated case history (warnings, mutes, kicks, bans) with ability to issue, pardon, or edit reasons directly from browser.
4. **Granular Logging Event Routing**: Splitting mod logs into dedicated destinations (e.g. member joins, message deletes, voice activity, role updates).

---

## 4. Permission-Node Catalog & Tiers

### 4.1 Permission Tiers
- **Tier 0 (Unconditional Full Access)**: Guild Owner and members with Discord `Administrator` permission. Cannot be restricted; immune to explicit denies.
- **Tier 1 (Guild Managers)**: Discord `Manage Server` (`manage_guild`). Assigned the standard Manager preset (all modules view/edit; cannot manage permissions).
- **Tier 2 (Custom Role & User Grants)**: Configured in the permissions matrix via specific nodes.
  - Evaluation precedence: `Explicit Deny` > `Explicit Allow` > `Default Deny`.

### 4.2 Full Node Catalog
```
dashboard.view                -- Access server dashboard
dashboard.overview.view       -- View analytics, uptime, and activity stream
modules.view                  -- View module configuration
modules.toggle                -- Enable or disable modules
commands.configure            -- Set channel/role restrictions on commands
automod.view                  -- View automod rules & filters
automod.edit                  -- Create, update, or delete automod rules
lockdown.view                 -- View server lockdown status
lockdown.toggle               -- Engage or release emergency lockdown
moderation.view               -- View moderation cases & logs
moderation.action             -- Issue warns, timeouts, kicks, bans via web
moderation.settings           -- Configure mod roles and channels
antinuke.view                 -- View antinuke status & incident logs
antinuke.edit                 -- Tune antinuke thresholds and defense modes
antinuke.whitelist            -- Manage antinuke trusted accounts
logging.view                  -- View system audit logs
logging.edit                  -- Configure log channels & event routing
tickets.view                  -- View support tickets and transcripts
tickets.manage                -- Close, reopen, or delete tickets
giveaways.view                -- View active and past giveaways
giveaways.manage              -- Create, reroll, or force-end giveaways
leveling.view                 -- View leaderboard and leveling rules
leveling.edit                 -- Configure XP rates, multipliers, and role rewards
welcome.edit                  -- Configure welcome/leave messages and autorole
reactionroles.manage          -- Configure reaction & button role assignments
ai.view                       -- View AI configurations
ai.edit                       -- Change model selection, channels, and system prompt
embeds.send                   -- Use the web embed builder and dispatcher
permissions.view              -- View the dashboard role/user permission matrix
permissions.manage            -- Edit role/user dashboard permission nodes
settings.export_import        -- Export, import, or clone server configurations
```

---

## 5. Implementation Phases

- **Phase A**: Security fixes, webdash package structure, `aiohttp-jinja2` template engine, rate limiter, session management, central permission engine, config cache, base layout, overview dashboard, permission matrix manager, and command restriction check in `bot.py`.
- **Phase B**: Web moderation suite, case manager, Automod v2 (spam, links, caps, mentions, banned words/regex), channel-preserving Lockdown v2, and granular event logging.
- **Phase C**: Schema-driven wrappers for existing cogs (`leveling`, `giveaways`, `tickets`, `autoresponse`, `birthdays`, `reminders`, `ai`), followed by the gap features (Embed Builder & Sender, Visual Reaction Roles).
- **Phase D**: Developer Owner Panel upgrade, export/import JSON, Ctrl+K command palette, free-tier optimization audit, and comprehensive test suite.
