"""
giveaways.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
GiveawayBot-style giveaway system.

Features
  • /giveaway start  — start a timed giveaway with optional requirements
  • /giveaway end    — force-end early
  • /giveaway reroll — pick a new winner
  • /giveaway list   — show all active giveaways
  • Button-based entry (not reaction-based — more reliable)
  • Multiple winners support
  • Role requirement filtering
  • Automatic winner announcement on expiry
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import datetime
import random
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks


def _parse_duration(s: str) -> int | None:
    """Parse '1d2h30m' → total seconds."""
    match = re.match(
        r"((?P<d>\d+)d)?((?P<h>\d+)h)?((?P<m>\d+)m)?((?P<s>\d+)s)?",
        s.strip(),
    )
    if not match or not any(match.groups()):
        return None
    d, h, m, sec = (int(match.group(k) or 0) for k in ("d", "h", "m", "s"))
    return d * 86400 + h * 3600 + m * 60 + sec


class GiveawayEntryView(discord.ui.View):
    """Persistent view on each giveaway message."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.success, custom_id="gw:enter")
    async def enter(self, interaction: discord.Interaction, _btn):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return

        doc = await self.bot.db.giveaways.find_one(
            {"guild_id": interaction.guild.id, "message_id": interaction.message.id, "active": True}
        )
        if not doc:
            return await interaction.response.send_message(
                "❌ This giveaway has already ended.", ephemeral=True
            )

        uid = interaction.user.id

        # Role requirement check
        req_role_id = doc.get("required_role")
        if req_role_id:
            req_role = interaction.guild.get_role(req_role_id)
            if req_role and req_role not in interaction.user.roles:
                return await interaction.response.send_message(
                    f"❌ You need the {req_role.mention} role to enter.", ephemeral=True
                )

        if uid in doc.get("entries", []):
            # Un-enter
            await self.bot.db.giveaways.update_one(
                {"message_id": interaction.message.id},
                {"$pull": {"entries": uid}},
            )
            count = len(doc.get("entries", [])) - 1
            await interaction.response.send_message("✅ You've left the giveaway.", ephemeral=True)
        else:
            await self.bot.db.giveaways.update_one(
                {"message_id": interaction.message.id},
                {"$addToSet": {"entries": uid}},
            )
            count = len(doc.get("entries", [])) + 1
            await interaction.response.send_message("✅ You've entered the giveaway! Good luck 🍀", ephemeral=True)

        # Update the embed entry count
        try:
            embed = interaction.message.embeds[0]
            for i, field in enumerate(embed.fields):
                if "Entries" in field.name:
                    embed.set_field_at(i, name="🎟️ Entries", value=str(count), inline=True)
                    break
            await interaction.message.edit(embed=embed)
        except Exception:
            pass


class Giveaways(commands.Cog):
    """Giveaway system for Recluse."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(GiveawayEntryView(bot))
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    # ─── helpers ─────────────────────────────────────────────────────────────

    async def _end_giveaway(self, doc: dict, guild: discord.Guild | None = None):
        """Pick winners and post the announcement."""
        if guild is None:
            guild = self.bot.get_guild(doc["guild_id"])
        if not guild:
            return

        channel = guild.get_channel(doc["channel_id"])
        entries = doc.get("entries", [])
        winners_count = doc.get("winners", 1)

        # Filter out members who have left
        valid = [uid for uid in entries if guild.get_member(uid)]
        winners = random.sample(valid, min(winners_count, len(valid))) if valid else []

        embed = discord.Embed(
            title=f"🎉 {doc['prize']}",
            color=discord.Color.gold(),
            timestamp=datetime.datetime.utcnow(),
        )
        if winners:
            w_mentions = " ".join(f"<@{uid}>" for uid in winners)
            embed.description = f"**Winners:** {w_mentions}\n\nCongratulations! 🥳"
        else:
            embed.description = "😔 No valid entries — no winner this time."
        embed.add_field(name="🎟️ Entries",     value=str(len(valid)),      inline=True)
        embed.add_field(name="🏆 Winners",      value=str(winners_count),   inline=True)
        embed.add_field(name="Hosted by",       value=f"<@{doc['host_id']}>", inline=True)
        embed.set_footer(text="Giveaway ended")

        # Edit the original message
        if channel:
            try:
                msg = await channel.fetch_message(doc["message_id"])
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
            if winners:
                w_mention_str = " ".join(f"<@{uid}>" for uid in winners)
                await channel.send(
                    f"🎉 Congratulations {w_mention_str}! You won **{doc['prize']}**!"
                )

        # Mark as inactive
        if hasattr(self.bot, "db"):
            await self.bot.db.giveaways.update_one(
                {"_id": doc["_id"]},
                {"$set": {"active": False, "final_winners": winners}},
            )

    @tasks.loop(seconds=15)
    async def check_giveaways(self):
        if not hasattr(self.bot, "db"):
            return
        now = datetime.datetime.utcnow().timestamp()
        cursor = self.bot.db.giveaways.find({"active": True, "ends_at": {"$lte": now}})
        async for doc in cursor:
            try:
                await self._end_giveaway(doc)
            except Exception as e:
                print(f"[Giveaways] Error ending giveaway: {e}")

    @check_giveaways.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # /giveaway  group
    # ─────────────────────────────────────────────────────────────────────────

    giveaway_group = app_commands.Group(name="giveaway", description="Giveaway management commands.")

    @giveaway_group.command(name="start", description="Start a new giveaway in this channel.")
    @app_commands.describe(
        prize="What you're giving away.",
        duration="Duration e.g. 1d, 12h, 30m.",
        winners="Number of winners (default 1).",
        required_role="Role required to enter.",
        channel="Channel to host the giveaway in (default: current).",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gw_start(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: int = 1,
        required_role: discord.Role | None = None,
        channel: discord.TextChannel | None = None,
    ):
        if not interaction.guild:
            return
        secs = _parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                "❌ Invalid duration. Use format like `1d`, `12h`, `30m`.", ephemeral=True
            )
        winners = max(1, min(winners, 20))
        target_ch = channel or interaction.channel
        ends_at   = datetime.datetime.utcnow() + datetime.timedelta(seconds=secs)

        embed = discord.Embed(
            title=f"🎉 {prize}",
            color=discord.Color.gold(),
            timestamp=ends_at,
        )
        embed.description = (
            f"React with 🎉 or click below to enter!\n\n"
            f"**Ends:** <t:{int(ends_at.timestamp())}:R>"
        )
        embed.add_field(name="🏆 Winners",  value=str(winners),                      inline=True)
        embed.add_field(name="🎟️ Entries",  value="0",                               inline=True)
        embed.add_field(name="Hosted by",   value=interaction.user.mention,          inline=True)
        if required_role:
            embed.add_field(name="Requirement", value=required_role.mention,         inline=True)
        embed.set_footer(text="Ends at")

        view = GiveawayEntryView(self.bot)
        msg  = await target_ch.send(embed=embed, view=view)

        if hasattr(self.bot, "db"):
            doc = {
                "guild_id":      interaction.guild.id,
                "channel_id":    target_ch.id,
                "message_id":    msg.id,
                "prize":         prize,
                "winners":       winners,
                "host_id":       interaction.user.id,
                "entries":       [],
                "active":        True,
                "ends_at":       ends_at.timestamp(),
                "required_role": required_role.id if required_role else None,
            }
            await self.bot.db.giveaways.insert_one(doc)

        await interaction.response.send_message(
            f"✅ Giveaway started in {target_ch.mention}!", ephemeral=True
        )

    @giveaway_group.command(name="end", description="Force-end a giveaway early.")
    @app_commands.describe(message_id="Message ID of the giveaway.")
    @app_commands.default_permissions(manage_guild=True)
    async def gw_end(self, interaction: discord.Interaction, message_id: str):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        doc = await self.bot.db.giveaways.find_one(
            {"guild_id": interaction.guild.id, "message_id": mid, "active": True}
        )
        if not doc:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)

        await self._end_giveaway(doc, interaction.guild)
        await interaction.response.send_message("✅ Giveaway ended.", ephemeral=True)

    @giveaway_group.command(name="reroll", description="Reroll winners for an ended giveaway.")
    @app_commands.describe(message_id="Message ID of the giveaway.")
    @app_commands.default_permissions(manage_guild=True)
    async def gw_reroll(self, interaction: discord.Interaction, message_id: str):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        doc = await self.bot.db.giveaways.find_one(
            {"guild_id": interaction.guild.id, "message_id": mid}
        )
        if not doc:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)

        entries = [uid for uid in doc.get("entries", []) if interaction.guild.get_member(uid)]
        if not entries:
            return await interaction.response.send_message("❌ No valid entries to reroll from.", ephemeral=True)

        winners = random.sample(entries, min(doc.get("winners", 1), len(entries)))
        w_str   = " ".join(f"<@{uid}>" for uid in winners)
        await interaction.response.send_message(
            f"🎉 New winner(s): {w_str}! Congratulations!", ephemeral=False
        )

    @giveaway_group.command(name="list", description="List all active giveaways in this server.")
    async def gw_list(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)

        docs = await self.bot.db.giveaways.find(
            {"guild_id": interaction.guild.id, "active": True}
        ).to_list(25)

        if not docs:
            return await interaction.response.send_message("No active giveaways.", ephemeral=True)

        embed = discord.Embed(
            title="🎉 Active Giveaways",
            color=discord.Color.gold(),
            timestamp=datetime.datetime.utcnow(),
        )
        for d in docs:
            ch = interaction.guild.get_channel(d["channel_id"])
            embed.add_field(
                name=d["prize"],
                value=(
                    f"Channel: {ch.mention if ch else '?'}\n"
                    f"Ends: <t:{int(d['ends_at'])}:R>\n"
                    f"Entries: {len(d.get('entries', []))}"
                ),
                inline=True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))
