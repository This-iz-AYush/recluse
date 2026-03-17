"""
admin.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
Centralised server-administration & configuration hub.

Covers:
  • Server setup wizard
  • Module enable/disable toggles
  • AI model config  (moved here from ai.py)
  • Logging system
  • Welcome / leave messages
  • Auto-role on join
  • Reaction roles
  • Anti-spam / auto-mod settings
  • Leveling system config
  • Ticket system setup
  • Starboard setup
  • Custom commands CRUD

All commands require Manage Guild or higher.
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

# ─── colour palette (consistent across embeds) ────────────────────────────────
C_OK      = discord.Color.brand_green()
C_WARN    = discord.Color.yellow()
C_ERR     = discord.Color.red()
C_INFO    = discord.Color(0x5865F2)   # Discord Blurple
C_NEUTRAL = discord.Color(0x2b2d31)

# ─── AI engine label map (shared with ai.py) ──────────────────────────────────
AI_LABELS = {
    "auto":     "🤖 Auto (Smart Routing)",
    "gemini":   "✨ Gemini 2.5 Flash",
    "nexusify": "⚡ Nexusify GLM-5",
    "sarvam":   "🇮🇳 Sarvam 30B",
}


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _cfg_embed(title: str, fields: dict, color=C_INFO) -> discord.Embed:
    """Build a clean settings embed from a dict."""
    e = discord.Embed(title=title, color=color, timestamp=datetime.datetime.utcnow())
    for k, v in fields.items():
        e.add_field(name=k, value=str(v) if v else "*Not set*", inline=True)
    return e


async def _get_settings(bot, guild_id: int) -> dict:
    if not hasattr(bot, "db"):
        return {}
    doc = await bot.db.guild_settings.find_one({"guild_id": guild_id})
    return doc or {}


async def _save_settings(bot, guild_id: int, update: dict):
    if not hasattr(bot, "db"):
        return
    await bot.db.guild_settings.update_one(
        {"guild_id": guild_id},
        {"$set": update},
        upsert=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Setup Wizard  (interactive, Dyno-style)
# ═════════════════════════════════════════════════════════════════════════════

class SetupView(discord.ui.View):
    """Step-by-step 3-minute server setup wizard."""

    def __init__(self, bot, guild: discord.Guild, author: discord.Member):
        super().__init__(timeout=180)
        self.bot    = bot
        self.guild  = guild
        self.author = author
        self.data   = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("This wizard belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Set Log Channel",  style=discord.ButtonStyle.primary)
    async def set_log(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(
            "📋 Mention or paste the ID of the channel to use for **mod logs**.", ephemeral=True
        )
        def check(m):
            return m.author == self.author and m.channel == interaction.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
            ch  = msg.channel_mentions[0] if msg.channel_mentions else None
            if ch is None:
                try:
                    ch = interaction.guild.get_channel(int(msg.content.strip()))
                except Exception:
                    ch = None

            if ch:
                self.data["log_channel"] = ch.id
                await msg.delete()
                await interaction.followup.send(f"✅ Log channel set to {ch.mention}.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Couldn't resolve that channel.", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)

    @discord.ui.button(label="👋 Set Welcome Channel", style=discord.ButtonStyle.primary)
    async def set_welcome(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(
            "📋 Mention or paste the ID of the **welcome channel**.", ephemeral=True
        )
        def check(m):
            return m.author == self.author and m.channel == interaction.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
            ch  = msg.channel_mentions[0] if msg.channel_mentions else None
            if ch is None:
                try:
                    ch = interaction.guild.get_channel(int(msg.content.strip()))
                except Exception:
                    ch = None
            if ch:
                self.data["welcome_channel"] = ch.id
                await msg.delete()
                await interaction.followup.send(f"✅ Welcome channel set to {ch.mention}.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Couldn't resolve that channel.", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)

    @discord.ui.button(label="💾 Save & Finish", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, _btn):
        if not self.data:
            return await interaction.response.send_message(
                "Nothing configured yet.", ephemeral=True
            )
        await _save_settings(self.bot, self.guild.id, self.data)
        summary = "\n".join(f"• **{k}:** `{v}`" for k, v in self.data.items())
        embed = discord.Embed(
            title="✅ Setup Complete",
            description=f"The following settings have been saved:\n\n{summary}",
            color=C_OK,
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _btn):
        await interaction.response.edit_message(
            content="Setup wizard cancelled.", embed=None, view=None
        )
        self.stop()


# ═════════════════════════════════════════════════════════════════════════════
# Admin Cog
# ═════════════════════════════════════════════════════════════════════════════

class Admin(commands.Cog):
    """Server administration and configuration for Recluse."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # reaction-role in-memory cache  { guild_id: { message_id: { emoji: role_id } } }
        self._rr_cache: dict[int, dict[int, dict[str, int]]] = {}

    # ─── generic permission guard ────────────────────────────────────────────

    async def _admin_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return False
        if interaction.user.guild_permissions.manage_guild or await self.bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message(
            "❌ You need **Manage Server** permission to use this command.", ephemeral=True
        )
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # /setup  — interactive wizard
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="setup", description="Launch the interactive server setup wizard.")
    @app_commands.default_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction):
        if not await self._admin_check(interaction):
            return
        embed = discord.Embed(
            title="🛠️ Recluse Setup Wizard",
            description=(
                "Click the buttons below to configure your server step by step.\n"
                "This wizard will time out in **3 minutes**."
            ),
            color=C_INFO,
        )
        view = SetupView(self.bot, interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /config view  — display all settings
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="config", description="View or reset this server's full configuration.")
    @app_commands.default_permissions(manage_guild=True)
    async def config(self, interaction: discord.Interaction):
        if not await self._admin_check(interaction):
            return
        cfg = await _get_settings(self.bot, interaction.guild.id)

        def _ch(cid):
            c = interaction.guild.get_channel(cid) if cid else None
            return c.mention if c else "*Not set*"

        def _role(rid):
            r = interaction.guild.get_role(rid) if rid else None
            return r.mention if r else "*Not set*"

        embed = discord.Embed(title=f"⚙️ Config — {interaction.guild.name}", color=C_INFO,
                              timestamp=datetime.datetime.utcnow())
        # Channels
        embed.add_field(name="📋 Log Channel",       value=_ch(cfg.get("log_channel")),          inline=True)
        embed.add_field(name="👋 Welcome Channel",   value=_ch(cfg.get("welcome_channel")),       inline=True)
        embed.add_field(name="🎫 Ticket Category",   value=_ch(cfg.get("ticket_category")),       inline=True)
        embed.add_field(name="⭐ Starboard Channel", value=_ch(cfg.get("starboard_channel")),     inline=True)
        # Roles
        embed.add_field(name="🤝 Auto-Role",         value=_role(cfg.get("auto_role")),           inline=True)
        embed.add_field(name="🎫 Ticket Support",    value=_role(cfg.get("ticket_support_role")), inline=True)
        # Modules
        modules = {
            "AI":           cfg.get("ai_enabled", True),
            "Moderation":   cfg.get("mod_enabled", True),
            "Leveling":     cfg.get("leveling_enabled", True),
            "Welcome":      cfg.get("welcome_enabled", True),
            "Sports":       cfg.get("sports_enabled", True),
            "Anime":        cfg.get("anime_enabled", True),
            "Misc":         cfg.get("misc_enabled", True),
            "Automod":      cfg.get("automod_enabled", False),
        }
        mod_str = "\n".join(
            f"{'✅' if v else '❌'} {k}" for k, v in modules.items()
        )
        embed.add_field(name="🧩 Module Status", value=mod_str, inline=False)
        # AI
        embed.add_field(
            name="🤖 Default AI",
            value=AI_LABELS.get(cfg.get("default_ai_model", "auto"), "Auto"),
            inline=True,
        )
        # Anti-spam
        embed.add_field(
            name="🛡️ Anti-Spam",
            value=(
                f"Threshold: `{cfg.get('antispam_threshold', 5)}` msgs / "
                f"`{cfg.get('antispam_window', 5)}s`\n"
                f"Action: `{cfg.get('antispam_action', 'mute')}`"
            ),
            inline=True,
        )
        embed.set_footer(text="Use /admin <section> to modify settings.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /modules  — per-module toggle
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="modules", description="Enable or disable individual bot modules.")
    @app_commands.describe(
        module="Module to toggle.",
        enabled="Turn it on or off.",
    )
    @app_commands.choices(module=[
        app_commands.Choice(name="AI Chat",       value="ai"),
        app_commands.Choice(name="Moderation",    value="mod"),
        app_commands.Choice(name="Leveling",      value="leveling"),
        app_commands.Choice(name="Welcome/Leave", value="welcome"),
        app_commands.Choice(name="Sports",        value="sports"),
        app_commands.Choice(name="Anime/Manga",   value="anime"),
        app_commands.Choice(name="Miscellaneous", value="misc"),
        app_commands.Choice(name="Automod",       value="automod"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def modules(
        self,
        interaction: discord.Interaction,
        module: app_commands.Choice[str],
        enabled: bool,
    ):
        if not await self._admin_check(interaction):
            return
        key_map = {
            "ai":       "ai_enabled",
            "mod":      "mod_enabled",
            "leveling": "leveling_enabled",
            "welcome":  "welcome_enabled",
            "sports":   "sports_enabled",
            "anime":    "anime_enabled",
            "misc":     "misc_enabled",
            "automod":  "automod_enabled",
        }
        await _save_settings(self.bot, interaction.guild.id, {key_map[module.value]: enabled})
        status = "✅ Enabled" if enabled else "❌ Disabled"
        await interaction.response.send_message(
            f"{status} the **{module.name}** module for this server.", ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /setlog  — logging channel
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="setlog", description="Set the channel where moderation logs are sent.")
    @app_commands.describe(channel="The text channel to use for logs.", event="Which events to log.")
    @app_commands.choices(event=[
        app_commands.Choice(name="All Events",        value="all"),
        app_commands.Choice(name="Mod Actions Only",  value="mod"),
        app_commands.Choice(name="Message Events",    value="messages"),
        app_commands.Choice(name="Member Events",     value="members"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def setlog(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        event: app_commands.Choice[str] = None,
    ):
        if not await self._admin_check(interaction):
            return
        update = {"log_channel": channel.id}
        if event:
            update["log_event_filter"] = event.value
        await _save_settings(self.bot, interaction.guild.id, update)
        await interaction.response.send_message(
            f"📋 Mod logs will now be sent to {channel.mention} "
            f"(filter: `{event.value if event else 'all'}`).",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /setwelcome  — welcome / leave messages
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="setwelcome",
        description="Configure the welcome/leave message system.",
    )
    @app_commands.describe(
        channel="Channel to send messages in.",
        welcome_msg="Message for new members. Use {user}, {server}, {count}.",
        leave_msg="Message when members leave. Use {user}, {server}.",
        dm_welcome="Also DM the new member their welcome message.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        welcome_msg: str = "👋 Welcome to **{server}**, {user}! You're member #{count}.",
        leave_msg: str   = "👋 **{user}** has left the server. Goodbye!",
        dm_welcome: bool = False,
    ):
        if not await self._admin_check(interaction):
            return
        await _save_settings(self.bot, interaction.guild.id, {
            "welcome_channel":  channel.id,
            "welcome_message":  welcome_msg,
            "leave_message":    leave_msg,
            "welcome_dm":       dm_welcome,
            "welcome_enabled":  True,
        })
        embed = discord.Embed(title="✅ Welcome System Updated", color=C_OK)
        embed.add_field(name="Channel",    value=channel.mention,  inline=True)
        embed.add_field(name="DM on Join", value="Yes" if dm_welcome else "No", inline=True)
        embed.add_field(name="Welcome",    value=f"`{welcome_msg}`", inline=False)
        embed.add_field(name="Leave",      value=f"`{leave_msg}`",   inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /autorole  — assign role on join
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="autorole", description="Assign a role automatically when a member joins.")
    @app_commands.describe(role="Role to give — pass 'none' to disable.", bots="Also apply to bots.")
    @app_commands.default_permissions(manage_guild=True)
    async def autorole(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        bots: bool = False,
    ):
        if not await self._admin_check(interaction):
            return
        if role is None:
            await _save_settings(self.bot, interaction.guild.id, {"auto_role": None})
            return await interaction.response.send_message("✅ Auto-role disabled.", ephemeral=True)

        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ That role is higher than my highest role. Move my role above it first.", ephemeral=True
            )
        await _save_settings(self.bot, interaction.guild.id, {
            "auto_role":       role.id,
            "auto_role_bots":  bots,
        })
        await interaction.response.send_message(
            f"✅ New members will automatically receive {role.mention}.", ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /reactionrole  — add / remove reaction-role bindings
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="reactionrole",
        description="Bind a reaction emoji on a message to a role.",
    )
    @app_commands.describe(
        action="Add or remove a binding.",
        message_id="ID of the message to react on.",
        emoji="Emoji to use (unicode or custom :name:).",
        role="Role to assign when users react.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add",    value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List",   value="list"),
    ])
    @app_commands.default_permissions(manage_roles=True)
    async def reactionrole(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        message_id: str | None = None,
        emoji: str | None = None,
        role: discord.Role | None = None,
    ):
        if not await self._admin_check(interaction):
            return

        gid = interaction.guild.id

        # LIST
        if action.value == "list":
            rr = await self.bot.db.reaction_roles.find({"guild_id": gid}).to_list(50) if hasattr(self.bot, "db") else []
            if not rr:
                return await interaction.response.send_message("No reaction roles configured.", ephemeral=True)
            lines = [
                f"• Message `{r['message_id']}` — {r['emoji']} → <@&{r['role_id']}>"
                for r in rr
            ]
            embed = discord.Embed(title="🎭 Reaction Roles", description="\n".join(lines), color=C_INFO)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not message_id or not emoji:
            return await interaction.response.send_message(
                "❌ Provide `message_id` and `emoji` for add/remove.", ephemeral=True
            )

        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        # ADD
        if action.value == "add":
            if not role:
                return await interaction.response.send_message("❌ Provide a role.", ephemeral=True)
            if role >= interaction.guild.me.top_role:
                return await interaction.response.send_message(
                    "❌ That role is above my highest role.", ephemeral=True
                )
            if hasattr(self.bot, "db"):
                await self.bot.db.reaction_roles.update_one(
                    {"guild_id": gid, "message_id": mid, "emoji": emoji},
                    {"$set": {"role_id": role.id}},
                    upsert=True,
                )
            # Update cache
            self._rr_cache.setdefault(gid, {}).setdefault(mid, {})[emoji] = role.id
            # Try to add the reaction to the message
            try:
                msg = await interaction.channel.fetch_message(mid)
                await msg.add_reaction(emoji)
            except Exception:
                pass
            await interaction.response.send_message(
                f"✅ Reacting {emoji} on message `{mid}` will grant {role.mention}.", ephemeral=True
            )

        # REMOVE
        elif action.value == "remove":
            if hasattr(self.bot, "db"):
                await self.bot.db.reaction_roles.delete_one(
                    {"guild_id": gid, "message_id": mid, "emoji": emoji}
                )
            self._rr_cache.get(gid, {}).get(mid, {}).pop(emoji, None)
            await interaction.response.send_message(
                f"✅ Reaction-role binding for {emoji} on `{mid}` removed.", ephemeral=True
            )

    # ─── Reaction-role event listeners ────────────────────────────────────────

    async def _resolve_rr(self, payload: discord.RawReactionActionEvent) -> discord.Role | None:
        """Return the role bound to a reaction, or None."""
        if not payload.guild_id or not hasattr(self.bot, "db"):
            return None
        emoji_str = str(payload.emoji)
        gid, mid   = payload.guild_id, payload.message_id
        # Try cache first
        role_id = self._rr_cache.get(gid, {}).get(mid, {}).get(emoji_str)
        if role_id is None:
            doc = await self.bot.db.reaction_roles.find_one(
                {"guild_id": gid, "message_id": mid, "emoji": emoji_str}
            )
            if doc:
                role_id = doc["role_id"]
                self._rr_cache.setdefault(gid, {}).setdefault(mid, {})[emoji_str] = role_id
        if not role_id:
            return None
        guild = self.bot.get_guild(gid)
        return guild.get_role(role_id) if guild else None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        role = await self._resolve_rr(payload)
        if not role:
            return
        guild  = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member and not member.bot:
            try:
                await member.add_roles(role, reason="Reaction role")
            except discord.Forbidden:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        role = await self._resolve_rr(payload)
        if not role:
            return
        guild  = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member and not member.bot:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except discord.Forbidden:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # /antispam  — configure the auto-moderator
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="antispam", description="Configure the anti-spam / auto-mod system.")
    @app_commands.describe(
        threshold="Number of messages before triggering (default 5).",
        window="Time window in seconds (default 5).",
        action="Action to take: warn, mute, kick, or ban.",
        mute_duration="If action is mute, how many minutes to mute (default 10).",
        log_channel="Override the log channel for automod events.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Warn Only",          value="warn"),
        app_commands.Choice(name="Timeout (mute)",     value="mute"),
        app_commands.Choice(name="Kick",               value="kick"),
        app_commands.Choice(name="Ban",                value="ban"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def antispam(
        self,
        interaction: discord.Interaction,
        threshold: int = 5,
        window: int = 5,
        action: app_commands.Choice[str] | None = None,
        mute_duration: int = 10,
        log_channel: discord.TextChannel | None = None,
    ):
        if not await self._admin_check(interaction):
            return
        update = {
            "automod_enabled":     True,
            "antispam_threshold":  max(2, min(threshold, 20)),
            "antispam_window":     max(2, min(window, 30)),
            "antispam_action":     action.value if action else "mute",
            "antispam_mute_mins":  max(1, mute_duration),
        }
        if log_channel:
            update["antispam_log_channel"] = log_channel.id
        await _save_settings(self.bot, interaction.guild.id, update)
        embed = discord.Embed(title="🛡️ Anti-Spam Updated", color=C_OK)
        embed.add_field(name="Threshold",     value=f"`{update['antispam_threshold']}` msgs", inline=True)
        embed.add_field(name="Window",        value=f"`{update['antispam_window']}s`",         inline=True)
        embed.add_field(name="Action",        value=f"`{update['antispam_action']}`",           inline=True)
        if action and action.value == "mute":
            embed.add_field(name="Mute Duration", value=f"`{mute_duration}m`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /levelconfig  — XP / ranking settings
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="levelconfig", description="Configure the XP leveling system.")
    @app_commands.describe(
        xp_per_message="XP awarded per message (default 15–25 random).",
        cooldown="Seconds between XP grants per user (default 60).",
        levelup_channel="Channel for level-up notifications (blank = same channel).",
        levelup_message="Custom level-up message. Use {user}, {level}.",
        multiplier="Global XP multiplier (e.g. 2.0 for double XP).",
        enabled="Enable or disable leveling entirely.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def levelconfig(
        self,
        interaction: discord.Interaction,
        xp_per_message: int = 20,
        cooldown: int = 60,
        levelup_channel: discord.TextChannel | None = None,
        levelup_message: str = "🎉 {user} just levelled up to **Level {level}**!",
        multiplier: float = 1.0,
        enabled: bool = True,
    ):
        if not await self._admin_check(interaction):
            return
        update = {
            "leveling_enabled":    enabled,
            "xp_per_message":      max(1, min(xp_per_message, 100)),
            "xp_cooldown":         max(10, min(cooldown, 600)),
            "levelup_channel":     levelup_channel.id if levelup_channel else None,
            "levelup_message":     levelup_message,
            "xp_multiplier":       max(0.5, min(multiplier, 5.0)),
        }
        await _save_settings(self.bot, interaction.guild.id, update)
        embed = discord.Embed(title="📈 Leveling Config Updated", color=C_OK)
        embed.add_field(name="Enabled",    value="✅" if enabled else "❌", inline=True)
        embed.add_field(name="XP / msg",   value=f"`{update['xp_per_message']}`",  inline=True)
        embed.add_field(name="Cooldown",   value=f"`{update['xp_cooldown']}s`",    inline=True)
        embed.add_field(name="Multiplier", value=f"`×{update['xp_multiplier']}`",  inline=True)
        if levelup_channel:
            embed.add_field(name="Notif Channel", value=levelup_channel.mention, inline=True)
        embed.add_field(name="Level-up Msg", value=f"`{levelup_message}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /levelrole  — assign roles at certain levels
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="levelrole", description="Grant a role when a member reaches a level.")
    @app_commands.describe(
        level="Level at which the role is granted.",
        role="Role to grant.",
        remove="Remove this level-role binding instead.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def levelrole(
        self,
        interaction: discord.Interaction,
        level: int,
        role: discord.Role,
        remove: bool = False,
    ):
        if not await self._admin_check(interaction):
            return
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ Database not connected.", ephemeral=True)

        if remove:
            await self.bot.db.level_roles.delete_one(
                {"guild_id": interaction.guild.id, "level": level}
            )
            return await interaction.response.send_message(
                f"✅ Level-role binding for level `{level}` removed.", ephemeral=True
            )

        await self.bot.db.level_roles.update_one(
            {"guild_id": interaction.guild.id, "level": level},
            {"$set": {"role_id": role.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Members reaching **Level {level}** will receive {role.mention}.", ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /ticketsetup  — support ticket system
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="ticketsetup", description="Set up the support ticket system.")
    @app_commands.describe(
        category="Category where ticket channels will be created.",
        support_role="Role that can view and respond to all tickets.",
        log_channel="Channel where ticket transcripts are saved.",
        button_channel="Channel where the 'Open a Ticket' button is posted.",
        panel_message="Text above the ticket button.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def ticketsetup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        support_role: discord.Role,
        log_channel: discord.TextChannel,
        button_channel: discord.TextChannel,
        panel_message: str = "📬 Click the button below to open a support ticket.",
    ):
        if not await self._admin_check(interaction):
            return
        await _save_settings(self.bot, interaction.guild.id, {
            "ticket_category":     category.id,
            "ticket_support_role": support_role.id,
            "ticket_log_channel":  log_channel.id,
            "tickets_enabled":     True,
        })
        # Post the panel in the specified channel
        from cogs.tickets import TicketPanelView  # lazy import
        embed = discord.Embed(
            title="🎫 Support Tickets",
            description=panel_message,
            color=C_INFO,
        )
        embed.set_footer(text=interaction.guild.name, icon_url=interaction.guild.icon.url if interaction.guild.icon else None)
        await button_channel.send(embed=embed, view=TicketPanelView(self.bot))
        await interaction.response.send_message(
            f"✅ Ticket system configured!\n"
            f"• Category: {category.mention}\n"
            f"• Support: {support_role.mention}\n"
            f"• Transcripts: {log_channel.mention}\n"
            f"• Panel posted in {button_channel.mention}",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /starboard  — starboard setup
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="starboard", description="Configure the starboard.")
    @app_commands.describe(
        channel="Channel where starred messages appear.",
        threshold="Number of ⭐ reactions needed (default 3).",
        emoji="Reaction emoji to watch (default ⭐).",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def starboard(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        threshold: int = 3,
        emoji: str = "⭐",
    ):
        if not await self._admin_check(interaction):
            return
        if channel is None:
            await _save_settings(self.bot, interaction.guild.id, {"starboard_channel": None})
            return await interaction.response.send_message("✅ Starboard disabled.", ephemeral=True)
        await _save_settings(self.bot, interaction.guild.id, {
            "starboard_channel":   channel.id,
            "starboard_threshold": max(1, threshold),
            "starboard_emoji":     emoji,
        })
        await interaction.response.send_message(
            f"⭐ Starboard set to {channel.mention} — needs `{threshold}` × {emoji}.", ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /customcommand  — per-server custom text commands
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="customcommand", description="Create, edit or delete a custom text command.")
    @app_commands.describe(
        action="What to do.",
        name="Command trigger (no prefix needed).",
        response="What the bot replies (supports {user}, {server}).",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add/Edit", value="set"),
        app_commands.Choice(name="Delete",   value="delete"),
        app_commands.Choice(name="List",     value="list"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def customcommand(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        name: str | None = None,
        response: str | None = None,
    ):
        if not await self._admin_check(interaction):
            return
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ Database not connected.", ephemeral=True)

        gid = interaction.guild.id

        if action.value == "list":
            docs = await self.bot.db.custom_commands.find({"guild_id": gid}).to_list(50)
            if not docs:
                return await interaction.response.send_message("No custom commands set.", ephemeral=True)
            lines = [f"• `{d['name']}` → {d['response'][:60]}" for d in docs]
            embed = discord.Embed(title="🗒️ Custom Commands", description="\n".join(lines), color=C_INFO)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not name:
            return await interaction.response.send_message("❌ Provide a command name.", ephemeral=True)

        name = name.lower().strip()

        if action.value == "delete":
            await self.bot.db.custom_commands.delete_one({"guild_id": gid, "name": name})
            return await interaction.response.send_message(f"✅ Custom command `{name}` deleted.", ephemeral=True)

        if not response:
            return await interaction.response.send_message("❌ Provide a response.", ephemeral=True)

        await self.bot.db.custom_commands.update_one(
            {"guild_id": gid, "name": name},
            {"$set": {"response": response, "created_by": interaction.user.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Custom command `{name}` saved.", ephemeral=True
        )

    # ─── Custom command listener ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not hasattr(self.bot, "db"):
            return
        # Strip prefix if present, otherwise treat full content as trigger
        content = message.content.lower().strip().lstrip(",").strip()
        if not content:
            return
        doc = await self.bot.db.custom_commands.find_one(
            {"guild_id": message.guild.id, "name": content.split()[0]}
        )
        if doc:
            resp = doc["response"].replace("{user}", message.author.mention).replace("{server}", message.guild.name)
            await message.channel.send(resp)

    # ─────────────────────────────────────────────────────────────────────────
    # AI Config  (moved from ai.py — admin-only)
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="aiconfig", description="[Admin] Configure AI settings for this server.")
    @app_commands.describe(
        default_model="Default AI engine for all members who haven't set a preference.",
        ai_enabled="Turn AI chat responses on or off server-wide.",
        automod="Enable the AI-module word-filter automod.",
        allowed_channel="Restrict AI responses to this channel (leave blank = all channels).",
    )
    @app_commands.choices(default_model=[
        app_commands.Choice(name="Auto (Smart Routing)", value="auto"),
        app_commands.Choice(name="Gemini 2.5 Flash",    value="gemini"),
        app_commands.Choice(name="Nexusify GLM-5",      value="nexusify"),
        app_commands.Choice(name="Sarvam 30B",          value="sarvam"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def aiconfig(
        self,
        interaction: discord.Interaction,
        default_model: app_commands.Choice[str] | None = None,
        ai_enabled: bool | None = None,
        automod: bool | None = None,
        allowed_channel: discord.TextChannel | None = None,
    ):
        if not await self._admin_check(interaction):
            return
        update: dict = {}
        if default_model is not None:
            update["default_ai_model"] = default_model.value
        if ai_enabled is not None:
            update["ai_enabled"] = ai_enabled
        if automod is not None:
            update["automod_enabled"] = automod
        if allowed_channel is not None:
            # Append to the allowed list, or start a fresh list
            cfg = await _get_settings(self.bot, interaction.guild.id)
            allowed = cfg.get("ai_allowed_channels", [])
            if allowed_channel.id not in allowed:
                allowed.append(allowed_channel.id)
            update["ai_allowed_channels"] = allowed

        if update:
            await _save_settings(self.bot, interaction.guild.id, update)

        cfg = await _get_settings(self.bot, interaction.guild.id)
        embed = discord.Embed(title="🤖 AI Configuration", color=C_INFO)
        embed.add_field(name="AI Enabled",    value="✅" if cfg.get("ai_enabled", True)     else "❌", inline=True)
        embed.add_field(name="Default Model", value=AI_LABELS.get(cfg.get("default_ai_model", "auto"), "Auto"), inline=True)
        embed.add_field(name="Automod",       value="✅" if cfg.get("automod_enabled", False) else "❌", inline=True)

        allowed_ids = cfg.get("ai_allowed_channels", [])
        if allowed_ids:
            ch_mentions = " ".join(
                c.mention for cid in allowed_ids
                if (c := interaction.guild.get_channel(cid))
            )
            embed.add_field(name="Allowed Channels", value=ch_mentions or "*All*", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="aistats", description="[Admin] View AI usage telemetry for this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def aistats(self, interaction: discord.Interaction):
        if not await self._admin_check(interaction):
            return
        embed = discord.Embed(title="📊 AI Usage Stats", color=C_INFO,
                              timestamp=datetime.datetime.utcnow())
        if hasattr(self.bot, "db") and interaction.guild:
            today  = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            doc    = await self.bot.db.ai_telemetry.find_one(
                {"guild_id": interaction.guild.id, "date": today}
            )
            week   = datetime.datetime.utcnow() - datetime.timedelta(days=7)
            pipeline = [
                {"$match": {
                    "guild_id": interaction.guild.id,
                    "date": {"$gte": week.strftime("%Y-%m-%d")},
                }},
                {"$group": {"_id": None, "total": {"$sum": "$requests_processed"}}},
            ]
            result = await self.bot.db.ai_telemetry.aggregate(pipeline).to_list(1)
            week_total = result[0]["total"] if result else 0

            embed.add_field(name="Requests Today",       value=str(doc.get("requests_processed", 0) if doc else 0), inline=True)
            embed.add_field(name="Requests This Week",   value=str(week_total), inline=True)
        else:
            embed.description = "Database not connected."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /banned_words  — manage automod word list
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="banned_words", description="Add or remove words from the automod blacklist.")
    @app_commands.describe(
        action="Add or remove a word.",
        word="Word or phrase to add/remove.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add",    value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List",   value="list"),
        app_commands.Choice(name="Clear",  value="clear"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def banned_words(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        word: str | None = None,
    ):
        if not await self._admin_check(interaction):
            return
        cfg = await _get_settings(self.bot, interaction.guild.id)
        words: list = cfg.get("banned_words", [])

        if action.value == "list":
            if not words:
                return await interaction.response.send_message("No banned words configured.", ephemeral=True)
            return await interaction.response.send_message(
                f"🚫 Banned words: {', '.join(f'`{w}`' for w in words)}", ephemeral=True
            )

        if action.value == "clear":
            await _save_settings(self.bot, interaction.guild.id, {"banned_words": []})
            return await interaction.response.send_message("✅ Banned word list cleared.", ephemeral=True)

        if not word:
            return await interaction.response.send_message("❌ Provide a word.", ephemeral=True)

        word = word.lower().strip()
        if action.value == "add":
            if word not in words:
                words.append(word)
            await _save_settings(self.bot, interaction.guild.id, {"banned_words": words})
            await interaction.response.send_message(f"✅ `{word}` added to automod blacklist.", ephemeral=True)
        elif action.value == "remove":
            words = [w for w in words if w != word]
            await _save_settings(self.bot, interaction.guild.id, {"banned_words": words})
            await interaction.response.send_message(f"✅ `{word}` removed from automod blacklist.", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /resetuser  — wipe a user's server data (XP, strikes, warnings)
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="resetuser", description="Wipe a specific member's server data (XP / strikes / warnings).")
    @app_commands.describe(
        member="Member whose data to reset.",
        scope="What data to reset.",
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="Everything",   value="all"),
        app_commands.Choice(name="XP / Level",   value="xp"),
        app_commands.Choice(name="Strikes",      value="strikes"),
        app_commands.Choice(name="Warnings",     value="warnings"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def resetuser(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        scope: app_commands.Choice[str],
    ):
        if not await self._admin_check(interaction):
            return
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ Database not connected.", ephemeral=True)
        gid, uid = interaction.guild.id, member.id
        deleted = []
        if scope.value in ("all", "xp"):
            await self.bot.db.levels.delete_one({"guild_id": gid, "user_id": uid})
            deleted.append("XP / Level")
        if scope.value in ("all", "strikes"):
            await self.bot.db.user_strikes.delete_one({"guild_id": gid, "user_id": uid})
            deleted.append("Strikes")
        if scope.value in ("all", "warnings"):
            await self.bot.db.warnings.delete_many({"guild_id": gid, "user_id": uid})
            deleted.append("Warnings")
        await interaction.response.send_message(
            f"✅ Reset **{', '.join(deleted)}** for {member.mention}.", ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Welcome / Leave  listener  (owned here, not in a separate cog)
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await _get_settings(self.bot, member.guild.id)
        # Auto-role
        auto_role_id = cfg.get("auto_role")
        if auto_role_id:
            if not member.bot or cfg.get("auto_role_bots", False):
                role = member.guild.get_role(auto_role_id)
                if role:
                    try:
                        await member.add_roles(role, reason="Auto-role on join")
                    except discord.Forbidden:
                        pass
        # Welcome message
        if not cfg.get("welcome_enabled", True):
            return
        ch_id = cfg.get("welcome_channel")
        if not ch_id:
            return
        channel = member.guild.get_channel(ch_id)
        if not channel:
            return
        msg = cfg.get("welcome_message", "👋 Welcome to **{server}**, {user}! You're member #{count}.")
        msg = (
            msg.replace("{user}",   member.mention)
               .replace("{server}", member.guild.name)
               .replace("{count}",  str(member.guild.member_count))
               .replace("{name}",   member.display_name)
        )
        embed = discord.Embed(description=msg, color=C_OK)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=member.guild.name)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass
        # DM
        if cfg.get("welcome_dm"):
            try:
                await member.send(embed=embed)
            except discord.Forbidden:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await _get_settings(self.bot, member.guild.id)
        if not cfg.get("welcome_enabled", True):
            return
        ch_id = cfg.get("welcome_channel")
        if not ch_id:
            return
        channel = member.guild.get_channel(ch_id)
        if not channel:
            return
        msg = cfg.get("leave_message", "👋 **{user}** has left the server. Goodbye!")
        msg = (
            msg.replace("{user}",   member.display_name)
               .replace("{name}",   member.display_name)
               .replace("{server}", member.guild.name)
        )
        embed = discord.Embed(description=msg, color=C_ERR)
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Message-edit / delete logging
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        cfg = await _get_settings(self.bot, message.guild.id)
        ch_id  = cfg.get("log_channel")
        filt   = cfg.get("log_event_filter", "all")
        if not ch_id or filt not in ("all", "messages"):
            return
        channel = message.guild.get_channel(ch_id)
        if not channel:
            return
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=C_ERR,
            timestamp=datetime.datetime.utcnow(),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Author",  value=message.author.mention,  inline=True)
        embed.add_field(name="Content", value=(message.content[:1020] or "*empty*"), inline=False)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild or before.content == after.content:
            return
        cfg = await _get_settings(self.bot, before.guild.id)
        ch_id = cfg.get("log_channel")
        filt  = cfg.get("log_event_filter", "all")
        if not ch_id or filt not in ("all", "messages"):
            return
        channel = before.guild.get_channel(ch_id)
        if not channel:
            return
        embed = discord.Embed(
            title="✏️ Message Edited",
            color=C_WARN,
            timestamp=datetime.datetime.utcnow(),
        )
        embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Before",  value=before.content[:512] or "*empty*", inline=False)
        embed.add_field(name="After",   value=after.content[:512]  or "*empty*", inline=False)
        embed.add_field(name="Jump",    value=f"[View Message]({after.jump_url})", inline=False)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        cfg = await _get_settings(self.bot, guild.id)
        ch_id = cfg.get("log_channel")
        filt  = cfg.get("log_event_filter", "all")
        if not ch_id or filt not in ("all", "mod", "members"):
            return
        channel = guild.get_channel(ch_id)
        if not channel:
            return
        embed = discord.Embed(title="🔨 Member Banned", color=C_ERR, timestamp=datetime.datetime.utcnow())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        cfg = await _get_settings(self.bot, guild.id)
        ch_id = cfg.get("log_channel")
        filt  = cfg.get("log_event_filter", "all")
        if not ch_id or filt not in ("all", "mod", "members"):
            return
        channel = guild.get_channel(ch_id)
        if not channel:
            return
        embed = discord.Embed(title="🔓 Member Unbanned", color=C_OK, timestamp=datetime.datetime.utcnow())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Starboard listener
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):  # noqa: F811
        # We handle BOTH reaction-roles AND starboard in the same event.
        # Reaction-role part is handled above; here we handle starboard.
        if not payload.guild_id:
            return
        cfg = await _get_settings(self.bot, payload.guild_id)
        sb_ch_id  = cfg.get("starboard_channel")
        threshold = cfg.get("starboard_threshold", 3)
        sb_emoji  = cfg.get("starboard_emoji", "⭐")
        if not sb_ch_id or str(payload.emoji) != sb_emoji:
            return

        guild   = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id) if guild else None
        sb_ch   = guild.get_channel(sb_ch_id) if guild else None
        if not channel or not sb_ch:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        star_rxn = discord.utils.get(message.reactions, emoji=sb_emoji)
        count    = star_rxn.count if star_rxn else 0
        if count < threshold:
            return

        # Check if already posted
        if hasattr(self.bot, "db"):
            existing = await self.bot.db.starboard.find_one(
                {"guild_id": payload.guild_id, "original_id": message.id}
            )
        else:
            existing = None

        embed = discord.Embed(
            description=message.content[:2048] or "*[No text content]*",
            color=discord.Color.gold(),
            timestamp=message.created_at,
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)
        embed.set_footer(text=f"{sb_emoji} {count} | #{channel.name}")
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)

        try:
            if existing:
                # Edit the existing starboard post
                sb_msg = await sb_ch.fetch_message(existing["sb_message_id"])
                await sb_msg.edit(embed=embed)
            else:
                sb_msg = await sb_ch.send(embed=embed)
                if hasattr(self.bot, "db"):
                    await self.bot.db.starboard.insert_one({
                        "guild_id":     payload.guild_id,
                        "original_id":  message.id,
                        "sb_message_id": sb_msg.id,
                    })
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
