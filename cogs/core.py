"""
core.py  —  Recluse Bot  v2.0
Core telemetry, help, and status management.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import time
import datetime
from itertools import cycle
from typing import Optional

# ─── colour palette ───────────────────────────────────────────────────────────
C_INFO    = discord.Color(0x5865F2)
C_OK      = discord.Color.brand_green()
C_NEUTRAL = discord.Color(0x2b2d31)


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Telemetry",        description="Bot info and uptime stats",        emoji="📊"),
            discord.SelectOption(label="Anime & Manga",    description="Search MyAnimeList database",       emoji="🎌"),
            discord.SelectOption(label="Moderation",       description="Ban, mute, purge and more",         emoji="🛡️"),
            discord.SelectOption(label="Sports",           description="Live Cricket Score",                emoji="🏏"),
            discord.SelectOption(label="Miscellaneous",    description="Server info, avatars, ping, afk",   emoji="🗂️"),
            discord.SelectOption(label="Generative AI",    description="Create images from text",           emoji="🎨"),
            discord.SelectOption(label="Conversational AI",description="Chat with Recluse",                 emoji="🤖"),
            discord.SelectOption(label="Leveling",         description="XP, ranks and leaderboards",        emoji="📈"),
            discord.SelectOption(label="Tickets",          description="Support ticket system",             emoji="🎫"),
            discord.SelectOption(label="Giveaways",        description="Host and manage giveaways",         emoji="🎉"),
            discord.SelectOption(label="Admin Config",     description="Server setup and configuration",    emoji="⚙️"),
        ]
        super().__init__(
            placeholder="Choose a command category…",
            min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        sel = self.values[0]
        embed: discord.Embed

        if sel == "Telemetry":
            embed = discord.Embed(title="📊 Telemetry Commands", color=C_INFO)
            embed.add_field(
                name="System Operations",
                value=(
                    "> `/botinfo` — Application telemetry and metadata.\n"
                    "> `/ping` — Network and websocket latency."
                ),
                inline=False,
            )

        elif sel == "Anime & Manga":
            embed = discord.Embed(title="🎌 Anime & Manga Commands", color=discord.Color.red())
            embed.add_field(
                name="Database Search",
                value=(
                    "> `/anime <query>` — Queries MyAnimeList for anime.\n"
                    "> `/manga <query>` — Queries MyAnimeList for manga."
                ),
                inline=False,
            )

        elif sel == "Sports":
            embed = discord.Embed(title="🏏 Sports Commands", color=discord.Color.orange())
            embed.add_field(
                name="Live Cricket Coverage",
                value=(
                    "> `/score all` — All live matches overview.\n"
                    "> `/score search <query>` — Find a specific match.\n"
                    "> `/score live <query>` — Auto-updating match tracker.\n"
                    "> `/score stop` — Halt active trackers."
                ),
                inline=False,
            )

        elif sel == "Moderation":
            embed = discord.Embed(title="🛡️ Moderation Commands", color=discord.Color.green())
            embed.add_field(name="🛑 Access Control",    value="> `/ban` `/tempban` `/softban` `/kick` `/unban`",                                      inline=False)
            embed.add_field(name="🔇 Restrictions",      value="> `/tempmute` `/unmute` `/vmute` `/vunmute` `/vckick`",                                 inline=False)
            embed.add_field(name="⚠️ Warning System",   value="> `/warn` `/warnings` `/delwarn` `/clearwarns` `/moderations`",                         inline=False)
            embed.add_field(name="🛠️ Channel Mgmt",     value="> `/purge` `/clean` `/lock` `/unlock` `/slowmode`",                                     inline=False)
            embed.add_field(name="👥 Member Mgmt",       value="> `/role` `/nick` `/members`",                                                          inline=False)

        elif sel == "Miscellaneous":
            embed = discord.Embed(title="🗂️ Miscellaneous Commands", color=discord.Color.teal())
            embed.add_field(name="👤 User Utilities",   value="> `/whois` `/avatar` `/afk`",                                                            inline=False)
            embed.add_field(name="🏢 Server Info",      value="> `/serverinfo` `/roleinfo` `/channelinfo` `/membercount`",                              inline=False)
            embed.add_field(name="🧰 General Tools",    value="> `/poll` `/color` `/search`",                                                           inline=False)

        elif sel == "Generative AI":
            embed = discord.Embed(title="🎨 Generative AI", color=C_INFO)
            embed.add_field(
                name="Image Creation",
                value=(
                    "> `/imagine <prompt> [model]` — Generate an image.\n"
                    "> `/describe <image> [question]` — Analyze an attached image."
                ),
                inline=False,
            )

        elif sel == "Conversational AI":
            embed = discord.Embed(title="🤖 Conversational AI", color=discord.Color.purple())
            embed.add_field(
                name="Interaction",
                value=(
                    "> `@Recluse <message>` — Chat directly in any channel.\n"
                    "> `/choose_ai <model>` — Switch your AI engine.\n"
                    "> `/clear_memory` — Wipe conversation history."
                ),
                inline=False,
            )

        elif sel == "Leveling":
            embed = discord.Embed(title="📈 Leveling Commands", color=discord.Color(0xf1c40f))
            embed.add_field(
                name="For Members",
                value=(
                    "> `/rank [member]` — View your or someone's rank card.\n"
                    "> `/leaderboard [page]` — Server XP leaderboard."
                ),
                inline=False,
            )
            embed.add_field(
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

        elif sel == "Tickets":
            embed = discord.Embed(title="🎫 Ticket System", color=C_INFO)
            embed.add_field(
                name="Setup (Admins)",
                value=(
                    "> `/ticketsetup` — Configure the ticket system and post the panel.\n"
                    "> `/tickets` — List all open tickets.\n"
                    "> `/addtoticket <member>` — Add someone to a ticket.\n"
                    "> `/removeticket <member>` — Remove someone from a ticket."
                ),
                inline=False,
            )
            embed.add_field(
                name="In Ticket",
                value=(
                    "> **Claim** — Staff claims the ticket.\n"
                    "> **Close** — Saves transcript and deletes channel."
                ),
                inline=False,
            )

        elif sel == "Giveaways":
            embed = discord.Embed(title="🎉 Giveaway Commands", color=discord.Color.gold())
            embed.add_field(
                name="Commands",
                value=(
                    "> `/giveaway start` — Start a giveaway.\n"
                    "> `/giveaway end <msg_id>` — Force-end early.\n"
                    "> `/giveaway reroll <msg_id>` — Reroll winners.\n"
                    "> `/giveaway list` — Show active giveaways."
                ),
                inline=False,
            )

        elif sel == "Admin Config":
            embed = discord.Embed(title="⚙️ Admin Configuration", color=C_INFO)
            embed.add_field(
                name="Server Setup",
                value=(
                    "> `/setup` — Interactive setup wizard.\n"
                    "> `/config` — View all current settings.\n"
                    "> `/modules` — Enable/disable bot modules."
                ),
                inline=False,
            )
            embed.add_field(
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

        else:
            embed = discord.Embed(title="Error", description="Category not found.", color=discord.Color.red())

        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpSelect())


class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.start_time = time.time()
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

    async def cog_check(self, ctx):
        if hasattr(self.bot, "db"):
            is_bl = await self.bot.db.global_blacklist.find_one(
                {"target_id": ctx.author.id, "type": "user"}
            )
            if is_bl:
                try:
                    await ctx.send("❌ **Access Denied:** You are globally blacklisted.", ephemeral=True)
                except Exception:
                    pass
                return False
        return True

    # ─── autocomplete ─────────────────────────────────────────────────────────

    async def command_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cmds    = [c.name for c in self.bot.commands if not c.hidden]
        matches = [c for c in cmds if current.lower() in c.lower()]
        return [app_commands.Choice(name=m, value=m) for m in matches[:25]]

    # ─── /help ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="help",
        description="Shows help info and commands.",
        usage="/help [command]",
        help="/help roleinfo",
    )
    @app_commands.autocomplete(command=command_autocomplete)
    async def custom_help(self, ctx, command: str = None):
        if not command:
            embed = discord.Embed(
                title="Recluse Help Desk",
                description="Select a category below to browse commands.",
                color=C_INFO,
            )
            embed.add_field(name="🌐 Dashboard",    value="[Visit Dashboard](https://recluse-1.onrender.com/)",         inline=True)
            embed.add_field(name="📈 Uptime",       value="[Status Page](https://sszvcg5v.status.cron-job.org)",        inline=True)
            embed.add_field(name="📊 Total Modules",value="10 active modules",                                           inline=True)
            return await ctx.send(embed=embed, view=HelpView())

        cmd = self.bot.get_command(command)
        if not cmd:
            return await ctx.send(f"❌ Command `{command}` not found.", ephemeral=True)

        desc = f"**Command:** `/{cmd.name}`\n\n"
        desc += f"**Description:** {cmd.description or 'No description provided.'}\n"
        if getattr(cmd, "_buckets", None) and cmd._buckets._cooldown:
            desc += f"**Cooldown:** {int(cmd._buckets._cooldown.per)}s\n"
        desc += f"**Usage:** `{cmd.usage or f'/{cmd.name} {cmd.signature}'.strip()}`\n"
        desc += f"**Example:** `{cmd.help or f'/{cmd.name}'}`"

        embed = discord.Embed(description=desc, color=C_NEUTRAL)
        await ctx.send(embed=embed)

    # ─── /botinfo ─────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="botinfo",
        description="Retrieves application telemetry and metadata.",
        usage="/botinfo",
        help="/botinfo",
    )
    async def botinfo(self, ctx):
        await ctx.defer()
        app_info = await self.bot.application_info()
        ai_cog   = self.bot.get_cog("AI")
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

        embed = discord.Embed(title="⚙️ System Telemetry", color=C_INFO, timestamp=datetime.datetime.utcnow())
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="👑 Owner",           value=str(app_info.owner),                                        inline=True)
        embed.add_field(name="📡 WS Latency",      value=f"`{round(self.bot.latency * 1000)}ms`",                    inline=True)
        embed.add_field(name="🧠 Your AI",         value=active_ai,                                                  inline=True)
        embed.add_field(name="🟢 Status",          value="**Operational**",                                          inline=True)
        embed.add_field(name="⏱️ Uptime",          value=f"`{days}d {hours}h {mins}m {secs}s`",                      inline=True)
        embed.add_field(name="🏘️ Servers",         value=f"`{guild_count:,}` servers, `{member_count:,}` members",  inline=True)
        embed.add_field(name="🔧 Modules",         value="`10` loaded",                                              inline=True)
        embed.add_field(name="🐍 Library",         value=f"`discord.py {discord.__version__}`",                      inline=True)
        await ctx.send(embed=embed)

    # ─── /ping ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="ping",
        description="Network latency diagnostics.",
        usage="/ping",
        help="/ping",
    )
    async def ping(self, ctx):
        import time as _time
        t0  = _time.perf_counter()
        msg = await ctx.send("🏓 Measuring latency…")
        api = round((_time.perf_counter() - t0) * 1000)
        ws  = round(self.bot.latency * 1000)

        colour = discord.Color.green() if ws < 100 else discord.Color.yellow() if ws < 200 else discord.Color.red()
        bar_len = 10
        filled  = max(0, bar_len - int(ws / 30))
        bar     = "█" * filled + "░" * (bar_len - filled)

        embed = discord.Embed(title="🏓 Pong!", color=colour, timestamp=datetime.datetime.utcnow())
        embed.add_field(name="📡 Gateway WS", value=f"`{ws}ms`  `[{bar}]`", inline=False)
        embed.add_field(name="🌐 API Round-trip", value=f"`{api}ms`", inline=True)
        embed.set_footer(text="Recluse Network Diagnostics")
        await msg.edit(content=None, embed=embed)

    # ─── Heartbeat ────────────────────────────────────────────────────────────

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

    # ─── Status cycle ─────────────────────────────────────────────────────────

    @tasks.loop(seconds=20)
    async def cycle_bot_status(self):
        try:
            sports_cog     = self.bot.get_cog("Sports")
            active_matches = len(sports_cog.live_trackers) if sports_cog else 0
            gw_cog         = self.bot.get_cog("Giveaways")
            active_gws     = 0
            if gw_cog and hasattr(self.bot, "db"):
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


async def setup(bot):
    await bot.add_cog(Core(bot))
