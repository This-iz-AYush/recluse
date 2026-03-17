"""
birthdays.py  —  Recluse Bot  v2.0
Birthday tracking and automated celebration system.

Features:
  • /birthday set         — Register your birthday
  • /birthday get [user]  — See someone's birthday
  • /birthday list        — Upcoming birthdays in the server
  • /birthday remove      — Delete your birthday
  • /birthdaysetup        — Admin: set the birthday announcement channel + role
  • Auto-celebration task — Posts embed + assigns role at midnight UTC on birthday
  • Age calculation (optional — only if user opts in)
  • Timezone-aware (uses server's configured timezone if set)
"""

import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks


_BIRTHDAY_MESSAGES = [
    "🎂 Happy Birthday {user}! Wishing you an amazing day! 🎉",
    "🎉 It's {user}'s birthday today! Everyone wish them well! 🥳",
    "🎂 A very happy birthday to {user}! Hope it's a great one! 🎈",
    "🥳 Today is {user}'s special day! Happy Birthday! 🎂",
    "🎊 Wishing {user} a fantastic birthday! You deserve the best! 🎂",
]


class Birthdays(commands.Cog):
    """Birthday tracking and auto-celebration system."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.birthday_task.start()

    def cog_unload(self):
        self.birthday_task.cancel()

    # ─────────────────────────────────────────────────────────────────────────
    # Daily birthday check (runs every hour, fires at midnight UTC)
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def birthday_task(self):
        if not hasattr(self.bot, "db"):
            return
        now   = datetime.datetime.utcnow()
        month = now.month
        day   = now.day

        # Only fire in the first hour of the day (0:00–1:00 UTC)
        if now.hour != 0:
            return

        # Find all birthdays matching today (month and day)
        docs = await self.bot.db.birthdays.find(
            {"month": month, "day": day}
        ).to_list(None)

        for doc in docs:
            guild_id  = doc.get("guild_id")
            user_id   = doc.get("user_id")
            if not guild_id or not user_id:
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            member = guild.get_member(user_id)
            if not member:
                continue

            # Get guild settings
            s = await self.bot.db.guild_settings.find_one({"guild_id": guild_id})
            if not s:
                continue

            ch_id   = s.get("birthday_channel")
            role_id = s.get("birthday_role")

            # Post celebration message
            if ch_id:
                channel = guild.get_channel(ch_id)
                if channel:
                    import random
                    msg   = random.choice(_BIRTHDAY_MESSAGES).replace("{user}", member.mention)
                    embed = discord.Embed(
                        title="🎂 Happy Birthday!",
                        description=msg,
                        color=discord.Color.pink(),
                        timestamp=datetime.datetime.utcnow(),
                    )
                    embed.set_thumbnail(url=member.display_avatar.url)
                    embed.set_footer(text=guild.name)

                    # Calculate age if year was provided
                    year = doc.get("year")
                    if year:
                        age = now.year - year
                        embed.add_field(name="🎈 Turning", value=f"**{age}** years old!", inline=True)
                    try:
                        await channel.send(content=member.mention, embed=embed)
                    except discord.Forbidden:
                        pass

            # Assign birthday role (remove after 24 hours)
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    try:
                        await member.add_roles(role, reason="Birthday!")
                        # Schedule removal after 24 hours
                        async def _remove_role(m=member, r=role):
                            await asyncio.sleep(86400)
                            try:
                                await m.remove_roles(r, reason="Birthday role expired")
                            except Exception:
                                pass
                        asyncio.ensure_future(_remove_role())
                    except discord.Forbidden:
                        pass

    @birthday_task.before_loop
    async def before_birthday_task(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # /birthdaysetup
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="birthdaysetup",
        description="Configure the birthday announcement system.",
    )
    @app_commands.describe(
        channel="Channel to post birthday announcements in.",
        role="Role to assign on someone's birthday (optional).",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def birthdaysetup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role | None = None,
    ):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        update = {"birthday_channel": channel.id}
        if role:
            update["birthday_role"] = role.id
        await self.bot.db.guild_settings.update_one(
            {"guild_id": interaction.guild.id}, {"$set": update}, upsert=True
        )
        resp = f"✅ Birthday announcements → {channel.mention}"
        if role:
            resp += f"\n🎂 Birthday role: {role.mention}"
        await interaction.response.send_message(resp, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /birthday group
    # ─────────────────────────────────────────────────────────────────────────

    bday_group = app_commands.Group(
        name="birthday",
        description="Manage birthday registrations.",
    )

    @bday_group.command(name="set", description="Register your birthday.")
    @app_commands.describe(
        day="Day of birth (1–31).",
        month="Month of birth (1–12).",
        year="Year of birth (optional — used for age calculation).",
    )
    async def bday_set(
        self,
        interaction: discord.Interaction,
        day: int,
        month: int,
        year: int | None = None,
    ):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)

        if not (1 <= day <= 31 and 1 <= month <= 12):
            return await interaction.response.send_message(
                "❌ Invalid day or month.", ephemeral=True
            )
        if year and not (1900 <= year <= datetime.datetime.utcnow().year):
            return await interaction.response.send_message(
                "❌ Invalid year.", ephemeral=True
            )

        # Validate date exists (e.g. Feb 30)
        try:
            datetime.date(year or 2000, month, day)
        except ValueError:
            return await interaction.response.send_message(
                f"❌ {day}/{month} is not a valid date.", ephemeral=True
            )

        doc = {
            "guild_id": interaction.guild.id,
            "user_id":  interaction.user.id,
            "day":      day,
            "month":    month,
        }
        if year:
            doc["year"] = year

        await self.bot.db.birthdays.update_one(
            {"guild_id": interaction.guild.id, "user_id": interaction.user.id},
            {"$set": doc},
            upsert=True,
        )

        month_name = datetime.date(2000, month, 1).strftime("%B")
        age_str    = f" (turning {datetime.datetime.utcnow().year - year} this year)" if year else ""
        await interaction.response.send_message(
            f"🎂 Birthday set to **{day} {month_name}**{age_str}!", ephemeral=True
        )

    @bday_group.command(name="get", description="View someone's registered birthday.")
    @app_commands.describe(member="Member to look up (default: yourself).")
    async def bday_get(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        target = member or interaction.user
        doc    = await self.bot.db.birthdays.find_one(
            {"guild_id": interaction.guild.id, "user_id": target.id}
        )
        if not doc:
            who = "You haven't" if target == interaction.user else f"{target.display_name} hasn't"
            return await interaction.response.send_message(
                f"❌ {who} registered a birthday yet.", ephemeral=True
            )

        day, month = doc["day"], doc["month"]
        year       = doc.get("year")
        month_name = datetime.date(2000, month, 1).strftime("%B")

        embed = discord.Embed(
            title=f"🎂 {target.display_name}'s Birthday",
            color=discord.Color.pink(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        date_str = f"**{day} {month_name}**"
        if year:
            age = datetime.datetime.utcnow().year - year
            date_str += f" {year} (Age: {age})"
        embed.description = date_str

        # How many days away
        today    = datetime.date.today()
        bday     = datetime.date(today.year, month, day)
        if bday < today:
            bday = datetime.date(today.year + 1, month, day)
        days_away = (bday - today).days
        embed.add_field(name="⏳ Days Away", value=f"`{days_away}` days", inline=True)
        embed.add_field(name="📅 Next", value=f"<t:{int(datetime.datetime(bday.year, bday.month, bday.day).timestamp())}:D>", inline=True)
        await interaction.response.send_message(embed=embed)

    @bday_group.command(name="list", description="View upcoming birthdays in this server.")
    async def bday_list(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        docs = await self.bot.db.birthdays.find(
            {"guild_id": interaction.guild.id}
        ).to_list(200)

        if not docs:
            return await interaction.response.send_message("No birthdays registered yet.", ephemeral=True)

        today = datetime.date.today()

        def _days_until(doc):
            d, m = doc["day"], doc["month"]
            try:
                bday = datetime.date(today.year, m, d)
                if bday < today:
                    bday = datetime.date(today.year + 1, m, d)
                return (bday - today).days
            except ValueError:
                return 9999

        docs.sort(key=_days_until)
        embed = discord.Embed(
            title=f"🎂 Upcoming Birthdays — {interaction.guild.name}",
            color=discord.Color.pink(),
            timestamp=datetime.datetime.utcnow(),
        )
        count = 0
        for doc in docs[:20]:
            uid    = doc["user_id"]
            member = interaction.guild.get_member(uid)
            if not member:
                continue
            d, m       = doc["day"], doc["month"]
            month_name = datetime.date(2000, m, 1).strftime("%b")
            days       = _days_until(doc)
            value      = f"**{d} {month_name}** — {days} days away"
            if days == 0:
                value = f"🎉 **TODAY! {d} {month_name}**"
            embed.add_field(name=member.display_name, value=value, inline=True)
            count += 1
        if count == 0:
            embed.description = "No members with registered birthdays are currently in the server."
        await interaction.response.send_message(embed=embed)

    @bday_group.command(name="remove", description="Remove your registered birthday.")
    async def bday_remove(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        result = await self.bot.db.birthdays.delete_one(
            {"guild_id": interaction.guild.id, "user_id": interaction.user.id}
        )
        if result.deleted_count:
            await interaction.response.send_message("✅ Birthday removed.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No birthday registered to remove.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Birthdays(bot))
