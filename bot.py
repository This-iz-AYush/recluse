"""
bot.py  —  Recluse  v2.0
Main bot class with global security, anti-spam middleware, and error handling.
"""

import collections
import datetime
import os
import time
import traceback
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

ERROR_LOG_CHANNEL_ID   = 1096869180621463662
CONSOLE_CHANNEL_ID     = 1188082818656510032

# ─── Anti-spam message tracker ───────────────────────────────────────────────
# { (guild_id, user_id): deque of timestamps }
_spam_tracker: dict[tuple, collections.deque] = {}


class Recluse(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(command_prefix=",", intents=intents)
        self.launch_time    = datetime.datetime.utcnow()
        self.global_lockdown = False
        self.remove_command("help")

    # ─────────────────────────────────────────────────────────────────────────
    async def setup_hook(self):
        self.tree.on_error           = self.on_app_command_error
        self.tree.interaction_check  = self.global_interaction_check
        self.add_check(self.global_prefix_check)

        # Load all cogs  (database must load first)
        cog_order = ["database", "admin", "core", "moderation", "misc",
                     "ai", "anime", "sports", "leveling", "tickets",
                     "giveaways", "owner"]
        loaded = []
        for name in cog_order:
            path = f"cogs/{name}.py"
            if os.path.exists(path):
                try:
                    await self.load_extension(f"cogs.{name}")
                    loaded.append(name)
                    print(f"  ✅ {name}")
                except Exception as e:
                    print(f"  ❌ {name}: {e}")

        # Also pick up any cogs not in the priority list
        for fn in os.listdir("./cogs"):
            if fn.endswith(".py") and not fn.startswith("_"):
                ext = fn[:-3]
                if ext not in loaded:
                    try:
                        await self.load_extension(f"cogs.{ext}")
                        print(f"  ✅ {ext}")
                    except Exception as e:
                        print(f"  ❌ {ext}: {e}")

        await self.tree.sync()
        print("✅ Slash commands synced.")

    async def on_ready(self):
        print(f"\n🟢 Recluse v2.0 online  —  {self.user}  ({self.user.id})")
        print(f"   Guilds: {len(self.guilds)}   Users: {sum(g.member_count for g in self.guilds if g.member_count):,}")

    # ─────────────────────────────────────────────────────────────────────────
    # Global security engine
    # ─────────────────────────────────────────────────────────────────────────

    async def _core_security_check(
        self, user_id: int, guild_id: int | None = None, is_owner: bool = False
    ) -> tuple[bool, str]:
        if self.global_lockdown and not is_owner:
            return False, "🛑 **Network Lockdown:** Recluse is under maintenance. Commands are disabled."

        if hasattr(self, "db"):
            user_ban = await self.db.global_blacklist.find_one({"target_id": user_id, "type": "user"})
            if user_ban:
                return False, (
                    f"⛔ **Blacklisted:** You have been globally restricted from Recluse.\n"
                    f"**Reason:** {user_ban.get('reason', 'TOS Violation')}"
                )
            if guild_id:
                guild_ban = await self.db.global_blacklist.find_one({"target_id": guild_id, "type": "guild"})
                if guild_ban:
                    return False, "⛔ **Server Blacklisted:** This server is restricted. Leaving."
        return True, ""

    async def _dashboard_module_check(self, guild_id: int, cog_name: str | None, cmd_name: str) -> tuple[bool, str]:
        """Fetches the dashboard settings and checks if a module or command is disabled."""
        if not hasattr(self, "db"):
            return True, ""

        settings = await self.db.guild_settings.find_one({"guild_id": guild_id})
        if not settings:
            return True, ""

        disabled_cogs = settings.get("disabled_cogs", [])
        disabled_cmds = settings.get("disabled_cmds", [])

        if cog_name and cog_name in disabled_cogs:
            return False, f"❌ The `{cog_name}` module is disabled in this server."

        if cmd_name in disabled_cmds:
            return False, f"❌ The `/{cmd_name}` command is disabled in this server."

        return True, ""

    async def global_interaction_check(self, interaction: discord.Interaction) -> bool:
        is_owner = await self.is_owner(interaction.user)
        guild_id = interaction.guild_id
        
        # 1. Global Bans & Lockdown Check
        passed, msg = await self._core_security_check(interaction.user.id, guild_id, is_owner)
        if not passed:
            if "Leaving" in msg and interaction.guild:
                try: await interaction.response.send_message(msg, ephemeral=True)
                except Exception: pass
                await interaction.guild.leave()
            else:
                try: await interaction.response.send_message(msg, ephemeral=True)
                except Exception: pass
            return False
            
        # 2. Dashboard Module Toggle Check (Slash Commands)
        if guild_id and interaction.command:
            # Safely extract the cog name from the slash command binding
            cog_name = getattr(interaction.command.binding, "qualified_name", type(interaction.command.binding).__name__) if interaction.command.binding else None
            cmd_name = interaction.command.name
            
            allowed, block_msg = await self._dashboard_module_check(guild_id, cog_name, cmd_name)
            if not allowed:
                try: await interaction.response.send_message(block_msg, ephemeral=True)
                except Exception: pass
                return False

        return True

    async def global_prefix_check(self, ctx: commands.Context) -> bool:
        is_owner = await self.is_owner(ctx.author)
        guild_id = ctx.guild.id if ctx.guild else None
        
        # 1. Global Bans & Lockdown Check
        passed, msg = await self._core_security_check(ctx.author.id, guild_id, is_owner)
        if not passed:
            if "Leaving" in msg and ctx.guild:
                try: await ctx.send(msg)
                except Exception: pass
                await ctx.guild.leave()
            else:
                await ctx.send(msg)
            return False
            
        # 2. Dashboard Module Toggle Check (Prefix Commands)
        if guild_id and ctx.command:
            cog_name = ctx.cog.qualified_name if ctx.cog else None
            cmd_name = ctx.command.qualified_name
            
            allowed, block_msg = await self._dashboard_module_check(guild_id, cog_name, cmd_name)
            if not allowed:
                await ctx.send(block_msg, delete_after=5)
                return False

        return True
    # ─────────────────────────────────────────────────────────────────────────
    # Anti-spam middleware  (runs on every message before cogs see it)
    # ─────────────────────────────────────────────────────────────────────────

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            await self.process_commands(message)
            return

        # Retrieve guild config
        cfg: dict = {}
        if hasattr(self, "db"):
            doc = await self.db.guild_settings.find_one({"guild_id": message.guild.id})
            cfg = doc or {}

        if cfg.get("automod_enabled"):
            # ── Banned-word check ────────────────────────────────────────────
            banned = cfg.get("banned_words", [])
            content_low = message.content.lower()
            if any(w in content_low for w in banned):
                try:
                    await message.delete()
                    warn = await message.channel.send(
                        f"⚠️ {message.author.mention} — that content is not permitted.",
                        delete_after=5,
                    )
                except discord.Forbidden:
                    pass
                if hasattr(self, "db"):
                    await self.db.security_logs.insert_one({
                        "guild_id":  message.guild.id,
                        "user_id":   message.author.id,
                        "action":    "Automod — Banned Word",
                        "content":   message.content[:500],
                        "timestamp": datetime.datetime.utcnow().timestamp(),
                    })
                    await self.db.user_strikes.update_one(
                        {"guild_id": message.guild.id, "user_id": message.author.id},
                        {"$inc": {"strikes": 1},
                         "$set": {"last_strike": datetime.datetime.utcnow().timestamp()}},
                        upsert=True,
                    )
                return  # don't process commands on banned content

            # ── Spam check ───────────────────────────────────────────────────
            threshold = cfg.get("antispam_threshold", 5)
            window    = cfg.get("antispam_window",    5)
            action    = cfg.get("antispam_action",    "mute")
            mute_mins = cfg.get("antispam_mute_mins", 10)

            key = (message.guild.id, message.author.id)
            now = time.time()
            dq  = _spam_tracker.setdefault(key, collections.deque())

            # Remove timestamps outside the window
            while dq and now - dq[0] > window:
                dq.popleft()
            dq.append(now)

            if len(dq) >= threshold:
                dq.clear()
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass

                member = message.guild.get_member(message.author.id)
                if member:
                    try:
                        if action == "mute":
                            until = discord.utils.utcnow() + datetime.timedelta(minutes=mute_mins)
                            await member.timeout(until, reason="Anti-spam trigger")
                            await message.channel.send(
                                f"🛑 {member.mention} has been muted for `{mute_mins}m` (spam).",
                                delete_after=10,
                            )
                        elif action == "kick":
                            await member.kick(reason="Anti-spam trigger")
                        elif action == "ban":
                            await member.ban(reason="Anti-spam trigger", delete_message_days=1)
                        elif action == "warn":
                            await message.channel.send(
                                f"⚠️ {member.mention} — slow down! (spam warning)", delete_after=10
                            )
                    except discord.Forbidden:
                        pass

                if hasattr(self, "db"):
                    await self.db.security_logs.insert_one({
                        "guild_id":  message.guild.id,
                        "user_id":   message.author.id,
                        "action":    f"Automod — Spam ({action})",
                        "timestamp": datetime.datetime.utcnow().timestamp(),
                    })
                return

        await self.process_commands(message)

    # ─────────────────────────────────────────────────────────────────────────
    # Guild events
    # ─────────────────────────────────────────────────────────────────────────

    async def on_guild_join(self, guild: discord.Guild):
        if hasattr(self, "db"):
            bl = await self.db.global_blacklist.find_one({"target_id": guild.id, "type": "guild"})
            if bl:
                ch = guild.system_channel or next(
                    (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
                )
                if ch:
                    try:
                        await ch.send(
                            f"⛔ This server is blacklisted from the Recluse network.\n"
                            f"**Reason:** {bl.get('reason', 'TOS Violation')}\nLeaving now."
                        )
                    except discord.Forbidden:
                        pass
                await guild.leave()
                return

        # Log join to console channel
        console = self.get_channel(CONSOLE_CHANNEL_ID)
        if console:
            embed = discord.Embed(
                title="➕ Joined Server",
                color=discord.Color.green(),
                timestamp=datetime.datetime.utcnow(),
            )
            embed.add_field(name="Name",    value=guild.name,              inline=True)
            embed.add_field(name="ID",      value=str(guild.id),           inline=True)
            embed.add_field(name="Members", value=str(guild.member_count), inline=True)
            try:
                await console.send(embed=embed)
            except Exception:
                pass

    async def on_guild_remove(self, guild: discord.Guild):
        console = self.get_channel(CONSOLE_CHANNEL_ID)
        if console:
            embed = discord.Embed(
                title="➖ Left Server",
                color=discord.Color.red(),
                timestamp=datetime.datetime.utcnow(),
            )
            embed.add_field(name="Name", value=guild.name, inline=True)
            embed.add_field(name="ID",   value=str(guild.id), inline=True)
            try:
                await console.send(embed=embed)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Error logging
    # ─────────────────────────────────────────────────────────────────────────

    async def log_system_error(self, ctx_or_msg, error, is_command=True):
        channel = self.get_channel(ERROR_LOG_CHANNEL_ID)
        if not channel:
            return
        embed = discord.Embed(
            title="⚠️ System Exception",
            color=discord.Color.red(),
            timestamp=datetime.datetime.utcnow(),
        )
        if is_command:
            embed.add_field(name="Command",  value=f"`{getattr(getattr(ctx_or_msg, 'command', None), 'name', '?')}`", inline=True)
            embed.add_field(name="Invoker",  value=f"{ctx_or_msg.author} (`{ctx_or_msg.author.id}`)",               inline=True)
        else:
            embed.add_field(name="Source",   value="`on_message`", inline=True)
            embed.add_field(name="User",     value=f"{ctx_or_msg.author} (`{ctx_or_msg.author.id}`)", inline=True)
        embed.add_field(name="Guild",        value=getattr(getattr(ctx_or_msg, "guild", None), "name", "DMs"), inline=True)
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        embed.add_field(name="Traceback", value=f"```py\n{tb[:1000]}\n```", inline=False)
        try:
            await channel.send(embed=embed)
        except Exception:
            print(f"CRITICAL — failed to log error:\n{tb}")

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.NotOwner):
            return await ctx.send("❌ Developer-only command.", ephemeral=True)
        if isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(f"⏳ Cooldown: retry in `{error.retry_after:.1f}s`.", ephemeral=True)
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"❌ Missing `{error.param.name}`. Use `/help {ctx.command.name}`.", ephemeral=True)
        if isinstance(error, commands.BadArgument):
            return await ctx.send("❌ Invalid argument. Check your input.", ephemeral=True)
        if isinstance(error, commands.MissingPermissions):
            return await ctx.send("❌ You lack permissions for this command.", ephemeral=True)
        if isinstance(error, commands.BotMissingPermissions):
            return await ctx.send("❌ I'm missing required permissions.", ephemeral=True)
        await self.log_system_error(ctx, error)
        try:
            await ctx.send("❌ An unexpected error occurred. It has been logged.", ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ):
        if isinstance(error, discord.app_commands.CommandInvokeError):
            error = error.original
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            return await _safe_reply(interaction, f"⏳ Cooldown: retry in `{error.retry_after:.1f}s`.")
        if isinstance(error, discord.app_commands.MissingPermissions):
            return await _safe_reply(interaction, "❌ You lack the required permissions.")
        if isinstance(error, discord.app_commands.BotMissingPermissions):
            return await _safe_reply(interaction, "❌ I'm missing required permissions.")
        if isinstance(error, discord.app_commands.CheckFailure):
            return

        class _MockCtx:
            def __init__(self, i):
                self.command = i.command
                self.author  = i.user
                self.guild   = i.guild

        await self.log_system_error(_MockCtx(interaction), error)
        await _safe_reply(interaction, "❌ An unexpected error occurred. It has been logged.")


async def _safe_reply(interaction: discord.Interaction, msg: str):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    load_dotenv()
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if TOKEN:
        bot = Recluse()
        bot.run(TOKEN)
    else:
        print("❌ DISCORD_BOT_TOKEN is not set in .env")
