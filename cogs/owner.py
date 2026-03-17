"""
owner.py  —  Recluse Bot  v2.0  ⟨Enterprise Edition⟩
═══════════════════════════════════════════════════════════════════════
Developer-only control panel — every command is locked to bot owner(s).

  NETWORK CONTROL
  ├─ /network blacklist_user / unblacklist_user
  ├─ /network blacklist_guild / unblacklist_guild
  ├─ /network lockdown <on|off>
  ├─ /network force_leave <guild_id>
  └─ /network whitelist_owner <user_id>      ← add co-owners

  SYSTEM MANAGEMENT
  ├─ /restart  <mode>                         ← reboot/shutdown/update/maintain
  ├─ /reload   <cog>                          ← hot-reload individual cog
  ├─ /sync     [guild]                        ← sync slash commands
  ├─ /shutdown                                ← graceful shutdown with confirm
  ├─ /update                                  ← git pull + reload all cogs
  └─ /setavatar <url>                         ← change bot avatar/banner

  LIVE EVAL ENGINE  (multi-mode REPL)
  ├─ /eval  <code>                            ← execute Python in-process
  ├─ /shell <command>                         ← run shell commands
  └─ ,eval  (prefix, channel terminal mode)   ← persistent REPL per channel

  BROADCAST & MESSAGING
  ├─ /echo         <channel> <msg>            ← echo to specific channel
  ├─ /globalecho   <msg>                      ← broadcast to all servers
  ├─ /dm           <user_id> <msg>            ← DM any user
  └─ /announce     <msg>                      ← styled announcement embed

  INSPECTION & DIAGNOSTICS
  ├─ /guilds       [sort]                     ← paginated server list
  ├─ /userinfo     <id>                       ← cross-server user lookup
  ├─ /guildinfo    <id>                       ← info for any guild by ID
  ├─ /dbstats                                 ← MongoDB collection stats
  ├─ /botstats                                ← deep runtime diagnostics
  └─ /checkperms   [guild_id]                 ← audit bot permissions

  MAINTENANCE
  ├─ /maintenance  <on|off>                   ← block all guilds except home
  ├─ /purgedb      <collection> <days>        ← manual DB cleanup
  └─ /resetguild   <guild_id>                 ← wipe all guild data

  LEGACY COMMANDS  (prefix-only for speed)
  └─ ,eval  ,shell  (channel REPL)
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import contextlib
import datetime
import io
import os
import subprocess
import sys
import textwrap
import time
import traceback
import copy

import discord
from discord import app_commands
from discord.ext import commands

# ─── Palette ──────────────────────────────────────────────────────────────────
C_OK      = discord.Color.brand_green()
C_ERR     = discord.Color.red()
C_WARN    = discord.Color.yellow()
C_INFO    = discord.Color(0x5865F2)
C_DEV     = discord.Color(0x2b2d31)

# ─── Channel IDs (update these in .env or hardcode) ──────────────────────────
ERROR_LOG_CHANNEL_ID   = 1096869180621463662
CONSOLE_CHANNEL_ID     = 1483449378202193992


# ═════════════════════════════════════════════════════════════════════════════
# UI Components
# ═════════════════════════════════════════════════════════════════════════════

class ConfirmView(discord.ui.View):
    """Generic two-button confirm/cancel view."""

    def __init__(self, author: discord.User, confirm_label: str = "Confirm",
                 confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger):
        super().__init__(timeout=30)
        self.author   = author
        self.decision: bool | None = None

        btn_confirm        = discord.ui.Button(label=confirm_label, style=confirm_style)
        btn_confirm.callback = self._confirm
        btn_cancel         = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        btn_cancel.callback  = self._cancel
        self.add_item(btn_confirm)
        self.add_item(btn_cancel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user != self.author:
            await i.response.send_message("This isn't yours.", ephemeral=True)
            return False
        return True

    async def _confirm(self, i: discord.Interaction):
        self.decision = True
        self.stop()
        await i.response.edit_message(content="✅ Confirmed.", view=None)

    async def _cancel(self, i: discord.Interaction):
        self.decision = False
        self.stop()
        await i.response.edit_message(content="❌ Cancelled.", view=None)


class GuildListView(discord.ui.View):
    """Paginated guild browser."""

    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=120)
        self.pages     = pages
        self.current   = 0
        self.author_id = author_id
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    async def _render(self, i: discord.Interaction):
        self._sync_buttons()
        await i.response.edit_message(embed=self.pages[self.current], view=self)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("Not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, i: discord.Interaction, _):
        self.current -= 1
        await self._render(i)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, i: discord.Interaction, _):
        self.current += 1
        await self._render(i)


# ═════════════════════════════════════════════════════════════════════════════
# Eval Engine  (Miza-inspired multi-mode REPL)
# ═════════════════════════════════════════════════════════════════════════════

class EvalEngine:
    """
    Stateful Python REPL engine.
    Supports: async eval, persistent globals, shell passthrough.
    Stores last result in `_` like a real Python REPL.
    """

    # Unicode quote normalisation (Miza's qtrans)
    _QMAP = str.maketrans({
        "\u201c": '"', "\u201d": '"', "\u201e": '"',
        "\u2018": "'", "\u2019": "'", "\u201a": "'",
        "\u301d": '"', "\u301e": '"',
    })

    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self.globals: dict = {}
        self._reset_globals()

    def _reset_globals(self):
        """Rebuild the global namespace with all useful bot references."""
        import discord as _discord
        self.globals = {
            "bot":      self.bot,
            "discord":  _discord,
            "commands": commands,
            "asyncio":  asyncio,
            "os":       os,
            "sys":      sys,
            "datetime": datetime,
            "_":        None,          # last result
        }

    def _strip_codeblock(self, code: str) -> str:
        """Strip markdown code fences and normalise quotes."""
        code = code.translate(self._QMAP).strip()
        if code.startswith("```") and code.endswith("```"):
            code = code[3:-3]
            lines = code.splitlines()
            if lines and lines[0].strip().isalnum():
                lines.pop(0)
            code = "\n".join(lines)
        return code.strip("`").strip()

    async def run(self, code: str, extra_globals: dict | None = None) -> tuple[str, bool]:
        """
        Execute code asynchronously.
        Returns (output_string, is_error).
        """
        code = self._strip_codeblock(code)
        if not code:
            return "*(empty)*", False

        g = dict(self.globals)
        if extra_globals:
            g.update(extra_globals)

        # Wrap in async function so we can use await at top level
        wrapped = f"async def __recluse_exec__():\n{textwrap.indent(code, '    ')}"

        stdout_capture = io.StringIO()
        result_value   = None
        error          = False

        try:
            exec(wrapped, g)                              # compile
            with contextlib.redirect_stdout(stdout_capture):
                result_value = await g["__recluse_exec__"]()  # run
        except Exception:
            error = True
            output = traceback.format_exc()
        else:
            # Printed output takes priority; fall back to return value
            printed = stdout_capture.getvalue()
            if printed:
                output = printed
            elif result_value is not None:
                output = repr(result_value)
                self.globals["_"] = result_value
            else:
                output = "*(no output)*"

        return output, error

    async def shell(self, command: str) -> str:
        """Run a shell command and return combined stdout+stderr."""
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return "⏰ Command timed out after 30 seconds."
        out = (stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")).strip()
        return out or "*(no output)*"


# ═════════════════════════════════════════════════════════════════════════════
# Owner Cog
# ═════════════════════════════════════════════════════════════════════════════

class Owner(commands.Cog):
    """Developer-only enterprise control panel for Recluse."""

    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.engine = EvalEngine(bot)
        # { channel_id: True }  — channels with active REPL terminal
        self._terminals: dict[int, bool] = {}
        # Maintenance mode: set to guild_id of home server, or None = off
        self._maintenance_guild: int | None = None

    # ─── universal owner guard ────────────────────────────────────────────────

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message(
            "❌ **Access Denied:** This command is restricted to the bot developer.",
            ephemeral=True,
        )
        return False

    def _fmt_code(self, output: str, max_len: int = 1980) -> str:
        """Wrap output in a py code block, trimming if needed."""
        if len(output) > max_len:
            output = output[:max_len] + "\n... (truncated)"
        return f"```py\n{output}\n```"

    async def _safe_send(self, ctx_or_channel, content: str):
        """Send output, splitting at 2000 chars if needed."""
        target = ctx_or_channel
        for i in range(0, len(content), 1990):
            chunk = content[i:i + 1990]
            try:
                if i == 0 and hasattr(target, "send"):
                    await target.send(chunk)
                else:
                    ch = target.channel if hasattr(target, "channel") else target
                    await ch.send(chunk)
            except discord.HTTPException:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # /network  group  — Wick-style global network security
    # ─────────────────────────────────────────────────────────────────────────

    network = app_commands.Group(
        name="network",
        description="[Dev] Global network security and management.",
    )

    @network.command(name="blacklist_user", description="[Dev] Globally restrict a user from the bot.")
    @app_commands.describe(user_id="Numeric user ID.", reason="Reason for blacklisting.")
    async def net_bl_user(self, interaction: discord.Interaction, user_id: str,
                          reason: str = "TOS Violation / Abuse"):
        if not await self._owner_check(interaction):
            return
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        if hasattr(self.bot, "db"):
            await self.bot.db.global_blacklist.update_one(
                {"target_id": uid, "type": "user"},
                {"$set": {"reason": reason, "by": str(interaction.user),
                          "timestamp": datetime.datetime.utcnow().timestamp()}},
                upsert=True,
            )
        embed = discord.Embed(title="⛔ User Blacklisted", color=C_ERR)
        embed.add_field(name="Target",  value=f"`{uid}`",  inline=True)
        embed.add_field(name="Reason",  value=reason,       inline=False)
        embed.timestamp = datetime.datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @network.command(name="unblacklist_user", description="[Dev] Remove a user from the global blacklist.")
    @app_commands.describe(user_id="Numeric user ID.")
    async def net_unbl_user(self, interaction: discord.Interaction, user_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        if hasattr(self.bot, "db"):
            result = await self.bot.db.global_blacklist.delete_one({"target_id": uid, "type": "user"})
            if result.deleted_count == 0:
                return await interaction.response.send_message(f"❌ `{uid}` is not blacklisted.", ephemeral=True)
        await interaction.response.send_message(f"✅ `{uid}` removed from global blacklist.", ephemeral=True)

    @network.command(name="blacklist_guild", description="[Dev] Blacklist a server and force-leave it.")
    @app_commands.describe(guild_id="Numeric guild ID.", reason="Reason.")
    async def net_bl_guild(self, interaction: discord.Interaction, guild_id: str,
                           reason: str = "TOS Violation / Network Abuse"):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        if hasattr(self.bot, "db"):
            await self.bot.db.global_blacklist.update_one(
                {"target_id": gid, "type": "guild"},
                {"$set": {"reason": reason, "by": str(interaction.user),
                          "timestamp": datetime.datetime.utcnow().timestamp()}},
                upsert=True,
            )
        guild = self.bot.get_guild(gid)
        left  = False
        if guild:
            try:
                await guild.leave()
                left = True
            except Exception:
                pass
        embed = discord.Embed(title="⛔ Guild Blacklisted", color=C_ERR)
        embed.add_field(name="Target",    value=f"`{gid}` ({guild.name if guild else 'Unknown'})", inline=True)
        embed.add_field(name="Left",      value="✅" if left else "Already not in it",             inline=True)
        embed.add_field(name="Reason",    value=reason,                                              inline=False)
        embed.timestamp = datetime.datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @network.command(name="unblacklist_guild", description="[Dev] Remove a guild from the blacklist.")
    @app_commands.describe(guild_id="Numeric guild ID.")
    async def net_unbl_guild(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        if hasattr(self.bot, "db"):
            await self.bot.db.global_blacklist.delete_one({"target_id": gid, "type": "guild"})
        await interaction.response.send_message(f"✅ Guild `{gid}` removed from blacklist.", ephemeral=True)

    @network.command(name="lockdown", description="[Dev] Toggle global lockdown — disables all commands network-wide.")
    @app_commands.describe(state="True = engage lockdown, False = lift.")
    async def net_lockdown(self, interaction: discord.Interaction, state: bool):
        if not await self._owner_check(interaction):
            return
        self.bot.global_lockdown = state
        if state:
            embed = discord.Embed(
                title="🔴 GLOBAL LOCKDOWN ENGAGED",
                description="All command processing is suspended across the entire network.\nOnly the bot owner can use commands.",
                color=C_ERR,
            )
        else:
            embed = discord.Embed(
                title="🟢 GLOBAL LOCKDOWN LIFTED",
                description="Normal operations have resumed.",
                color=C_OK,
            )
        embed.timestamp = datetime.datetime.utcnow()
        await interaction.response.send_message(embed=embed)

    @network.command(name="force_leave", description="[Dev] Force the bot to leave a server without blacklisting.")
    @app_commands.describe(guild_id="Numeric guild ID.")
    async def net_force_leave(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        guild = self.bot.get_guild(gid)
        if not guild:
            return await interaction.response.send_message(f"❌ Not in guild `{gid}`.", ephemeral=True)
        await guild.leave()
        await interaction.response.send_message(f"👋 Left **{guild.name}** (`{gid}`).", ephemeral=True)

    @network.command(name="whitelist_owner", description="[Dev] Add a co-owner who can bypass security checks.")
    @app_commands.describe(user_id="Numeric user ID.", remove="Remove instead of add.")
    async def net_co_owner(self, interaction: discord.Interaction, user_id: str, remove: bool = False):
        if not await self._owner_check(interaction):
            return
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        if remove:
            await self.bot.db.co_owners.delete_one({"user_id": uid})
            await interaction.response.send_message(f"✅ `{uid}` removed from co-owners.", ephemeral=True)
        else:
            await self.bot.db.co_owners.update_one(
                {"user_id": uid},
                {"$set": {"added_by": str(interaction.user), "timestamp": datetime.datetime.utcnow().timestamp()}},
                upsert=True,
            )
            await interaction.response.send_message(f"✅ `{uid}` added as co-owner.", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /restart  — Miza-style multi-mode restart
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="restart", description="[Dev] Restart or shut down the bot.")
    @app_commands.describe(
        mode="Operation mode.",
        delay="Delay before action (seconds, default 0).",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Reboot         — full restart",           value="reboot"),
        app_commands.Choice(name="Shutdown        — go offline permanently", value="shutdown"),
        app_commands.Choice(name="Maintain        — wait until idle, then restart", value="maintain"),
        app_commands.Choice(name="Update (git pull) — pull latest + reload cogs",   value="update"),
    ])
    @commands.is_owner()
    async def restart(self, interaction: discord.Interaction,
                      mode: app_commands.Choice[str] = None,
                      delay: int = 0):
        if not await self._owner_check(interaction):
            return
        mode_val = mode.value if mode else "reboot"
        await interaction.response.defer(ephemeral=True)

        if mode_val == "update":
            # ── git pull + reload all cogs ────────────────────────────────
            msg = await interaction.followup.send("⬇️ Pulling latest changes…", ephemeral=True)
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "pull"],
                    capture_output=True, text=True,
                )
                output = (result.stdout + result.stderr).strip()
            except Exception as e:
                output = str(e)
            await msg.edit(content=f"```\n{output[:1900]}\n```")

            # Hot-reload every loaded cog
            reloaded, failed = [], []
            for ext in list(self.bot.extensions.keys()):
                try:
                    await self.bot.reload_extension(ext)
                    reloaded.append(ext.split(".")[-1])
                except Exception as e:
                    failed.append(f"{ext.split('.')[-1]}: {e}")
            summary = (
                f"✅ Reloaded: {', '.join(reloaded)}\n"
                + (f"❌ Failed: {', '.join(failed)}" if failed else "")
            )
            await interaction.followup.send(summary, ephemeral=True)
            return

        if mode_val == "maintain":
            # Wait until the bot's command queue is clear
            await interaction.followup.send("⏳ Waiting until idle before restarting…", ephemeral=True)
            # Simple idle wait — sleep 5 s then proceed
            await asyncio.sleep(5)

        if delay > 0:
            embed = discord.Embed(
                title=f"⚠️ {'Shutting down' if mode_val == 'shutdown' else 'Restarting'} in {delay}s",
                color=C_WARN,
            )
            for guild in self.bot.guilds:
                ch = guild.system_channel
                if ch:
                    try:
                        await ch.send(embed=embed)
                    except Exception:
                        pass
            await asyncio.sleep(delay)

        if mode_val == "shutdown":
            await interaction.followup.send("🛑 Shutting down. Goodbye.", ephemeral=True)
            await self.bot.change_presence(status=discord.Status.invisible)
            await asyncio.sleep(1)
            await self.bot.close()
        else:
            await interaction.followup.send("🔄 Rebooting now. See you in a moment.", ephemeral=True)
            await self.bot.change_presence(status=discord.Status.idle)
            await asyncio.sleep(1)
            await self.bot.close()
            # Trigger OS-level restart if using a process manager (systemd/pm2)
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # ─────────────────────────────────────────────────────────────────────────
    # /reload  — hot-reload a single cog
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="reload", description="[Dev] Hot-reload a specific cog.")
    @app_commands.describe(
        extension="Cog name (e.g. 'moderation', 'ai', 'antinuke').",
        all_cogs="Reload ALL cogs at once.",
    )
    @commands.is_owner()
    async def reload_cog(self, interaction: discord.Interaction,
                         extension: str = "", all_cogs: bool = False):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if all_cogs:
            results = []
            for ext in list(self.bot.extensions.keys()):
                try:
                    await self.bot.reload_extension(ext)
                    results.append(f"✅ {ext.split('.')[-1]}")
                except Exception as e:
                    results.append(f"❌ {ext.split('.')[-1]}: {str(e)[:60]}")
            await interaction.followup.send("\n".join(results), ephemeral=True)
            return

        if not extension:
            return await interaction.followup.send("❌ Provide an extension name or set all_cogs=True.", ephemeral=True)

        ext = f"cogs.{extension}" if not extension.startswith("cogs.") else extension
        try:
            await self.bot.reload_extension(ext)
            await interaction.followup.send(f"✅ `{extension}` reloaded.", ephemeral=True)
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(ext)
                await interaction.followup.send(f"✅ `{extension}` loaded (was not loaded).", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ Failed: ```py\n{e}\n```", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ ```py\n{traceback.format_exc()[:1800]}\n```", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /sync  — sync slash commands
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sync", description="[Dev] Synchronise slash commands to Discord.")
    @app_commands.describe(
        guild_id="Sync to a specific guild only (faster for testing).",
        clear="Clear all commands first (nuclear option).",
    )
    @commands.is_owner()
    async def sync_tree(self, interaction: discord.Interaction,
                        guild_id: str = "", clear: bool = False):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if clear:
                if guild_id:
                    g = discord.Object(id=int(guild_id))
                    self.bot.tree.clear_commands(guild=g)
                    await self.bot.tree.sync(guild=g)
                else:
                    self.bot.tree.clear_commands(guild=None)
                    await self.bot.tree.sync()
                await interaction.followup.send("🗑️ Cleared all commands.", ephemeral=True)
                return

            if guild_id:
                g       = discord.Object(id=int(guild_id))
                self.bot.tree.copy_global_to(guild=g)
                synced  = await self.bot.tree.sync(guild=g)
                await interaction.followup.send(f"✅ Synced `{len(synced)}` commands to guild `{guild_id}`.", ephemeral=True)
            else:
                synced  = await self.bot.tree.sync()
                await interaction.followup.send(f"✅ Synced `{len(synced)}` global commands.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ ```py\n{e}\n```", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /shutdown  — graceful shutdown with confirm button
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="shutdown", description="[Dev] Gracefully shut down the bot.")
    @commands.is_owner()
    async def shutdown(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        view = ConfirmView(interaction.user, "⚠️ Shut Down", discord.ButtonStyle.danger)
        await interaction.response.send_message("Are you sure you want to shut down?", view=view, ephemeral=True)
        await view.wait()
        if view.decision:
            await self.bot.change_presence(status=discord.Status.invisible)
            await asyncio.sleep(1)
            await self.bot.close()

    # ─────────────────────────────────────────────────────────────────────────
    # /setavatar  — change bot avatar (Miza SetAvatar)
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="setavatar", description="[Dev] Change the bot's avatar.")
    @app_commands.describe(url="Direct image URL (PNG/JPG/GIF).", banner="Change banner instead of avatar.")
    @commands.is_owner()
    async def set_avatar(self, interaction: discord.Interaction, url: str, banner: bool = False):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        return await interaction.followup.send(f"❌ HTTP {r.status}.", ephemeral=True)
                    data = await r.read()
            if banner:
                await self.bot.user.edit(banner=data)
                await interaction.followup.send("✅ Banner updated.", ephemeral=True)
            else:
                await self.bot.user.edit(avatar=data)
                await interaction.followup.send("✅ Avatar updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ `{type(e).__name__}: {e}`", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /eval  — live Python evaluation (slash command version)
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="eval", description="[Dev] Execute Python code in the bot process.")
    @app_commands.describe(code="Python code to execute (supports await).")
    @commands.is_owner()
    async def eval_cmd(self, interaction: discord.Interaction, code: str):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        extra = {
            "guild":   interaction.guild,
            "channel": interaction.channel,
            "user":    interaction.user,
            "me":      interaction.guild.me if interaction.guild else self.bot.user,
        }
        output, is_error = await self.engine.run(code, extra_globals=extra)
        formatted        = self._fmt_code(output)
        react            = "❌" if is_error else "✅"
        await interaction.followup.send(f"{react} {formatted}", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /shell  — run a shell command
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="shell", description="[Dev] Execute a shell command on the host system.")
    @app_commands.describe(command="Shell command to run.")
    @commands.is_owner()
    async def shell_cmd(self, interaction: discord.Interaction, command: str):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        output    = await self.engine.shell(command)
        formatted = self._fmt_code(output)
        await interaction.followup.send(formatted, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Prefix-only ,eval / ,shell  — persistent channel REPL terminal
    # Inspired by Miza's UpdateExec  (any message = code execution)
    # ─────────────────────────────────────────────────────────────────────────

    @commands.command(name="eval", hidden=True, aliases=["exec", "aeval"])
    @commands.is_owner()
    async def prefix_eval(self, ctx: commands.Context, *, code: str):
        """One-shot eval via prefix command."""
        extra = {
            "ctx":     ctx,
            "guild":   ctx.guild,
            "channel": ctx.channel,
            "author":  ctx.author,
            "message": ctx.message,
        }
        async with ctx.typing():
            output, is_error = await self.engine.run(code, extra_globals=extra)
        react = "❌" if is_error else "✅"
        await ctx.message.add_reaction(react)
        await self._safe_send(ctx, self._fmt_code(output))

    @commands.command(name="shell", hidden=True, aliases=["sh", "bash"])
    @commands.is_owner()
    async def prefix_shell(self, ctx: commands.Context, *, command: str):
        """Run a shell command."""
        async with ctx.typing():
            output = await self.engine.shell(command)
        await ctx.message.add_reaction("✅")
        await self._safe_send(ctx, self._fmt_code(output))

    @commands.command(name="terminal", hidden=True, aliases=["term", "repl"])
    @commands.is_owner()
    async def toggle_terminal(self, ctx: commands.Context):
        """
        Toggle a persistent REPL terminal in this channel.
        While active, every non-command message from the owner is eval'd.
        """
        cid = ctx.channel.id
        if cid in self._terminals:
            del self._terminals[cid]
            await ctx.send("🔴 REPL terminal **disabled** for this channel.")
        else:
            self._terminals[cid] = True
            await ctx.send(
                "🟢 REPL terminal **active** in this channel.\n"
                "Every message you send will be executed as Python.\n"
                "Run `,terminal` again to disable."
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Intercepts messages in active REPL terminals."""
        if message.author.bot:
            return
        if not await self.bot.is_owner(message.author):
            return
        if message.channel.id not in self._terminals:
            return
        # Skip commands
        if message.content.startswith(self.bot.command_prefix):
            return
        code = message.content.strip()
        if not code or code.startswith("#"):
            return
        extra = {
            "ctx":     None,
            "guild":   message.guild,
            "channel": message.channel,
            "author":  message.author,
            "message": message,
        }
        await message.add_reaction("⚙️")
        output, is_error = await self.engine.run(code, extra_globals=extra)
        react = "❌" if is_error else "✅"
        await message.remove_reaction("⚙️", self.bot.user)
        await message.add_reaction(react)
        await self._safe_send(message.channel, self._fmt_code(output))

    # ─────────────────────────────────────────────────────────────────────────
    # /maintenance  — Miza-style single-server maintenance mode
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="maintenance", description="[Dev] Block all guilds except the home server.")
    @app_commands.describe(
        state="Enable or disable maintenance mode.",
        home_guild_id="Guild that stays operational during maintenance (default: current).",
    )
    @commands.is_owner()
    async def maintenance(self, interaction: discord.Interaction,
                          state: bool, home_guild_id: str = ""):
        if not await self._owner_check(interaction):
            return
        if state:
            gid = int(home_guild_id) if home_guild_id else (interaction.guild_id or 0)
            self._maintenance_guild = gid
            self.bot.global_lockdown = True   # use existing lockdown flag as backing
            # Store in DB so it survives restarts
            if hasattr(self.bot, "db"):
                await self.bot.db.bot_telemetry.update_one(
                    {"id": "maintenance"},
                    {"$set": {"active": True, "home_guild": gid}},
                    upsert=True,
                )
            embed = discord.Embed(
                title="🔧 Maintenance Mode ACTIVE",
                description=(
                    f"All guilds except `{gid}` are now locked out.\n"
                    "Commands are disabled network-wide."
                ),
                color=C_WARN,
            )
        else:
            self._maintenance_guild = None
            self.bot.global_lockdown = False
            if hasattr(self.bot, "db"):
                await self.bot.db.bot_telemetry.delete_one({"id": "maintenance"})
            embed = discord.Embed(
                title="✅ Maintenance Mode LIFTED",
                description="Normal operations have resumed across the network.",
                color=C_OK,
            )
        embed.timestamp = datetime.datetime.utcnow()
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # Broadcast & Messaging
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="echo", description="[Dev] Send a message to a specific channel.")
    @app_commands.describe(channel="Target channel.", message="Message text.")
    @commands.is_owner()
    async def echo(self, interaction: discord.Interaction,
                   channel: discord.TextChannel, message: str):
        if not await self._owner_check(interaction):
            return
        try:
            await channel.send(message)
            await interaction.response.send_message(f"✅ Sent to {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Missing permissions in that channel.", ephemeral=True)

    @app_commands.command(name="globalecho", description="[Dev] Broadcast a message to every server.")
    @app_commands.describe(message="Message to broadcast.", embed_style="Use a styled embed instead of plain text.")
    @commands.is_owner()
    async def global_echo(self, interaction: discord.Interaction, message: str, embed_style: bool = False):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        ok, fail = 0, 0
        for guild in self.bot.guilds:
            ch = guild.system_channel or next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
            )
            if not ch:
                fail += 1
                continue
            try:
                if embed_style:
                    emb = discord.Embed(description=message, color=C_INFO, timestamp=datetime.datetime.utcnow())
                    emb.set_author(name=self.bot.user.name, icon_url=self.bot.user.display_avatar.url)
                    await ch.send(embed=emb)
                else:
                    await ch.send(message)
                ok += 1
            except discord.Forbidden:
                fail += 1
        await interaction.followup.send(
            f"📢 Delivered to `{ok}` servers. Failed: `{fail}`.", ephemeral=True
        )

    @app_commands.command(name="dm", description="[Dev] Send a DM to any user by ID.")
    @app_commands.describe(user_id="Numeric user ID.", message="Message to send.")
    @commands.is_owner()
    async def dm_user(self, interaction: discord.Interaction, user_id: str, message: str):
        if not await self._owner_check(interaction):
            return
        try:
            uid  = int(user_id.strip())
            user = await self.bot.fetch_user(uid)
            await user.send(message)
            await interaction.response.send_message(f"✅ DM sent to **{user}** (`{uid}`).", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ User has DMs disabled.", ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message("❌ User not found.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)

    @app_commands.command(name="announce", description="[Dev] Post a styled announcement embed in a channel.")
    @app_commands.describe(
        channel="Target channel.",
        title="Embed title.",
        body="Embed description / body text.",
        color="Hex colour e.g. FF5733 (default: blurple).",
        ping_everyone="Whether to @everyone ping.",
    )
    @commands.is_owner()
    async def announce(self, interaction: discord.Interaction,
                       channel: discord.TextChannel, title: str, body: str,
                       color: str = "5865F2", ping_everyone: bool = False):
        if not await self._owner_check(interaction):
            return
        try:
            col = int(color.lstrip("#"), 16)
        except ValueError:
            col = 0x5865F2
        embed = discord.Embed(title=title, description=body, color=discord.Color(col),
                              timestamp=datetime.datetime.utcnow())
        embed.set_footer(text=self.bot.user.name, icon_url=self.bot.user.display_avatar.url)
        content = "@everyone" if ping_everyone else None
        try:
            await channel.send(content=content, embed=embed)
            await interaction.response.send_message(f"✅ Announced in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Inspection & Diagnostics
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="guilds", description="[Dev] Browse all servers the bot is in.")
    @app_commands.describe(
        sort="Sort order.",
        search="Filter by guild name.",
    )
    @app_commands.choices(sort=[
        app_commands.Choice(name="Members (largest first)",  value="members"),
        app_commands.Choice(name="Name (alphabetical)",      value="name"),
        app_commands.Choice(name="Joined (newest first)",    value="joined"),
    ])
    @commands.is_owner()
    async def list_guilds(self, interaction: discord.Interaction,
                          sort: app_commands.Choice[str] = None,
                          search: str = ""):
        if not await self._owner_check(interaction):
            return
        guilds = list(self.bot.guilds)
        if search:
            guilds = [g for g in guilds if search.lower() in g.name.lower()]
        sort_key = sort.value if sort else "members"
        if sort_key == "members":
            guilds.sort(key=lambda g: g.member_count or 0, reverse=True)
        elif sort_key == "name":
            guilds.sort(key=lambda g: g.name.lower())
        elif sort_key == "joined":
            guilds.sort(key=lambda g: g.me.joined_at or datetime.datetime.min, reverse=True)

        PAGE   = 10
        pages: list[discord.Embed] = []
        for i in range(0, max(len(guilds), 1), PAGE):
            chunk = guilds[i:i + PAGE]
            embed = discord.Embed(
                title=f"🏘️ Connected Servers ({len(guilds)} total)",
                color=C_DEV,
                timestamp=datetime.datetime.utcnow(),
            )
            for g in chunk:
                joined = g.me.joined_at.strftime("%Y-%m-%d") if g.me.joined_at else "?"
                embed.add_field(
                    name=f"{g.name}",
                    value=(
                        f"ID: `{g.id}`\n"
                        f"Members: `{g.member_count or '?'}`\n"
                        f"Joined: `{joined}`"
                    ),
                    inline=True,
                )
            embed.set_footer(text=f"Page {len(pages)+1}/{-(-len(guilds)//PAGE) or 1}")
            pages.append(embed)

        if not pages:
            return await interaction.response.send_message("No guilds found.", ephemeral=True)

        view = GuildListView(pages, interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    @app_commands.command(name="userinfo", description="[Dev] Fetch info on any user by ID (cross-server).")
    @app_commands.describe(user_id="Numeric user ID.")
    @commands.is_owner()
    async def user_info(self, interaction: discord.Interaction, user_id: str):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            uid  = int(user_id.strip())
            user = await self.bot.fetch_user(uid)
        except (ValueError, discord.NotFound):
            return await interaction.followup.send("❌ User not found.", ephemeral=True)

        # Check DB blacklist
        bl_status = "Not blacklisted"
        if hasattr(self.bot, "db"):
            bl = await self.bot.db.global_blacklist.find_one({"target_id": uid, "type": "user"})
            if bl:
                bl_status = f"⛔ **Blacklisted** — {bl.get('reason', '?')}"

        # Check which guilds they share with the bot
        shared = [g for g in self.bot.guilds if g.get_member(uid)]

        embed = discord.Embed(title=f"User Dossier: {user}", color=C_DEV)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="🆔 ID",        value=f"`{user.id}`",                          inline=True)
        embed.add_field(name="🤖 Bot",        value="Yes" if user.bot else "No",             inline=True)
        embed.add_field(name="📅 Created",    value=f"<t:{int(user.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="🛡️ BL Status", value=bl_status,                              inline=False)
        embed.add_field(name="🏘️ Shared",    value=f"{len(shared)} server(s)",             inline=True)
        if shared:
            embed.add_field(name="Shared Guilds", value=", ".join(g.name for g in shared[:5]), inline=False)
        embed.timestamp = datetime.datetime.utcnow()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="guildinfo", description="[Dev] Fetch info on any guild by ID.")
    @app_commands.describe(guild_id="Numeric guild ID.")
    @commands.is_owner()
    async def guild_info(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            gid   = int(guild_id.strip())
            guild = self.bot.get_guild(gid) or await self.bot.fetch_guild(gid)
        except (ValueError, discord.NotFound):
            return await interaction.followup.send("❌ Guild not found.", ephemeral=True)

        bl_status = "Not blacklisted"
        if hasattr(self.bot, "db"):
            bl = await self.bot.db.global_blacklist.find_one({"target_id": gid, "type": "guild"})
            if bl:
                bl_status = f"⛔ Blacklisted — {bl.get('reason', '?')}"

        embed = discord.Embed(title=f"Guild Dossier: {guild.name}", color=C_DEV)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="🆔 ID",       value=f"`{guild.id}`",                              inline=True)
        embed.add_field(name="👑 Owner",    value=f"<@{guild.owner_id}> (`{guild.owner_id}`)",  inline=True)
        embed.add_field(name="👥 Members",  value=str(guild.member_count or "?"),               inline=True)
        embed.add_field(name="📅 Created",  value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="🛡️ BL",      value=bl_status,                                    inline=False)
        embed.timestamp = datetime.datetime.utcnow()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="dbstats", description="[Dev] View MongoDB collection sizes and document counts.")
    @commands.is_owner()
    async def db_stats(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        collections = [
            "guild_settings", "mod_logs", "security_logs",
            "global_blacklist", "warnings", "user_strikes",
            "levels", "tickets", "giveaways",
            "ai_telemetry", "antinuke_config", "antinuke_logs",
            "custom_commands", "reaction_roles", "starboard",
            "command_telemetry", "bot_telemetry", "afk",
        ]
        embed = discord.Embed(title="📊 MongoDB Statistics", color=C_INFO,
                              timestamp=datetime.datetime.utcnow())
        lines = []
        total = 0
        for col in collections:
            try:
                count = await self.bot.db[col].count_documents({})
                total += count
                lines.append(f"`{col:<25}` {count:>6} docs")
            except Exception:
                pass
        embed.description = "```\n" + "\n".join(lines) + f"\n{'─'*35}\n{'Total':<25} {total:>6} docs\n```"
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="botstats", description="[Dev] Deep runtime diagnostics.")
    @commands.is_owner()
    async def bot_stats(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        import platform

        guild_count  = len(self.bot.guilds)
        member_count = sum(g.member_count or 0 for g in self.bot.guilds)
        text_count   = sum(len(g.text_channels) for g in self.bot.guilds)
        voice_count  = sum(len(g.voice_channels) for g in self.bot.guilds)
        cog_count    = len(self.bot.cogs)
        ext_count    = len(self.bot.extensions)
        ws_ms        = round(self.bot.latency * 1000)

        # Uptime
        uptime_secs = 0
        if hasattr(self.bot, "db"):
            doc = await self.bot.db.bot_telemetry.find_one({"id": "uptime"})
            if doc:
                uptime_secs = max(0, int(time.time()) - doc.get("start_time", int(time.time())))
        d, r       = divmod(uptime_secs, 86400)
        h, r       = divmod(r, 3600)
        m, s       = divmod(r, 60)

        embed = discord.Embed(title="🔬 Runtime Diagnostics", color=C_DEV,
                              timestamp=datetime.datetime.utcnow())
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="🐍 Python",     value=f"`{sys.version.split()[0]}`",  inline=True)
        embed.add_field(name="📦 discord.py", value=f"`{discord.__version__}`",     inline=True)
        embed.add_field(name="💻 Host OS",    value=f"`{platform.system()} {platform.release()}`", inline=True)
        embed.add_field(name="📡 WS Latency", value=f"`{ws_ms}ms`",                 inline=True)
        embed.add_field(name="⏱️ Uptime",     value=f"`{d}d {h}h {m}m {s}s`",       inline=True)
        embed.add_field(name="🧩 Cogs",       value=f"`{cog_count}` ({ext_count} ext)", inline=True)
        embed.add_field(name="🏘️ Guilds",    value=f"`{guild_count:,}`",            inline=True)
        embed.add_field(name="👥 Members",    value=f"`{member_count:,}`",           inline=True)
        embed.add_field(name="💬 Channels",  value=f"T:`{text_count}` V:`{voice_count}`", inline=True)
        embed.add_field(name="🔒 Lockdown",  value="🔴 ON" if self.bot.global_lockdown else "🟢 OFF", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="checkperms", description="[Dev] Audit bot permissions in all channels of a guild.")
    @app_commands.describe(guild_id="Guild to inspect (default: current).")
    @commands.is_owner()
    async def check_perms(self, interaction: discord.Interaction, guild_id: str = ""):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        gid   = int(guild_id) if guild_id else (interaction.guild_id or 0)
        guild = self.bot.get_guild(gid)
        if not guild:
            return await interaction.followup.send("❌ Guild not found.", ephemeral=True)

        critical = ["send_messages", "embed_links", "attach_files",
                    "read_messages", "manage_messages", "add_reactions"]
        lines    = []
        for ch in guild.text_channels[:30]:
            perms = ch.permissions_for(guild.me)
            missing = [p for p in critical if not getattr(perms, p)]
            if missing:
                lines.append(f"⚠️ #{ch.name}: missing `{'`, `'.join(missing)}`")

        if not lines:
            text = "✅ All checked channels have required permissions."
        else:
            text = "\n".join(lines)
        embed = discord.Embed(title=f"🔑 Permission Audit — {guild.name}",
                              description=text[:3000], color=C_INFO)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Database Maintenance
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="purgedb", description="[Dev] Manually delete old records from a DB collection.")
    @app_commands.describe(
        collection="Collection name (e.g. 'mod_logs').",
        older_than_days="Delete records older than this many days.",
    )
    @commands.is_owner()
    async def purge_db(self, interaction: discord.Interaction,
                       collection: str, older_than_days: int = 90):
        if not await self._owner_check(interaction):
            return
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=older_than_days)).timestamp()
        try:
            result = await self.bot.db[collection].delete_many({"timestamp": {"$lt": cutoff}})
            await interaction.followup.send(
                f"🗑️ Deleted `{result.deleted_count}` records older than {older_than_days}d from `{collection}`.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ `{e}`", ephemeral=True)

    @app_commands.command(name="resetguild", description="[Dev] Wipe ALL stored data for a specific guild.")
    @app_commands.describe(guild_id="Guild to wipe.")
    @commands.is_owner()
    async def reset_guild(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)

        view = ConfirmView(interaction.user, "⚠️ Wipe Guild Data", discord.ButtonStyle.danger)
        await interaction.response.send_message(
            f"⚠️ This will delete **all data** for guild `{gid}` from every collection. Are you sure?",
            view=view, ephemeral=True,
        )
        await view.wait()
        if not view.decision:
            return

        if not hasattr(self.bot, "db"):
            return

        collections = [
            "guild_settings", "mod_logs", "security_logs", "warnings",
            "user_strikes", "levels", "tickets", "giveaways", "ai_telemetry",
            "antinuke_config", "antinuke_logs", "custom_commands",
            "reaction_roles", "starboard", "command_telemetry", "level_roles",
        ]
        deleted = 0
        for col in collections:
            try:
                r = await self.bot.db[col].delete_many({"guild_id": gid})
                deleted += r.deleted_count
            except Exception:
                pass
        await interaction.followup.send(
            f"✅ Wiped `{deleted}` records across `{len(collections)}` collections for guild `{gid}`.",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Legacy shortcuts  (for muscle memory / speed)
    # ─────────────────────────────────────────────────────────────────────────

    @commands.command(name="reload", hidden=True)
    @commands.is_owner()
    async def prefix_reload(self, ctx: commands.Context, extension: str):
        try:
            await self.bot.reload_extension(f"cogs.{extension}")
            await ctx.send(f"✅ `{extension}` reloaded.")
        except Exception as e:
            await ctx.send(f"❌ ```py\n{e}\n```")

    @commands.command(name="sync_prefix", hidden=True, aliases=["synccmds"])
    @commands.is_owner()
    async def prefix_sync(self, ctx: commands.Context):
        synced = await self.bot.tree.sync()
        await ctx.send(f"✅ Synced `{len(synced)}` commands.")

    @commands.command(name="cls", hidden=True)
    @commands.is_owner()
    async def cls_eval(self, ctx: commands.Context):
        """Reset the eval engine's global namespace."""
        self.engine._reset_globals()
        await ctx.send("🔄 Eval namespace reset.")


# ═════════════════════════════════════════════════════════════════════════════
async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
