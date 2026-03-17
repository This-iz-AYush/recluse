"""
reminders.py  —  Recluse Bot  v2.0
Miza-inspired reminder system.

Features:
  • /remind set  — Set a reminder (DM or channel)
  • /remind list — View your pending reminders
  • /remind delete — Cancel a reminder
  • Duration parser: 1d2h30m15s
  • Max 25 reminders per user
  • Persists to MongoDB, survives restarts
  • Auto-DM when due (with jump link)
  • Recurring reminders (optional)
"""

import asyncio
import datetime
import re
import time

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks


_DURATION_RE = re.compile(
    r"((?P<d>\d+)\s*d(?:ays?)?)?\s*"
    r"((?P<h>\d+)\s*h(?:ours?)?)?\s*"
    r"((?P<m>\d+)\s*m(?:in(?:utes?)?)?)?\s*"
    r"((?P<s>\d+)\s*s(?:econds?)?)?",
    re.IGNORECASE,
)


def parse_duration(s: str) -> int | None:
    """Parse a duration string to seconds. Returns None if invalid."""
    match = _DURATION_RE.match(s.strip())
    if not match or not any(match.groups()):
        return None
    d  = int(match.group("d") or 0)
    h  = int(match.group("h") or 0)
    m  = int(match.group("m") or 0)
    sc = int(match.group("s") or 0)
    total = d * 86400 + h * 3600 + m * 60 + sc
    return total if total > 0 else None


def format_duration(seconds: int) -> str:
    """Convert seconds to a human-readable duration string."""
    d, r  = divmod(seconds, 86400)
    h, r  = divmod(r, 3600)
    m, s  = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    return " ".join(parts) if parts else "0s"


class Reminders(commands.Cog):
    """Persistent reminder system with DM delivery."""

    def __init__(self, bot: commands.Bot):
        self.bot            = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    # ─────────────────────────────────────────────────────────────────────────
    # Background task — checks every 30 seconds
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def check_reminders(self):
        if not hasattr(self.bot, "db"):
            return
        now   = datetime.datetime.utcnow().timestamp()
        due   = await self.bot.db.reminders.find(
            {"due_at": {"$lte": now}, "fired": False}
        ).to_list(100)

        for doc in due:
            await self._fire_reminder(doc)

    @check_reminders.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def _fire_reminder(self, doc: dict):
        """Send the reminder and mark it as fired."""
        user_id   = doc.get("user_id")
        message   = doc.get("message", "Reminder!")
        jump_url  = doc.get("jump_url", "")
        guild_id  = doc.get("guild_id")
        channel_id = doc.get("channel_id")
        dm_mode   = doc.get("dm", True)
        created   = doc.get("created_at", 0)
        due       = doc.get("due_at", 0)

        embed = discord.Embed(
            title="⏰ Reminder!",
            description=message[:1024],
            color=discord.Color(0xF1C40F),
            timestamp=datetime.datetime.utcfromtimestamp(due),
        )
        embed.add_field(
            name="⏱️ Set",
            value=f"<t:{int(created)}:R>",
            inline=True,
        )
        if jump_url:
            embed.add_field(name="📍 Context", value=f"[Jump to message]({jump_url})", inline=True)
        embed.set_footer(text="Recluse Reminder System")

        sent = False
        user = await self.bot.fetch_user(user_id) if user_id else None

        if dm_mode and user:
            try:
                await user.send(embed=embed)
                sent = True
            except discord.Forbidden:
                pass

        if not sent and channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(f"<@{user_id}>", embed=embed)
                    sent = True
                except discord.Forbidden:
                    pass

        # Handle recurring reminders
        recur = doc.get("recur_seconds")
        if recur and recur > 0:
            next_due = due + recur
            await self.bot.db.reminders.update_one(
                {"_id": doc["_id"]},
                {"$set": {"due_at": next_due, "fired": False}},
            )
        else:
            await self.bot.db.reminders.update_one(
                {"_id": doc["_id"]},
                {"$set": {"fired": True}},
            )

    # ─────────────────────────────────────────────────────────────────────────
    # /remind group
    # ─────────────────────────────────────────────────────────────────────────

    remind_group = app_commands.Group(
        name="remind",
        description="Set and manage personal reminders.",
    )

    @remind_group.command(name="set", description="Set a reminder.")
    @app_commands.describe(
        duration='When to remind you e.g. "1h30m", "2d", "45m".',
        message="What to remind you about.",
        dm="Send reminder as a DM (default). If False, reminds in this channel.",
        repeat="Repeat every this interval (same format as duration). Leave blank for one-time.",
    )
    async def remind_set(
        self,
        interaction: discord.Interaction,
        duration: str,
        message: str,
        dm: bool = True,
        repeat: str = "",
    ):
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)

        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                "❌ Invalid duration. Try `1h30m`, `2d`, `45m`.", ephemeral=True
            )
        if secs < 30:
            return await interaction.response.send_message(
                "❌ Minimum reminder time is 30 seconds.", ephemeral=True
            )
        if secs > 365 * 86400:
            return await interaction.response.send_message(
                "❌ Maximum reminder time is 1 year.", ephemeral=True
            )

        # Check limit
        count = await self.bot.db.reminders.count_documents(
            {"user_id": interaction.user.id, "fired": False}
        )
        if count >= 25:
            return await interaction.response.send_message(
                "❌ You already have 25 pending reminders. Delete some with `/remind delete`.",
                ephemeral=True,
            )

        recur_secs = None
        if repeat:
            recur_secs = parse_duration(repeat)
            if not recur_secs:
                return await interaction.response.send_message(
                    "❌ Invalid repeat duration.", ephemeral=True
                )

        now    = datetime.datetime.utcnow().timestamp()
        due_at = now + secs

        doc = {
            "user_id":     interaction.user.id,
            "guild_id":    interaction.guild_id,
            "channel_id":  interaction.channel_id,
            "message":     message,
            "dm":          dm,
            "due_at":      due_at,
            "created_at":  now,
            "fired":       False,
            "recur_seconds": recur_secs,
            "jump_url":    "",
        }
        await self.bot.db.reminders.insert_one(doc)

        desc = (
            f"⏰ I'll remind you in **{format_duration(secs)}**.\n"
            f"📨 Delivery: {'DM' if dm else 'This channel'}\n"
            f"📝 Message: {message[:200]}"
        )
        if recur_secs:
            desc += f"\n🔁 Repeating every **{format_duration(recur_secs)}**"

        embed = discord.Embed(
            title="✅ Reminder Set",
            description=desc,
            color=discord.Color(0xF1C40F),
        )
        embed.add_field(name="⏱️ Due", value=f"<t:{int(due_at)}:R>  (<t:{int(due_at)}:f>)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @remind_group.command(name="list", description="View all your pending reminders.")
    async def remind_list(self, interaction: discord.Interaction):
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        docs = await self.bot.db.reminders.find(
            {"user_id": interaction.user.id, "fired": False}
        ).sort("due_at", 1).to_list(25)

        if not docs:
            return await interaction.response.send_message(
                "📭 You have no pending reminders.", ephemeral=True
            )

        embed = discord.Embed(
            title=f"⏰ Your Reminders ({len(docs)}/25)",
            color=discord.Color(0xF1C40F),
        )
        for i, doc in enumerate(docs, 1):
            due    = int(doc.get("due_at", 0))
            msg    = doc.get("message", "")[:60]
            recur  = doc.get("recur_seconds")
            label  = f"#{i}"
            value  = f"`{msg}`\n<t:{due}:R>  (<t:{due}:f>)"
            if recur:
                value += f"\n🔁 Every {format_duration(recur)}"
            embed.add_field(name=label, value=value, inline=True)
        embed.set_footer(text="Use /remind delete <number> to cancel one.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @remind_group.command(name="delete", description="Cancel a pending reminder.")
    @app_commands.describe(number="The reminder number from /remind list.")
    async def remind_delete(self, interaction: discord.Interaction, number: int):
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        docs = await self.bot.db.reminders.find(
            {"user_id": interaction.user.id, "fired": False}
        ).sort("due_at", 1).to_list(25)

        if number < 1 or number > len(docs):
            return await interaction.response.send_message(
                f"❌ Invalid number. You have {len(docs)} pending reminder(s).", ephemeral=True
            )
        doc = docs[number - 1]
        await self.bot.db.reminders.delete_one({"_id": doc["_id"]})
        await interaction.response.send_message(
            f"✅ Reminder #{number} cancelled: *{doc.get('message','')[:80]}*", ephemeral=True
        )

    @remind_group.command(name="clear", description="Cancel ALL your pending reminders.")
    async def remind_clear(self, interaction: discord.Interaction):
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        result = await self.bot.db.reminders.delete_many(
            {"user_id": interaction.user.id, "fired": False}
        )
        await interaction.response.send_message(
            f"🗑️ Cancelled **{result.deleted_count}** reminder(s).", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
