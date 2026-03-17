"""
autoresponse.py  —  Recluse Bot  v2.0
Miza-style auto-response / keyword trigger system.

Features:
  • Per-guild keyword triggers with custom responses
  • Wildcards: {user} {server} {count} in responses
  • Response types: text | embed | react | DM
  • Match modes: exact | contains | startswith | regex
  • Cooldown per trigger to prevent spam
  • Up to 50 triggers per server
  • Admin commands: /autoresponse add / remove / list / test
"""

import datetime
import re
import time

import discord
from discord import app_commands
from discord.ext import commands


class AutoResponse(commands.Cog):
    """Keyword-triggered auto-response system."""

    def __init__(self, bot: commands.Bot):
        self.bot       = bot
        # In-memory cooldown tracker: { (guild_id, trigger_id): last_triggered }
        self._cooldowns: dict[tuple, float] = {}

    async def _get_triggers(self, guild_id: int) -> list[dict]:
        if not hasattr(self.bot, "db"):
            return []
        return await self.bot.db.auto_responses.find(
            {"guild_id": guild_id}
        ).to_list(50)

    def _matches(self, trigger: dict, content: str) -> bool:
        keyword = trigger.get("keyword", "")
        mode    = trigger.get("match_mode", "contains")
        if not trigger.get("case_sensitive", False):
            content = content.lower()
            keyword = keyword.lower()
        if mode == "exact":
            return content == keyword
        elif mode == "startswith":
            return content.startswith(keyword)
        elif mode == "endswith":
            return content.endswith(keyword)
        elif mode == "regex":
            try:
                flags = 0 if trigger.get("case_sensitive") else re.IGNORECASE
                return bool(re.search(keyword, content, flags))
            except re.error:
                return False
        else:  # contains (default)
            return keyword in content

    def _resolve_response(self, text: str, message: discord.Message) -> str:
        return (
            text
            .replace("{user}",    message.author.mention)
            .replace("{name}",    message.author.display_name)
            .replace("{server}",  message.guild.name if message.guild else "DM")
            .replace("{channel}", message.channel.mention)
            .replace("{count}",   str(message.guild.member_count if message.guild else 0))
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Listener
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not hasattr(self.bot, "db"):
            return

        # Module enabled check
        s = await self.bot.db.guild_settings.find_one({"guild_id": message.guild.id})
        if s and not s.get("autoresponse_enabled", True):
            return

        triggers = await self._get_triggers(message.guild.id)
        content  = message.content

        for trigger in triggers:
            if not trigger.get("enabled", True):
                continue
            if not self._matches(trigger, content):
                continue

            # Cooldown check
            tid       = str(trigger.get("_id", ""))
            cd_key    = (message.guild.id, tid)
            cooldown  = trigger.get("cooldown", 5)
            now       = time.monotonic()
            last      = self._cooldowns.get(cd_key, 0)
            if now - last < cooldown:
                continue
            self._cooldowns[cd_key] = now

            # Build response
            response_text = self._resolve_response(
                trigger.get("response", ""), message
            )
            resp_type     = trigger.get("response_type", "text")

            try:
                if resp_type == "react":
                    await message.add_reaction(response_text.strip())
                elif resp_type == "dm":
                    try:
                        await message.author.send(response_text)
                    except discord.Forbidden:
                        pass
                elif resp_type == "embed":
                    embed = discord.Embed(
                        description=response_text,
                        color=discord.Color(0x5865F2),
                    )
                    await message.channel.send(embed=embed)
                else:  # text
                    await message.channel.send(response_text)
            except discord.Forbidden:
                pass

            if trigger.get("delete_trigger", False):
                try:
                    await message.delete()
                except Exception:
                    pass
            break  # Only fire first matching trigger

    # ─────────────────────────────────────────────────────────────────────────
    # Admin commands
    # ─────────────────────────────────────────────────────────────────────────

    ar_group = app_commands.Group(
        name="autoresponse",
        description="Manage keyword auto-response triggers.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @ar_group.command(name="add", description="Add a keyword trigger.")
    @app_commands.describe(
        keyword="The word or phrase to match.",
        response="What the bot should reply. Use {user}, {server}, {channel}.",
        match_mode="How to match the keyword.",
        response_type="How to respond.",
        cooldown="Seconds between responses (default 5).",
        case_sensitive="Whether matching is case-sensitive.",
        delete_trigger="Delete the triggering message.",
    )
    @app_commands.choices(
        match_mode=[
            app_commands.Choice(name="Contains (default)", value="contains"),
            app_commands.Choice(name="Exact match",        value="exact"),
            app_commands.Choice(name="Starts with",        value="startswith"),
            app_commands.Choice(name="Ends with",          value="endswith"),
            app_commands.Choice(name="Regex",              value="regex"),
        ],
        response_type=[
            app_commands.Choice(name="Text (default)", value="text"),
            app_commands.Choice(name="Embed",          value="embed"),
            app_commands.Choice(name="React (emoji)",  value="react"),
            app_commands.Choice(name="DM the user",    value="dm"),
        ],
    )
    @app_commands.default_permissions(manage_guild=True)
    async def ar_add(
        self,
        interaction: discord.Interaction,
        keyword: str,
        response: str,
        match_mode: app_commands.Choice[str] = None,
        response_type: app_commands.Choice[str] = None,
        cooldown: int = 5,
        case_sensitive: bool = False,
        delete_trigger: bool = False,
    ):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)

        count = await self.bot.db.auto_responses.count_documents({"guild_id": interaction.guild.id})
        if count >= 50:
            return await interaction.response.send_message(
                "❌ You've reached the 50 trigger limit for this server.", ephemeral=True
            )

        # Validate regex
        if match_mode and match_mode.value == "regex":
            try:
                re.compile(keyword)
            except re.error as e:
                return await interaction.response.send_message(
                    f"❌ Invalid regex pattern: `{e}`", ephemeral=True
                )

        doc = {
            "guild_id":      interaction.guild.id,
            "keyword":       keyword,
            "response":      response,
            "match_mode":    match_mode.value if match_mode else "contains",
            "response_type": response_type.value if response_type else "text",
            "cooldown":      max(1, min(cooldown, 3600)),
            "case_sensitive": case_sensitive,
            "delete_trigger": delete_trigger,
            "enabled":       True,
            "created_by":    interaction.user.id,
            "created_at":    datetime.datetime.utcnow().timestamp(),
        }
        await self.bot.db.auto_responses.insert_one(doc)
        embed = discord.Embed(title="✅ Auto-Response Added", color=discord.Color.green())
        embed.add_field(name="Keyword",   value=f"`{keyword}`",                                     inline=True)
        embed.add_field(name="Match",     value=doc["match_mode"],                                  inline=True)
        embed.add_field(name="Type",      value=doc["response_type"],                               inline=True)
        embed.add_field(name="Response",  value=response[:200],                                    inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ar_group.command(name="remove", description="Remove a keyword trigger.")
    @app_commands.describe(keyword="The keyword to remove.")
    @app_commands.default_permissions(manage_guild=True)
    async def ar_remove(self, interaction: discord.Interaction, keyword: str):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        result = await self.bot.db.auto_responses.delete_one(
            {"guild_id": interaction.guild.id, "keyword": keyword}
        )
        if result.deleted_count:
            await interaction.response.send_message(f"✅ Trigger `{keyword}` removed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ No trigger found for `{keyword}`.", ephemeral=True)

    @ar_group.command(name="list", description="List all auto-response triggers for this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def ar_list(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        triggers = await self._get_triggers(interaction.guild.id)
        if not triggers:
            return await interaction.response.send_message("No triggers configured.", ephemeral=True)
        embed = discord.Embed(
            title=f"⚡ Auto-Responses ({len(triggers)}/50)",
            color=discord.Color(0x5865F2),
        )
        for t in triggers[:20]:
            embed.add_field(
                name=f"`{t['keyword'][:40]}`",
                value=(
                    f"Mode: `{t.get('match_mode','contains')}` | "
                    f"Type: `{t.get('response_type','text')}` | "
                    f"CD: `{t.get('cooldown',5)}s`"
                ),
                inline=False,
            )
        if len(triggers) > 20:
            embed.set_footer(text=f"Showing 20/{len(triggers)} triggers")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ar_group.command(name="test", description="Test a keyword trigger manually.")
    @app_commands.describe(keyword="The keyword to test.")
    @app_commands.default_permissions(manage_guild=True)
    async def ar_test(self, interaction: discord.Interaction, keyword: str):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        triggers = await self._get_triggers(interaction.guild.id)
        for t in triggers:
            if t.get("keyword", "").lower() == keyword.lower():
                response = self._resolve_response(t.get("response", ""), interaction.message or interaction)
                embed    = discord.Embed(
                    title="✅ Trigger Found",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Keyword",  value=f"`{t['keyword']}`", inline=True)
                embed.add_field(name="Mode",     value=t.get("match_mode"),  inline=True)
                embed.add_field(name="Type",     value=t.get("response_type"), inline=True)
                embed.add_field(name="Response Preview", value=response[:400], inline=False)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        await interaction.response.send_message(f"❌ No trigger found for `{keyword}`.", ephemeral=True)

    @ar_group.command(name="toggle", description="Enable or disable a trigger.")
    @app_commands.describe(keyword="Keyword to toggle.", enabled="Enable or disable.")
    @app_commands.default_permissions(manage_guild=True)
    async def ar_toggle(self, interaction: discord.Interaction, keyword: str, enabled: bool):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        result = await self.bot.db.auto_responses.update_one(
            {"guild_id": interaction.guild.id, "keyword": keyword},
            {"$set": {"enabled": enabled}},
        )
        if result.matched_count:
            status = "enabled" if enabled else "disabled"
            await interaction.response.send_message(
                f"{'✅' if enabled else '❌'} Trigger `{keyword}` {status}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(f"❌ Trigger `{keyword}` not found.", ephemeral=True)

    @ar_group.command(name="clear", description="Remove ALL auto-response triggers for this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def ar_clear(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        result = await self.bot.db.auto_responses.delete_many({"guild_id": interaction.guild.id})
        await interaction.response.send_message(
            f"🗑️ Removed **{result.deleted_count}** auto-response trigger(s).", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoResponse(bot))
