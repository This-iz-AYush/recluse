"""
core.py  —  Recluse Bot  v2.0
Core telemetry, help, and status management.

Help system:
  • All users see 12 public categories via a dropdown
  • Owners + co-owners see an additional 🔐 Developer category
  • The Developer option is only injected at construction time
    AND re-verified at interaction time — cannot be spoofed
"""

import datetime
import time
from itertools import cycle

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ─── Colour palette ───────────────────────────────────────────────────────────
C_INFO    = discord.Color(0x5865F2)
C_OK      = discord.Color.brand_green()
C_NEUTRAL = discord.Color(0x2b2d31)
C_ERR     = discord.Color.red()


# ═════════════════════════════════════════════════════════════════════════════
# Co-owner helper  — checks both bot.owner_ids and the DB co_owners collection
# ═════════════════════════════════════════════════════════════════════════════

async def is_dev(bot: commands.Bot, user: discord.User | discord.Member) -> bool:
    """Return True if the user is the bot owner or a registered co-owner."""
    if await bot.is_owner(user):
        return True
    if hasattr(bot, "db"):
        doc = await bot.db.co_owners.find_one({"user_id": user.id})
        if doc:
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Help embeds  — one function per category
# ═════════════════════════════════════════════════════════════════════════════

def _embed_telemetry() -> discord.Embed:
    e = discord.Embed(title="📊 Telemetry Commands", color=C_INFO)
    e.add_field(
        name="System Operations",
        value=(
            "> `/botinfo` — Application telemetry and metadata.\n"
            "> `/ping` — Network and websocket latency."
        ),
        inline=False,
    )
    return e


def _embed_anime() -> discord.Embed:
    e = discord.Embed(title="🎌 Anime & Manga Commands", color=discord.Color.red())
    e.add_field(
        name="Database Search",
        value=(
            "> `/anime <query>` — Queries MyAnimeList for anime.\n"
            "> `/manga <query>` — Queries MyAnimeList for manga."
        ),
        inline=False,
    )
    return e


def _embed_sports() -> discord.Embed:
    e = discord.Embed(title="🏏 Sports Commands", color=discord.Color.orange())
    e.add_field(
        name="Live Cricket Coverage",
        value=(
            "> `/score all` — All live matches overview.\n"
            "> `/score search <query>` — Find a specific match.\n"
            "> `/score live <query>` — Auto-updating match tracker.\n"
            "> `/score stop` — Halt active trackers."
        ),
        inline=False,
    )
    return e


def _embed_moderation() -> discord.Embed:
    e = discord.Embed(title="🛡️ Moderation Commands", color=discord.Color.green())
    e.add_field(name="🛑 Access Control",  value="> `/ban` `/tempban` `/softban` `/kick` `/unban`",              inline=False)
    e.add_field(name="🔇 Restrictions",    value="> `/tempmute` `/unmute` `/vmute` `/vunmute` `/vckick`",         inline=False)
    e.add_field(name="⚠️ Warning System", value="> `/warn` `/warnings` `/delwarn` `/clearwarns` `/moderations`", inline=False)
    e.add_field(name="🛠️ Channel Mgmt",   value="> `/purge` `/clean` `/lock` `/unlock` `/slowmode`",             inline=False)
    e.add_field(name="👥 Member Mgmt",     value="> `/role` `/nick` `/members`",                                 inline=False)
    return e


def _embed_misc() -> discord.Embed:
    e = discord.Embed(title="🗂️ Miscellaneous Commands", color=discord.Color.teal())
    e.add_field(name="👤 User Utilities", value="> `/whois` `/avatar` `/afk`",                              inline=False)
    e.add_field(name="🏢 Server Info",    value="> `/serverinfo` `/roleinfo` `/channelinfo` `/membercount`", inline=False)
    e.add_field(name="🧰 General Tools", value="> `/poll` `/color` `/search`",                              inline=False)
    return e


def _embed_gen_ai() -> discord.Embed:
    e = discord.Embed(title="🎨 Generative AI", color=C_INFO)
    e.add_field(
        name="Image Creation",
        value=(
            "> `/imagine <prompt> [model]` — Generate an image.\n"
            "> `/describe <image> [question]` — Analyse an attached image."
        ),
        inline=False,
    )
    return e


def _embed_conv_ai() -> discord.Embed:
    e = discord.Embed(title="🤖 Conversational AI", color=discord.Color.purple())
    e.add_field(
        name="Interaction",
        value=(
            "> `@Recluse <message>` — Chat directly in any channel.\n"
            "> `/choose_ai <model>` — Switch your AI engine.\n"
            "> `/clear_memory` — Wipe conversation history."
        ),
        inline=False,
    )
    return e


def _embed_leveling() -> discord.Embed:
    e = discord.Embed(title="📈 Leveling Commands", color=discord.Color(0xF1C40F))
    e.add_field(
        name="For Members",
        value=(
            "> `/rank [member]` — View your or someone's rank card.\n"
            "> `/leaderboard [page]` — Server XP leaderboard."
        ),
        inline=False,
    )
    e.add_field(
        name="For Admins",
        value=(
            "> `/givexp <member> <amount>` — Give XP.\n"
            "> `/setlevel <member> <level>` — Force-set a level.\n"
            "> `/resetxp <member>` — Wipe XP.\n"
            "> `/levelconfig` — Configure XP rates & level-up messages.\n"
            "> `/levelrole <level> <role>` — Grant role on level-up."
        ),
        inline=False,
    )
    return e


def _embed_tickets() -> discord.Embed:
    e = discord.Embed(title="🎫 Ticket System", color=C_INFO)
    e.add_field(
        name="Setup (Admins)",
        value=(
            "> `/ticketsetup` — Configure the system and post the panel.\n"
            "> `/tickets` — List all open tickets.\n"
            "> `/addtoticket <member>` — Add someone to a ticket.\n"
            "> `/removeticket <member>` — Remove someone from a ticket."
        ),
        inline=False,
    )
    e.add_field(
        name="In Ticket Channel",
        value=(
            "> **Claim** button — Staff claims the ticket.\n"
            "> **Close** button — Saves transcript and deletes channel."
        ),
        inline=False,
    )
    return e


def _embed_giveaways() -> discord.Embed:
    e = discord.Embed(title="🎉 Giveaway Commands", color=discord.Color.gold())
    e.add_field(
        name="Commands",
        value=(
            "> `/giveaway start` — Start a giveaway.\n"
            "> `/giveaway end <msg_id>` — Force-end early.\n"
            "> `/giveaway reroll <msg_id>` — Reroll winners.\n"
            "> `/giveaway list` — Show active giveaways."
        ),
        inline=False,
    )
    return e


def _embed_admin() -> discord.Embed:
    e = discord.Embed(title="⚙️ Admin Configuration", color=C_INFO)
    e.add_field(
        name="Server Setup",
        value=(
            "> `/setup` — Interactive setup wizard.\n"
            "> `/config` — View all current settings.\n"
            "> `/modules` — Enable/disable bot modules."
        ),
        inline=False,
    )
    e.add_field(
        name="Systems",
        value=(
            "> `/setlog` — Set mod log channel.\n"
            "> `/setwelcome` — Configure welcome/leave messages.\n"
            "> `/autorole` — Auto-assign roles on join.\n"
            "> `/reactionrole` — Bind reactions to roles.\n"
            "> `/antispam` — Configure auto-mod.\n"
            "> `/banned_words` — Manage word blacklist.\n"
            "> `/starboard` — Set up the starboard.\n"
            "> `/customcommand` — Create server-specific commands.\n"
            "> `/aiconfig` — Configure AI for this server.\n"
            "> `/levelconfig` — Configure XP leveling.\n"
            "> `/ticketsetup` — Configure ticket system."
        ),
        inline=False,
    )
    return e


def _embed_antinuke() -> discord.Embed:
    e = discord.Embed(title="🔒 Anti-Nuke System", color=discord.Color.red())
    e.add_field(
        name="Commands",
        value=(
            "> `/antinuke enable` — Turn protection on or off.\n"
            "> `/antinuke config` — Set threshold, window, action, log channel.\n"
            "> `/antinuke whitelist` — Trust a user or bot.\n"
            "> `/antinuke status` — View current config & trigger count.\n"
            "> `/antinuke logs` — Last 10 detections.\n"
            "> `/antinuke test` — Send a test alert to verify setup."
        ),
        inline=False,
    )
    return e


def _embed_developer() -> discord.Embed:
    """
    Developer-only category embed.
    Only rendered after identity is re-verified at callback time.
    """
    e = discord.Embed(
        title="🔐 Developer Console",
        description=(
            "These commands are restricted to the **bot owner and co-owners only**.\n"
            "They will silently fail for everyone else.\n\u200b"
        ),
        color=discord.Color(0xFF6B35),
        timestamp=datetime.datetime.utcnow(),
    )

    e.add_field(
        name="🌐 Network Security  (`/network`)",
        value=(
            "> `/network blacklist_user <id> [reason]` — Globally ban a user.\n"
            "> `/network unblacklist_user <id>` — Remove a user ban.\n"
            "> `/network blacklist_guild <id> [reason]` — Blacklist & force-leave a server.\n"
            "> `/network unblacklist_guild <id>` — Remove a guild ban.\n"
            "> `/network lockdown <true|false>` — Engage / lift global lockdown.\n"
            "> `/network force_leave <guild_id>` — Leave a server without blacklisting.\n"
            "> `/network whitelist_owner <id> [remove]` — Add or remove a co-owner."
        ),
        inline=False,
    )

    e.add_field(
        name="⚙️ System Management",
        value=(
            "> `/restart [mode] [delay]` — Reboot / shutdown / update / maintain.\n"
            "> `/reload [extension] [all_cogs]` — Hot-reload one or all cogs.\n"
            "> `/sync [guild_id] [clear]` — Sync slash commands to Discord.\n"
            "> `/shutdown` — Graceful shutdown with confirm button.\n"
            "> `/setavatar <url> [banner]` — Change bot avatar or banner.\n"
            "> `/maintenance <state> [guild_id]` — Lock all guilds except home."
        ),
        inline=False,
    )

    e.add_field(
        name="🖥️ Live Eval Engine",
        value=(
            "> `/eval <code>` — Execute Python in-process (supports `await`).\n"
            "> `/shell <command>` — Run a shell command on the host.\n"
            "> `,eval <code>` — One-shot prefix eval.\n"
            "> `,shell <cmd>` / `,sh <cmd>` / `,bash <cmd>` — Prefix shell.\n"
            "> `,terminal` / `,term` / `,repl` — Toggle a **persistent REPL** in this channel.\n"
            "> `,cls` — Reset the eval engine's global namespace."
        ),
        inline=False,
    )

    e.add_field(
        name="📢 Broadcast & Messaging",
        value=(
            "> `/echo <channel> <msg>` — Send a message to any channel.\n"
            "> `/globalecho <msg> [embed_style]` — Broadcast to every server.\n"
            "> `/dm <user_id> <msg>` — DM any user by ID.\n"
            "> `/announce <channel> <title> <body>` — Post a styled embed."
        ),
        inline=False,
    )

    e.add_field(
        name="🔬 Inspection & Diagnostics",
        value=(
            "> `/guilds [sort] [search]` — Paginated server browser.\n"
            "> `/userinfo <id>` — Cross-server user lookup + blacklist status.\n"
            "> `/guildinfo <id>` — Guild info by ID (even if not in it).\n"
            "> `/dbstats` — MongoDB collection document counts.\n"
            "> `/botstats` — Full runtime diagnostics (OS, Python, latency, uptime).\n"
            "> `/checkperms [guild_id]` — Audit bot permissions across all channels."
        ),
        inline=False,
    )

    e.add_field(
        name="🗄️ Database Maintenance",
        value=(
            "> `/purgedb <collection> [older_than_days]` — Delete old records.\n"
            "> `/resetguild <guild_id>` — Wipe **all** stored data for a guild."
        ),
        inline=False,
    )

    e.set_footer(text="🔐 Visible to owner & co-owners only  •  Recluse Dev Console")
    return e


# ─── Category → embed builder map ────────────────────────────────────────────
_PUBLIC_BUILDERS: dict[str, callable] = {
    "Telemetry":         _embed_telemetry,
    "Anime & Manga":     _embed_anime,
    "Sports":            _embed_sports,
    "Moderation":        _embed_moderation,
    "Miscellaneous":     _embed_misc,
    "Generative AI":     _embed_gen_ai,
    "Conversational AI": _embed_conv_ai,
    "Leveling":          _embed_leveling,
    "Tickets":           _embed_tickets,
    "Giveaways":         _embed_giveaways,
    "Admin Config":      _embed_admin,
    "Anti-Nuke":         _embed_antinuke,
}

_DEV_LABEL = "🔐 Developer"


# ═════════════════════════════════════════════════════════════════════════════
# Help UI  — dynamically built per invoker
# ═════════════════════════════════════════════════════════════════════════════

class HelpSelect(discord.ui.Select):
    """
    Dynamically-built category selector.
    The Developer option is only appended when include_dev=True, which is only
    set by custom_help() after verifying the invoker is owner or co-owner.

    Security model:
      1. include_dev=True → Discord shows the option to this user.
      2. callback() re-calls is_dev() on the clicking user.
         If they somehow see the embed but are no longer authorised,
         they get an ephemeral denial — the real content is never sent.
    """

    def __init__(self, bot: commands.Bot, include_dev: bool = False):
        self.bot         = bot
        self.include_dev = include_dev

        options = [
            discord.SelectOption(label="Telemetry",         description="Bot info and uptime stats",         emoji="📊"),
            discord.SelectOption(label="Anime & Manga",     description="Search MyAnimeList database",        emoji="🎌"),
            discord.SelectOption(label="Sports",            description="Live Cricket Score",                 emoji="🏏"),
            discord.SelectOption(label="Moderation",        description="Ban, mute, purge and more",          emoji="🛡️"),
            discord.SelectOption(label="Miscellaneous",     description="Server info, avatars, ping, afk",    emoji="🗂️"),
            discord.SelectOption(label="Generative AI",     description="Create images from text",            emoji="🎨"),
            discord.SelectOption(label="Conversational AI", description="Chat with Recluse",                  emoji="🤖"),
            discord.SelectOption(label="Leveling",          description="XP, ranks and leaderboards",         emoji="📈"),
            discord.SelectOption(label="Tickets",           description="Support ticket system",              emoji="🎫"),
            discord.SelectOption(label="Giveaways",         description="Host and manage giveaways",          emoji="🎉"),
            discord.SelectOption(label="Admin Config",      description="Server setup and configuration",     emoji="⚙️"),
            discord.SelectOption(label="Anti-Nuke",         description="Nuke detection & server protection", emoji="🔒"),
        ]

        if include_dev:
            options.append(
                discord.SelectOption(
                    label=_DEV_LABEL,
                    description="Owner & co-owner only control panel",
                    emoji="🔐",
                )
            )

        super().__init__(
            placeholder="Choose a command category…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        sel = self.values[0]

        # ── Developer gate: re-verify at interaction time ────────────────────
        if sel == _DEV_LABEL:
            if not await is_dev(self.bot, interaction.user):
                return await interaction.response.send_message(
                    "🔐 **Access Denied:** This category is restricted to the bot owner and co-owners.",
                    ephemeral=True,
                )
            return await interaction.response.edit_message(embed=_embed_developer())

        # ── Public categories ────────────────────────────────────────────────
        builder = _PUBLIC_BUILDERS.get(sel)
        if builder:
            embed = builder()
        else:
            embed = discord.Embed(title="Error", description="Category not found.", color=C_ERR)

        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot, include_dev: bool = False):
        super().__init__(timeout=120)
        self.add_item(HelpSelect(bot, include_dev=include_dev))


# ═════════════════════════════════════════════════════════════════════════════
# Core Cog
# ═════════════════════════════════════════════════════════════════════════════

class Core(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot          = bot
        self.start_time   = time.time()
        self.STATUS_MESSAGES = [
            "active in {servers} servers",
            "/help | Recluse v2.0",
            "watching {members} members",
            "Type @Recluse to chat!",
        ]
        self.status_cycle = cycle(self.STATUS_MESSAGES)
        self.cycle_bot_status.start()
        self.uptime_heartbeat.start()

    def cog_unload(self):
        self.cycle_bot_status.cancel()
        self.uptime_heartbeat.cancel()

    # ─── blacklist guard ──────────────────────────────────────────────────────

    async def cog_check(self, ctx: commands.Context) -> bool:
        if hasattr(self.bot, "db"):
            bl = await self.bot.db.global_blacklist.find_one(
                {"target_id": ctx.author.id, "type": "user"}
            )
            if bl:
                try:
                    await ctx.send("❌ **Access Denied:** You are globally blacklisted.", ephemeral=True)
                except Exception:
                    pass
                return False
        return True

    # ─── command autocomplete ─────────────────────────────────────────────────

    async def command_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cmds    = [c.name for c in self.bot.commands if not c.hidden]
        matches = [c for c in cmds if current.lower() in c.lower()]
        return [app_commands.Choice(name=m, value=m) for m in matches[:25]]

    # ─────────────────────────────────────────────────────────────────────────
    # /help
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="help",
        description="Shows help info and commands.",
        usage="/help [command]",
        help="/help roleinfo",
    )
    @app_commands.autocomplete(command=command_autocomplete)
    async def custom_help(self, ctx: commands.Context, command: str = None):

        # ── Specific command lookup ───────────────────────────────────────────
        if command:
            cmd = self.bot.get_command(command)
            if not cmd:
                return await ctx.send(f"❌ Command `{command}` not found.", ephemeral=True)
            desc  = f"**Command:** `/{cmd.name}`\n\n"
            desc += f"**Description:** {cmd.description or 'No description provided.'}\n"
            if getattr(cmd, "_buckets", None) and cmd._buckets._cooldown:
                desc += f"**Cooldown:** {int(cmd._buckets._cooldown.per)}s\n"
            desc += f"**Usage:** `{cmd.usage or f'/{cmd.name} {cmd.signature}'.strip()}`\n"
            desc += f"**Example:** `{cmd.help or f'/{cmd.name}'}`"
            embed = discord.Embed(description=desc, color=C_NEUTRAL)
            return await ctx.send(embed=embed)

        # ── Category menu ─────────────────────────────────────────────────────
        # Check once here to decide whether to inject the Developer option
        dev = await is_dev(self.bot, ctx.author)

        embed = discord.Embed(title="Recluse Help Desk", color=C_INFO)

        if dev:
            embed.description = (
                "Select a category below to browse commands.\n"
                "-# 🔐 Developer Console is visible because you are an owner or co-owner."
            )
        else:
            embed.description = "Select a category below to browse commands."

        embed.add_field(
            name="🌐 Dashboard",
            value="[Visit Dashboard](https://recluse-1.onrender.com/)",
            inline=True,
        )
        embed.add_field(
            name="📈 Uptime",
            value="[Status Page](https://sszvcg5v.status.cron-job.org)",
            inline=True,
        )
        embed.add_field(
            name="📊 Categories",
            value=f"`{'13' if dev else '12'}` categories",
            inline=True,
        )

        view = HelpView(self.bot, include_dev=dev)
        await ctx.send(embed=embed, view=view)

    # ─────────────────────────────────────────────────────────────────────────
    # /botinfo
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="botinfo",
        description="Retrieves application telemetry and metadata.",
        usage="/botinfo",
        help="/botinfo",
    )
    async def botinfo(self, ctx: commands.Context):
        await ctx.defer()
        app_info  = await self.bot.application_info()
        ai_cog    = self.bot.get_cog("AI")
        active_ai = "Auto"
        if ai_cog:
            pref = ai_cog._model_pref.get(ctx.author.id, "auto")
            active_ai = {
                "auto":     "🤖 Auto",
                "gemini":   "✨ Gemini",
                "nexusify": "⚡ Nexusify",
                "sarvam":   "🇮🇳 Sarvam",
            }.get(pref, pref.title())

        uptime_seconds = max(0, int(time.time() - self.start_time))
        if hasattr(self.bot, "db"):
            data = await self.bot.db.bot_telemetry.find_one({"id": "uptime"})
            if data:
                uptime_seconds = max(0, int(time.time()) - data.get("start_time", int(time.time())))

        days, r    = divmod(uptime_seconds, 86400)
        hours, r   = divmod(r, 3600)
        mins, secs = divmod(r, 60)

        guild_count  = len(self.bot.guilds)
        member_count = sum(g.member_count for g in self.bot.guilds if g.member_count)

        embed = discord.Embed(
            title="⚙️ System Telemetry",
            color=C_INFO,
            timestamp=datetime.datetime.utcnow(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="👑 Owner",       value=str(app_info.owner),                                       inline=True)
        embed.add_field(name="📡 WS Latency",  value=f"`{round(self.bot.latency * 1000)}ms`",                   inline=True)
        embed.add_field(name="🧠 Your AI",     value=active_ai,                                                 inline=True)
        embed.add_field(name="🟢 Status",      value="**Operational**",                                         inline=True)
        embed.add_field(name="⏱️ Uptime",      value=f"`{days}d {hours}h {mins}m {secs}s`",                     inline=True)
        embed.add_field(name="🏘️ Servers",    value=f"`{guild_count:,}` servers, `{member_count:,}` members",  inline=True)
        embed.add_field(name="🔧 Modules",     value="`11` loaded",                                             inline=True)
        embed.add_field(name="🐍 Library",     value=f"`discord.py {discord.__version__}`",                     inline=True)
        await ctx.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /ping
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="ping",
        description="Network latency diagnostics.",
        usage="/ping",
        help="/ping",
    )
    async def ping(self, ctx: commands.Context):
        import time as _t
        t0  = _t.perf_counter()
        msg = await ctx.send("🏓 Measuring latency…")
        api = round((_t.perf_counter() - t0) * 1000)
        ws  = round(self.bot.latency * 1000)

        colour  = discord.Color.green() if ws < 100 else discord.Color.yellow() if ws < 200 else C_ERR
        bar_len = 10
        filled  = max(0, bar_len - int(ws / 30))
        bar     = "█" * filled + "░" * (bar_len - filled)

        embed = discord.Embed(title="🏓 Pong!", color=colour, timestamp=datetime.datetime.utcnow())
        embed.add_field(name="📡 Gateway WS",    value=f"`{ws}ms`  `[{bar}]`", inline=False)
        embed.add_field(name="🌐 API Round-trip", value=f"`{api}ms`",           inline=True)
        embed.set_footer(text="Recluse Network Diagnostics")
        await msg.edit(content=None, embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # Uptime heartbeat
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def uptime_heartbeat(self):
        if not hasattr(self.bot, "db"):
            return
        now  = int(time.time())
        data = await self.bot.db.bot_telemetry.find_one({"id": "uptime"})
        if not data:
            await self.bot.db.bot_telemetry.insert_one(
                {"id": "uptime", "start_time": now, "last_heartbeat": now}
            )
        else:
            last = data.get("last_heartbeat", now)
            if now - last > 1200:
                await self.bot.db.bot_telemetry.update_one(
                    {"id": "uptime"}, {"$set": {"start_time": now, "last_heartbeat": now}}
                )
            else:
                await self.bot.db.bot_telemetry.update_one(
                    {"id": "uptime"}, {"$set": {"last_heartbeat": now}}
                )

    @uptime_heartbeat.before_loop
    async def _before_heartbeat(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # Status cycle
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=20)
    async def cycle_bot_status(self):
        try:
            sports_cog     = self.bot.get_cog("Sports")
            active_matches = len(sports_cog.live_trackers) if sports_cog else 0
            active_gws     = 0
            if hasattr(self.bot, "db"):
                active_gws = await self.bot.db.giveaways.count_documents({"active": True})

            if active_matches > 0:
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{active_matches} live cricket match{'es' if active_matches > 1 else ''}",
                )
            elif active_gws > 0:
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"🎉 {active_gws} active giveaway{'s' if active_gws > 1 else ''}",
                )
            else:
                servers = len(self.bot.guilds)
                members = sum(g.member_count for g in self.bot.guilds if g.member_count)
                raw     = next(self.status_cycle)
                name    = raw.replace("{servers}", str(servers)).replace("{members}", str(members))
                activity = discord.Game(name=name)

            await self.bot.change_presence(activity=activity)
        except Exception:
            pass

    @cycle_bot_status.before_loop
    async def _before_cycle(self):
        await self.bot.wait_until_ready()


# ─────────────────────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(Core(bot))
