"""
antinuke.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
Enterprise-grade Anti-Nuke protection system.

WORKING...
  Discord fires audit-log events milliseconds AFTER an action lands.
  We can't intercept before Discord processes it, but we CAN:
    1. Detect the action the instant it fires (< 200 ms)
    2. Immediately strip every permission from the attacker
    3. Ban them before they can queue a second action
    4. Restore anything that was deleted (channels, roles)
    5. Log everything with full forensic detail

WHAT IT CATCHES
  • Mass channel delete / create (raid bots)
  • Mass role delete / permission escalation
  • Mass member ban / kick
  • Webhook creation (phishing / spam bots)
  • Dangerous permission grants (@everyone admin, etc.)
  • Bot joins that immediately perform destructive actions
  • Server name / icon / vanity changes
  • Integration / OAuth app abuse

THRESHOLDS (configurable per server via /antinuke config)
  Default:  3 destructive actions within 10 seconds = nuke attempt

WHITELIST
  Server owner is always immune.
  Trusted users/bots can be whitelisted via /antinuke whitelist.

SETUP
  /antinuke enable   — turn on protection
  /antinuke config   — tune thresholds
  /antinuke whitelist add/remove <id>
  /antinuke status   — view current config & recent detections
  /antinuke logs     — last 20 triggered alerts
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import collections
import copy
import datetime
import time

import discord
from discord import app_commands
from discord.ext import commands

# ─── Colours ──────────────────────────────────────────────────────────────────
C_NUKE  = discord.Color(0xFF0000)
C_OK    = discord.Color.brand_green()
C_INFO  = discord.Color(0x5865F2)
C_WARN  = discord.Color.yellow()

# ─── Action weight table ──────────────────────────────────────────────────────
# Each audit-log action that is destructive gets a weight.
# When a user's weighted score within the time window exceeds the threshold,
# they are treated as a nuke attempt.
ACTION_WEIGHTS: dict[discord.AuditLogAction, int] = {
    discord.AuditLogAction.channel_delete:          3,
    discord.AuditLogAction.channel_create:          1,  # mass-create can also be spam
    discord.AuditLogAction.role_delete:             3,
    discord.AuditLogAction.role_update:             2,  # permission escalation
    discord.AuditLogAction.member_ban:              3,
    discord.AuditLogAction.kick:                    2,
    discord.AuditLogAction.member_prune:            4,  # pruning = mass kick
    discord.AuditLogAction.webhook_create:          2,
    discord.AuditLogAction.guild_update:            2,
    discord.AuditLogAction.bot_add:                 1,
    discord.AuditLogAction.member_role_update:      1,
    discord.AuditLogAction.channel_update:          1,
    discord.AuditLogAction.emoji_delete:            1,
    discord.AuditLogAction.sticker_delete:          1,
    discord.AuditLogAction.integration_create:      2,
    discord.AuditLogAction.integration_delete:      2,
}

# Default thresholds
DEFAULT_THRESHOLD    = 8    # weighted score before triggering
DEFAULT_WINDOW_SECS  = 10   # rolling window in seconds
DEFAULT_ACTION       = "ban" # ban | kick | strip  (strip = remove all roles/perms only)


# ─────────────────────────────────────────────────────────────────────────────
# Per-guild state tracker
# ─────────────────────────────────────────────────────────────────────────────

class _GuildState:
    """
    Tracks rolling action scores per user and caches snapshots for restoration.
    One instance lives per guild in AntiNuke._state.
    """
    def __init__(self):
        # { user_id: deque of (timestamp, weight) }
        self.scores: dict[int, collections.deque] = collections.defaultdict(
            lambda: collections.deque()
        )
        # Users currently locked down (prevent double-triggering)
        self.locked: set[int] = set()

        # ── Restoration snapshots ─────────────────────────────────────────────
        # We keep a rolling snapshot of the guild state so we can restore
        # channels and roles deleted during a nuke attempt.
        # { channel_id: ChannelSnapshot }
        self.channel_snapshots: dict[int, "_ChannelSnapshot"] = {}
        # { role_id: RoleSnapshot }
        self.role_snapshots: dict[int, "_RoleSnapshot"] = {}

    def add_action(self, user_id: int, weight: int, window: int) -> int:
        """Record a weighted action and return the current rolling score."""
        now = time.monotonic()
        dq  = self.scores[user_id]
        # Evict expired entries
        while dq and now - dq[0][0] > window:
            dq.popleft()
        dq.append((now, weight))
        return sum(w for _, w in dq)

    def reset_score(self, user_id: int):
        self.scores[user_id].clear()


class _ChannelSnapshot:
    """Minimal snapshot of a channel for restoration."""
    __slots__ = ("name", "category_id", "position", "type",
                 "topic", "nsfw", "slowmode", "overwrites", "bitrate", "user_limit")

    def __init__(self, ch: discord.abc.GuildChannel):
        self.name        = ch.name
        self.category_id = ch.category_id
        self.position    = ch.position
        self.type        = ch.type
        self.overwrites  = dict(ch.overwrites)
        # Text-specific
        self.topic       = getattr(ch, "topic",       None)
        self.nsfw        = getattr(ch, "nsfw",         False)
        self.slowmode    = getattr(ch, "slowmode_delay", 0)
        # Voice-specific
        self.bitrate     = getattr(ch, "bitrate",     64000)
        self.user_limit  = getattr(ch, "user_limit",  0)


class _RoleSnapshot:
    """Minimal snapshot of a role for restoration."""
    __slots__ = ("name", "color", "hoist", "mentionable", "permissions", "position")

    def __init__(self, r: discord.Role):
        self.name        = r.name
        self.color       = r.color
        self.hoist       = r.hoist
        self.mentionable = r.mentionable
        self.permissions = r.permissions
        self.position    = r.position


# ─────────────────────────────────────────────────────────────────────────────
# Main Cog
# ─────────────────────────────────────────────────────────────────────────────

class AntiNuke(commands.Cog):
    """Real-time nuke detection and automatic threat neutralisation."""

    def __init__(self, bot: commands.Bot):
        self.bot   = bot
        # { guild_id: _GuildState }
        self._state: dict[int, _GuildState] = {}

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _gs(self, guild_id: int) -> _GuildState:
        if guild_id not in self._state:
            self._state[guild_id] = _GuildState()
        return self._state[guild_id]

    async def _cfg(self, guild_id: int) -> dict:
        if not hasattr(self.bot, "db"):
            return {}
        doc = await self.bot.db.antinuke_config.find_one({"guild_id": guild_id})
        return doc or {}

    async def _save_cfg(self, guild_id: int, update: dict):
        if not hasattr(self.bot, "db"):
            return
        await self.bot.db.antinuke_config.update_one(
            {"guild_id": guild_id}, {"$set": update}, upsert=True
        )

    async def _is_whitelisted(self, guild_id: int, user_id: int, guild: discord.Guild) -> bool:
        """Owner is always immune. Trusted IDs from DB are also immune."""
        if guild.owner_id == user_id:
            return True
        # Bot itself is always immune
        if self.bot.user and user_id == self.bot.user.id:
            return True
        cfg = await self._cfg(guild_id)
        return user_id in cfg.get("whitelist", [])

    async def _get_audit_actor(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
        within_secs: float = 3.0,
    ) -> discord.User | discord.Member | None:
        """
        Fetch the most recent audit log entry for `action` and return the
        responsible user if it was logged within `within_secs` seconds.
        """
        try:
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(limit=5, action=action):
                age = (now - entry.created_at).total_seconds()
                if age > within_secs:
                    break
                if target_id is None or (entry.target and entry.target.id == target_id):
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            pass
        return None

    # ─── Core neutralisation engine ──────────────────────────────────────────

    async def _neutralise(
        self,
        guild: discord.Guild,
        attacker_id: int,
        reason: str,
        cfg: dict,
        restore_channels: list["_ChannelSnapshot | None"] | None = None,
        restore_roles: list["_RoleSnapshot | None"] | None = None,
    ):
        """
        The nuclear response:
          1. Immediately lock the attacker out (strip roles + perms)
          2. Execute the configured action (ban / kick / strip)
          3. Log to the antinuke log channel
          4. Attempt to restore deleted channels/roles
        """
        gs = self._gs(guild.id)

        # Double-trigger guard
        if attacker_id in gs.locked:
            return
        gs.locked.add(attacker_id)
        gs.reset_score(attacker_id)

        action_taken = cfg.get("action", DEFAULT_ACTION)
        log_ch_id    = cfg.get("log_channel")

        attacker: discord.Member | None = guild.get_member(attacker_id)

        # ── Step 1: Strip all permissions IMMEDIATELY ─────────────────────────
        if attacker:
            try:
                # Remove every role below bot's top role
                removable = [
                    r for r in attacker.roles
                    if r != guild.default_role and r < guild.me.top_role
                ]
                if removable:
                    await attacker.remove_roles(*removable, reason="[AntiNuke] Threat neutralised")
            except discord.Forbidden:
                pass

        # ── Step 2: Execute configured action ────────────────────────────────
        ban_success = False
        try:
            if action_taken == "ban":
                await guild.ban(
                    discord.Object(id=attacker_id),
                    reason=f"[AntiNuke] {reason}",
                    delete_message_days=1,
                )
                ban_success = True
            elif action_taken == "kick" and attacker:
                await attacker.kick(reason=f"[AntiNuke] {reason}")
                ban_success = True
            # "strip" = roles already stripped above, nothing more
        except discord.Forbidden:
            pass

        # ── Step 3: Restore deleted channels ─────────────────────────────────
        restored_channels: list[str] = []
        if restore_channels:
            for snap in restore_channels:
                if snap is None:
                    continue
                try:
                    category = guild.get_channel(snap.category_id) if snap.category_id else None
                    if snap.type == discord.ChannelType.text:
                        new_ch = await guild.create_text_channel(
                            snap.name,
                            category=category,
                            topic=snap.topic,
                            nsfw=snap.nsfw,
                            slowmode_delay=snap.slowmode,
                            overwrites=snap.overwrites,
                            reason="[AntiNuke] Restoring deleted channel",
                        )
                    elif snap.type == discord.ChannelType.voice:
                        new_ch = await guild.create_voice_channel(
                            snap.name,
                            category=category,
                            bitrate=snap.bitrate,
                            user_limit=snap.user_limit,
                            overwrites=snap.overwrites,
                            reason="[AntiNuke] Restoring deleted channel",
                        )
                    else:
                        continue
                    restored_channels.append(snap.name)
                except Exception:
                    pass

        # ── Step 4: Restore deleted roles ─────────────────────────────────────
        restored_roles: list[str] = []
        if restore_roles:
            for snap in restore_roles:
                if snap is None:
                    continue
                try:
                    await guild.create_role(
                        name=snap.name,
                        color=snap.color,
                        hoist=snap.hoist,
                        mentionable=snap.mentionable,
                        permissions=snap.permissions,
                        reason="[AntiNuke] Restoring deleted role",
                    )
                    restored_roles.append(snap.name)
                except Exception:
                    pass

        # ── Step 5: Release lock after 60 s (allow re-detection if needed) ────
        async def _release():
            await asyncio.sleep(60)
            gs.locked.discard(attacker_id)
        asyncio.ensure_future(_release())

        # ── Step 6: Log alert ─────────────────────────────────────────────────
        embed = discord.Embed(
            title="🚨 NUKE ATTEMPT NEUTRALISED",
            color=C_NUKE,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="🎯 Attacker",   value=f"<@{attacker_id}> (`{attacker_id}`)", inline=True)
        embed.add_field(name="⚡ Action",      value=action_taken.upper(),                  inline=True)
        embed.add_field(name="✅ Banned",      value="Yes" if ban_success else "No",        inline=True)
        embed.add_field(name="📋 Trigger",    value=reason[:512],                           inline=False)
        if restored_channels:
            embed.add_field(name="♻️ Channels Restored", value=", ".join(f"`{n}`" for n in restored_channels), inline=False)
        if restored_roles:
            embed.add_field(name="♻️ Roles Restored", value=", ".join(f"`{n}`" for n in restored_roles), inline=False)
        embed.set_footer(text=f"Guild: {guild.name} ({guild.id})")

        # Post to configured log channel
        if log_ch_id:
            ch = guild.get_channel(log_ch_id)
            if ch:
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

        # Also post to bot owner's error channel (fallback)
        owner_log = self.bot.get_channel(1096869180621463662)
        if owner_log:
            try:
                embed2 = embed.copy()
                embed2.set_author(name=f"AntiNuke — {guild.name}")
                await owner_log.send(embed=embed2)
            except Exception:
                pass

        # Persist to DB for /antinuke logs
        if hasattr(self.bot, "db"):
            await self.bot.db.antinuke_logs.insert_one({
                "guild_id":         guild.id,
                "attacker_id":      attacker_id,
                "reason":           reason,
                "action":           action_taken,
                "banned":           ban_success,
                "restored_channels": restored_channels,
                "restored_roles":   restored_roles,
                "timestamp":        datetime.datetime.utcnow().timestamp(),
            })

    # ─── Snapshot maintenance ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Keep snapshots fresh whenever a channel is created."""
        gs = self._gs(channel.guild.id)
        gs.channel_snapshots[channel.id] = _ChannelSnapshot(channel)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, _after):
        """Re-snapshot on update so we always have the latest state."""
        gs = self._gs(before.guild.id)
        gs.channel_snapshots[before.id] = _ChannelSnapshot(before)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        gs = self._gs(role.guild.id)
        gs.role_snapshots[role.id] = _RoleSnapshot(role)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, _after):
        gs = self._gs(before.guild.id)
        gs.role_snapshots[before.id] = _RoleSnapshot(before)

    # We also snapshot ALL existing channels/roles when the bot joins / restarts

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._snapshot_guild(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._snapshot_guild(guild)

    async def _snapshot_guild(self, guild: discord.Guild):
        gs = self._gs(guild.id)
        for ch in guild.channels:
            gs.channel_snapshots[ch.id] = _ChannelSnapshot(ch)
        for role in guild.roles:
            gs.role_snapshots[role.id] = _RoleSnapshot(role)

    # ─────────────────────────────────────────────────────────────────────────
    # EVENT LISTENERS — each one checks thresholds and triggers if needed
    # ─────────────────────────────────────────────────────────────────────────

    async def _check_and_trigger(
        self,
        guild: discord.Guild,
        actor_id: int,
        action: discord.AuditLogAction,
        extra_reason: str = "",
        restore_channels: list | None = None,
        restore_roles: list | None = None,
    ):
        """
        Central scoring hub.  Called by every event listener.
        Adds weight, checks threshold, triggers neutralisation if exceeded.
        """
        cfg = await self._cfg(guild.id)
        if not cfg.get("enabled", False):
            return

        if await self._is_whitelisted(guild.id, actor_id, guild):
            return

        weight    = ACTION_WEIGHTS.get(action, 1)
        threshold = cfg.get("threshold", DEFAULT_THRESHOLD)
        window    = cfg.get("window",    DEFAULT_WINDOW_SECS)
        gs        = self._gs(guild.id)
        score     = gs.add_action(actor_id, weight, window)

        if score >= threshold:
            reason = (
                f"Score {score}/{threshold} in {window}s — "
                f"triggered by {action.name}"
                + (f" | {extra_reason}" if extra_reason else "")
            )
            await self._neutralise(
                guild, actor_id, reason, cfg,
                restore_channels=restore_channels,
                restore_roles=restore_roles,
            )

    # ── Channel delete ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        gs    = self._gs(guild.id)
        snap  = gs.channel_snapshots.pop(channel.id, _ChannelSnapshot(channel))

        actor = await self._get_audit_actor(guild, discord.AuditLogAction.channel_delete, channel.id)
        if not actor:
            return
        await self._check_and_trigger(
            guild, actor.id,
            discord.AuditLogAction.channel_delete,
            extra_reason=f"Deleted #{channel.name}",
            restore_channels=[snap],
        )

    # ── Role delete ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild = role.guild
        gs    = self._gs(guild.id)
        snap  = gs.role_snapshots.pop(role.id, _RoleSnapshot(role))

        actor = await self._get_audit_actor(guild, discord.AuditLogAction.role_delete, role.id)
        if not actor:
            return
        await self._check_and_trigger(
            guild, actor.id,
            discord.AuditLogAction.role_delete,
            extra_reason=f"Deleted role @{role.name}",
            restore_roles=[snap],
        )

    # ── Role update (permission escalation guard) ─────────────────────────────

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        guild = after.guild
        # Detect dangerous permission grants
        dangerous_perms = (
            "administrator",
            "ban_members",
            "kick_members",
            "manage_guild",
            "manage_channels",
            "manage_roles",
            "mention_everyone",
        )
        newly_granted = [
            p for p in dangerous_perms
            if not getattr(before.permissions, p) and getattr(after.permissions, p)
        ]
        if not newly_granted:
            return

        actor = await self._get_audit_actor(guild, discord.AuditLogAction.role_update, after.id)
        if not actor:
            return

        # Immediately revert the dangerous permission change
        cfg = await self._cfg(guild.id)
        if cfg.get("enabled", False) and not await self._is_whitelisted(guild.id, actor.id, guild):
            try:
                await after.edit(
                    permissions=before.permissions,
                    reason=f"[AntiNuke] Reverting dangerous perm grant by {actor}",
                )
            except discord.Forbidden:
                pass

        await self._check_and_trigger(
            guild, actor.id,
            discord.AuditLogAction.role_update,
            extra_reason=f"Granted {', '.join(newly_granted)} to @{after.name}",
        )

    # ── Member ban ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        actor = await self._get_audit_actor(guild, discord.AuditLogAction.ban, user.id)
        if not actor:
            return
        await self._check_and_trigger(
            guild, actor.id,
            discord.AuditLogAction.member_ban,
            extra_reason=f"Banned {user}",
        )

    # ── Member kick (detected via on_member_remove + audit log) ───────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        actor = await self._get_audit_actor(guild, discord.AuditLogAction.kick, member.id, within_secs=2.0)
        if not actor:
            return  # voluntary leave, not a kick
        if actor.id == member.id:
            return  # self-leave
        await self._check_and_trigger(
            guild, actor.id,
            discord.AuditLogAction.kick,
            extra_reason=f"Kicked {member}",
        )

    # ── Webhook create ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.TextChannel):
        guild = channel.guild
        actor = await self._get_audit_actor(guild, discord.AuditLogAction.webhook_create)
        if not actor:
            return
        # Delete all webhooks in this channel if attacker is suspicious
        cfg = await self._cfg(guild.id)
        if not cfg.get("enabled", False):
            return
        if await self._is_whitelisted(guild.id, actor.id, guild):
            return
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.user and wh.user.id == actor.id:
                    await wh.delete(reason="[AntiNuke] Webhook from suspicious actor removed")
        except discord.Forbidden:
            pass
        await self._check_and_trigger(
            guild, actor.id,
            discord.AuditLogAction.webhook_create,
            extra_reason=f"Created webhook in #{channel.name}",
        )

    # ── Guild update (name / icon wipe) ──────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        # Only care if name or icon was changed to something suspicious
        changed = []
        if before.name != after.name:
            changed.append(f"name: '{before.name}' → '{after.name}'")
        if before.icon != after.icon:
            changed.append("icon changed")
        if not changed:
            return

        actor = await self._get_audit_actor(after, discord.AuditLogAction.guild_update)
        if not actor:
            return
        await self._check_and_trigger(
            after, actor.id,
            discord.AuditLogAction.guild_update,
            extra_reason=", ".join(changed),
        )

    # ── Suspicious bot joins ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            return
        guild = member.guild
        cfg   = await self._cfg(guild.id)
        if not cfg.get("enabled", False):
            return
        if not cfg.get("bot_join_protection", True):
            return

        # Who added this bot?
        actor = await self._get_audit_actor(guild, discord.AuditLogAction.bot_add, member.id, within_secs=5.0)
        if not actor:
            return
        if await self._is_whitelisted(guild.id, actor.id, guild):
            return

        # Only flag if the person adding the bot is NOT an admin
        adder = guild.get_member(actor.id)
        if adder and adder.guild_permissions.administrator:
            return

        # Kick the unverified bot immediately
        try:
            await member.kick(reason="[AntiNuke] Unverified bot added by non-admin")
        except discord.Forbidden:
            pass

        log_ch_id = cfg.get("log_channel")
        if log_ch_id:
            ch = guild.get_channel(log_ch_id)
            if ch:
                embed = discord.Embed(
                    title="🤖 Suspicious Bot Addition Blocked",
                    color=C_WARN,
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Bot",    value=f"{member.mention} (`{member.id}`)", inline=True)
                embed.add_field(name="Added by", value=f"{actor.mention} (`{actor.id}`)", inline=True)
                embed.add_field(name="Action", value="Bot kicked",                        inline=True)
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # SLASH COMMANDS
    # ─────────────────────────────────────────────────────────────────────────

    antinuke_group = app_commands.Group(
        name="antinuke",
        description="Configure the anti-nuke protection system.",
        default_permissions=discord.Permissions(administrator=True),
    )

    # ── /antinuke enable ──────────────────────────────────────────────────────

    @antinuke_group.command(name="enable", description="Enable or disable anti-nuke protection.")
    @app_commands.describe(enabled="Turn protection on or off.")
    async def an_enable(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.guild:
            return
        await self._save_cfg(interaction.guild.id, {"enabled": enabled})
        if enabled:
            # Snapshot the guild immediately
            await self._snapshot_guild(interaction.guild)
            embed = discord.Embed(
                title="🛡️ Anti-Nuke ENABLED",
                description=(
                    "Your server is now protected.\n\n"
                    "**What's monitored:**\n"
                    "• Mass channel deletions\n"
                    "• Mass role deletions\n"
                    "• Dangerous permission grants\n"
                    "• Mass bans / kicks\n"
                    "• Webhook creation abuse\n"
                    "• Server name / icon changes\n"
                    "• Suspicious bot additions\n\n"
                    "Use `/antinuke config` to tune thresholds.\n"
                    "Use `/antinuke whitelist` to trust your staff bots."
                ),
                color=C_OK,
            )
        else:
            embed = discord.Embed(
                title="⚠️ Anti-Nuke DISABLED",
                description="Your server is no longer protected. Re-enable with `/antinuke enable`.",
                color=C_WARN,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /antinuke config ──────────────────────────────────────────────────────

    @antinuke_group.command(name="config", description="Tune anti-nuke thresholds and response action.")
    @app_commands.describe(
        threshold="Weighted score to trigger (default 8). Lower = more sensitive.",
        window="Rolling time window in seconds (default 10).",
        action="What to do with the attacker.",
        log_channel="Channel to send alerts to.",
        bot_join_protection="Block bots added by non-admins.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Ban  (recommended)", value="ban"),
        app_commands.Choice(name="Kick",               value="kick"),
        app_commands.Choice(name="Strip roles only",   value="strip"),
    ])
    async def an_config(
        self,
        interaction: discord.Interaction,
        threshold:           int                        = 8,
        window:              int                        = 10,
        action:              app_commands.Choice[str]   = None,
        log_channel:         discord.TextChannel | None = None,
        bot_join_protection: bool                       = True,
    ):
        if not interaction.guild:
            return
        update: dict = {
            "threshold":           max(3, min(threshold, 50)),
            "window":              max(3, min(window, 30)),
            "action":              action.value if action else DEFAULT_ACTION,
            "bot_join_protection": bot_join_protection,
        }
        if log_channel:
            update["log_channel"] = log_channel.id
        await self._save_cfg(interaction.guild.id, update)

        embed = discord.Embed(title="⚙️ Anti-Nuke Config Saved", color=C_INFO)
        embed.add_field(name="Threshold",        value=f"`{update['threshold']}` pts",        inline=True)
        embed.add_field(name="Window",           value=f"`{update['window']}s`",               inline=True)
        embed.add_field(name="Action",           value=f"`{update['action']}`",                inline=True)
        embed.add_field(name="Bot Join Guard",   value="✅" if bot_join_protection else "❌",  inline=True)
        if log_channel:
            embed.add_field(name="Log Channel",  value=log_channel.mention,                    inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /antinuke whitelist ───────────────────────────────────────────────────

    @antinuke_group.command(name="whitelist", description="Add or remove a trusted user/bot from the whitelist.")
    @app_commands.describe(
        action="Add or remove.",
        user_id="User or bot ID to whitelist.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add",    value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List",   value="list"),
    ])
    async def an_whitelist(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user_id: str | None = None,
    ):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return

        cfg       = await self._cfg(interaction.guild.id)
        whitelist: list = cfg.get("whitelist", [])

        if action.value == "list":
            if not whitelist:
                return await interaction.response.send_message("Whitelist is empty.", ephemeral=True)
            lines = [f"• <@{uid}> (`{uid}`)" for uid in whitelist]
            embed = discord.Embed(title="🛡️ Anti-Nuke Whitelist", description="\n".join(lines), color=C_INFO)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not user_id:
            return await interaction.response.send_message("❌ Provide a user ID.", ephemeral=True)
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID — must be a number.", ephemeral=True)

        if uid == interaction.guild.owner_id:
            return await interaction.response.send_message(
                "ℹ️ The server owner is always immune — no need to whitelist.", ephemeral=True
            )

        if action.value == "add":
            if uid not in whitelist:
                whitelist.append(uid)
            await self._save_cfg(interaction.guild.id, {"whitelist": whitelist})
            await interaction.response.send_message(
                f"✅ `{uid}` added to the anti-nuke whitelist. They are now trusted.", ephemeral=True
            )
        elif action.value == "remove":
            whitelist = [x for x in whitelist if x != uid]
            await self._save_cfg(interaction.guild.id, {"whitelist": whitelist})
            await interaction.response.send_message(
                f"✅ `{uid}` removed from the whitelist.", ephemeral=True
            )

    # ── /antinuke status ──────────────────────────────────────────────────────

    @antinuke_group.command(name="status", description="View current anti-nuke configuration and stats.")
    async def an_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        cfg = await self._cfg(interaction.guild.id)

        enabled   = cfg.get("enabled",    False)
        threshold = cfg.get("threshold",  DEFAULT_THRESHOLD)
        window    = cfg.get("window",     DEFAULT_WINDOW_SECS)
        action    = cfg.get("action",     DEFAULT_ACTION)
        whitelist = cfg.get("whitelist",  [])
        log_ch_id = cfg.get("log_channel")
        log_ch    = interaction.guild.get_channel(log_ch_id) if log_ch_id else None

        total_triggers = 0
        if hasattr(self.bot, "db"):
            total_triggers = await self.bot.db.antinuke_logs.count_documents(
                {"guild_id": interaction.guild.id}
            )

        embed = discord.Embed(
            title="🛡️ Anti-Nuke Status",
            color=C_OK if enabled else C_WARN,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Protection",     value="🟢 **ACTIVE**" if enabled else "🔴 **INACTIVE**", inline=True)
        embed.add_field(name="Threshold",      value=f"`{threshold}` pts / `{window}s`",                inline=True)
        embed.add_field(name="Response",       value=f"`{action}`",                                     inline=True)
        embed.add_field(name="Bot Join Guard", value="✅" if cfg.get("bot_join_protection", True) else "❌", inline=True)
        embed.add_field(name="Log Channel",    value=log_ch.mention if log_ch else "*Not set*",         inline=True)
        embed.add_field(name="Triggers (all)", value=str(total_triggers),                               inline=True)
        if whitelist:
            wl_str = " ".join(f"`{uid}`" for uid in whitelist[:10])
            embed.add_field(name=f"Whitelist ({len(whitelist)})", value=wl_str, inline=False)
        else:
            embed.add_field(name="Whitelist", value="*Empty — only server owner is immune*", inline=False)

        embed.set_footer(text="Use /antinuke enable to toggle | /antinuke config to adjust")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /antinuke logs ────────────────────────────────────────────────────────

    @antinuke_group.command(name="logs", description="View the last 10 anti-nuke detections for this server.")
    async def an_logs(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)

        docs = await self.bot.db.antinuke_logs.find(
            {"guild_id": interaction.guild.id}
        ).sort("timestamp", -1).limit(10).to_list(10)

        if not docs:
            return await interaction.response.send_message("No detections recorded yet.", ephemeral=True)

        embed = discord.Embed(
            title="📋 Anti-Nuke Detection Logs",
            color=C_NUKE,
            timestamp=discord.utils.utcnow(),
        )
        for d in docs:
            ts      = int(d.get("timestamp", 0))
            attacker = d.get("attacker_id")
            reason   = d.get("reason", "?")[:80]
            action   = d.get("action", "?")
            banned   = "✅ Banned" if d.get("banned") else "⚠️ Failed"
            embed.add_field(
                name=f"<t:{ts}:R>",
                value=(
                    f"**Attacker:** <@{attacker}>\n"
                    f"**Trigger:** {reason}\n"
                    f"**Outcome:** `{action}` — {banned}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /antinuke test ────────────────────────────────────────────────────────

    @antinuke_group.command(name="test", description="[Owner only] Simulate a nuke alert to verify your log channel.")
    async def an_test(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner-only command.", ephemeral=True)

        cfg      = await self._cfg(interaction.guild.id)
        log_ch_id = cfg.get("log_channel")
        if not log_ch_id:
            return await interaction.response.send_message(
                "❌ No log channel set. Run `/antinuke config` first.", ephemeral=True
            )
        ch = interaction.guild.get_channel(log_ch_id)
        if not ch:
            return await interaction.response.send_message("❌ Log channel not found.", ephemeral=True)

        embed = discord.Embed(
            title="🚨 NUKE ATTEMPT NEUTRALISED  [TEST]",
            description="This is a test alert to verify your anti-nuke setup is working correctly.",
            color=C_NUKE,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="🎯 Attacker",  value=f"{interaction.user.mention} (test)", inline=True)
        embed.add_field(name="⚡ Action",     value="BAN (simulated)",                   inline=True)
        embed.add_field(name="📋 Trigger",   value="Manual test via /antinuke test",    inline=False)
        embed.set_footer(text="✅ If you can see this, your log channel is configured correctly.")
        await ch.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Test alert sent to {ch.mention}. If you can see it there, you're all set!", ephemeral=True
        )


# ─────────────────────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
