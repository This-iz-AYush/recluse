"""
leveling.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
MEE6/Carl-bot style XP & Ranking system — the #1 server-growth driver.

Features
  • Per-guild XP tracking with configurable rewards & cooldowns
  • Smooth level-up formula  (xp_needed = 5 × lvl² + 50 × lvl + 100)
  • Level-role rewards (configured via /levelrole in admin.py)
  • /rank  — personal rank card (text-based, no Pillow dependency)
  • /leaderboard  — paginated top-10 embed
  • /givexp, /setlevel, /resetxp  — admin overrides
  • XP multiplier per server
  • Bonus XP for voice activity (optional, if voice state enabled)
═══════════════════════════════════════════════════════════════════════
"""

import datetime
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

XP_PER_MSG_DEFAULT = 15   # base; actual = random(15, 25) × multiplier
COOLDOWN_DEFAULT   = 60   # seconds between XP grants


def _xp_for_level(level: int) -> int:
    """XP required to reach `level` from level 0."""
    return 5 * (level ** 2) + 50 * level + 100


def _total_xp_for_level(level: int) -> int:
    """Cumulative XP needed to reach this level."""
    return sum(_xp_for_level(i) for i in range(level))


def _level_from_xp(total_xp: int) -> tuple[int, int, int]:
    """Return (level, current_xp_in_level, xp_needed_for_next)."""
    level = 0
    while total_xp >= _xp_for_level(level):
        total_xp -= _xp_for_level(level)
        level += 1
    return level, total_xp, _xp_for_level(level)


def _progress_bar(current: int, total: int, length: int = 20) -> str:
    filled = int(length * current / max(total, 1))
    bar    = "█" * filled + "░" * (length - filled)
    return f"[{bar}]"


class LeaderboardView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=120)
        self.pages     = pages
        self.current   = 0
        self.author_id = author_id
        self._update()

    def _update(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    async def _show(self, interaction: discord.Interaction):
        self._update()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your leaderboard.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _btn):
        self.current -= 1
        await self._show(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn):
        self.current += 1
        await self._show(interaction)


class Leveling(commands.Cog):
    """XP / Leveling system for Recluse."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # cooldown tracking  { (guild_id, user_id): last_xp_unix }
        self._xp_cd: dict[tuple[int, int], float] = {}
        # voice join time  { (guild_id, user_id): join_unix }
        self._voice_joined: dict[tuple[int, int], float] = {}
        self.voice_xp_ticker.start()

    def cog_unload(self):
        self.voice_xp_ticker.cancel()

    # ─── helpers ─────────────────────────────────────────────────────────────

    async def _get_cfg(self, guild_id: int) -> dict:
        if not hasattr(self.bot, "db"):
            return {}
        doc = await self.bot.db.guild_settings.find_one({"guild_id": guild_id})
        return doc or {}

    async def _get_user(self, guild_id: int, user_id: int) -> dict:
        doc = await self.bot.db.levels.find_one({"guild_id": guild_id, "user_id": user_id})
        return doc or {"guild_id": guild_id, "user_id": user_id, "xp": 0, "total_xp": 0}

    async def _save_user(self, guild_id: int, user_id: int, xp: int):
        await self.bot.db.levels.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$set": {"xp": xp, "last_updated": datetime.datetime.utcnow().timestamp()}},
            upsert=True,
        )

    async def _grant_xp(self, guild: discord.Guild, member: discord.Member, amount: int):
        """Core XP grant — checks level-ups and awards level-roles."""
        if not hasattr(self.bot, "db"):
            return
        doc = await self._get_user(guild.id, member.id)
        old_xp   = doc.get("xp", 0)
        new_xp   = old_xp + amount
        old_lvl  = _level_from_xp(old_xp)[0]
        new_lvl  = _level_from_xp(new_xp)[0]

        await self.bot.db.levels.update_one(
            {"guild_id": guild.id, "user_id": member.id},
            {
                "$inc": {"xp": amount},
                "$set": {"last_updated": datetime.datetime.utcnow().timestamp()},
            },
            upsert=True,
        )

        if new_lvl > old_lvl:
            await self._on_level_up(guild, member, new_lvl)

    async def _on_level_up(self, guild: discord.Guild, member: discord.Member, level: int):
        cfg = await self._get_cfg(guild.id)
        # Level-up notification
        ch_id = cfg.get("levelup_channel")
        msg   = cfg.get(
            "levelup_message",
            "🎉 {user} just levelled up to **Level {level}**!",
        )
        msg = msg.replace("{user}", member.mention).replace("{level}", str(level))

        # Find a channel
        channel = None
        if ch_id:
            channel = guild.get_channel(ch_id)

        if channel:
            try:
                await channel.send(msg)
            except discord.Forbidden:
                pass

        # Level-role rewards
        if hasattr(self.bot, "db"):
            role_doc = await self.bot.db.level_roles.find_one(
                {"guild_id": guild.id, "level": level}
            )
            if role_doc:
                role = guild.get_role(role_doc["role_id"])
                if role:
                    try:
                        await member.add_roles(role, reason=f"Level {level} reward")
                    except discord.Forbidden:
                        pass

    # ─── XP on message ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not hasattr(self.bot, "db"):
            return

        cfg = await self._get_cfg(message.guild.id)
        if not cfg.get("leveling_enabled", True):
            return

        key      = (message.guild.id, message.author.id)
        now      = time.monotonic()
        cooldown = cfg.get("xp_cooldown", COOLDOWN_DEFAULT)

        if now - self._xp_cd.get(key, 0) < cooldown:
            return
        self._xp_cd[key] = now

        base       = random.randint(15, 25)
        multiplier = float(cfg.get("xp_multiplier", 1.0))
        amount     = max(1, int(base * multiplier))
        await self._grant_xp(message.guild, message.author, amount)

    # ─── Voice XP (every 5 min) ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        key = (member.guild.id, member.id)
        if after.channel and not before.channel:
            self._voice_joined[key] = time.time()
        elif not after.channel and before.channel:
            self._voice_joined.pop(key, None)

    @tasks.loop(minutes=5)
    async def voice_xp_ticker(self):
        now = time.time()
        for (gid, uid), join_ts in list(self._voice_joined.items()):
            elapsed = now - join_ts
            if elapsed < 300:
                continue
            guild = self.bot.get_guild(gid)
            if not guild:
                continue
            member = guild.get_member(uid)
            if not member or member.bot:
                continue
            cfg = await self._get_cfg(gid)
            if not cfg.get("leveling_enabled", True):
                continue
            mult   = float(cfg.get("xp_multiplier", 1.0))
            amount = max(1, int(10 * mult))
            await self._grant_xp(guild, member, amount)
            self._voice_joined[(gid, uid)] = now  # reset timer

    @voice_xp_ticker.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # /rank  — personal rank card
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="rank", description="View your XP rank card.")
    @app_commands.describe(member="Member to look up (default: yourself).")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not interaction.guild:
            return await interaction.response.send_message("Server-only.", ephemeral=True)
        target = member or interaction.user
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ Database not connected.", ephemeral=True)

        await interaction.response.defer()

        doc = await self._get_user(interaction.guild.id, target.id)
        xp  = doc.get("xp", 0)
        lvl, cur_xp, needed = _level_from_xp(xp)
        bar = _progress_bar(cur_xp, needed)

        # Server rank
        cursor = self.bot.db.levels.find({"guild_id": interaction.guild.id}).sort("xp", -1)
        rank_pos = 1
        async for entry in cursor:
            if entry["user_id"] == target.id:
                break
            rank_pos += 1

        embed = discord.Embed(color=target.color if target.color.value else discord.Color(0x5865F2))
        embed.set_author(name=f"{target.display_name}'s Rank", icon_url=target.display_avatar.url)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="🏅 Server Rank", value=f"**#{rank_pos}**", inline=True)
        embed.add_field(name="⬆️ Level",       value=f"**{lvl}**",      inline=True)
        embed.add_field(name="✨ Total XP",    value=f"`{xp:,}`",        inline=True)
        embed.add_field(
            name=f"Progress  {cur_xp:,} / {needed:,} XP",
            value=f"`{bar}` {int(cur_xp / needed * 100)}%",
            inline=False,
        )
        embed.set_footer(text=interaction.guild.name)
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /leaderboard
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="View the XP leaderboard.")
    @app_commands.describe(page="Page number to jump to.")
    async def leaderboard(self, interaction: discord.Interaction, page: int = 1):
        if not interaction.guild:
            return await interaction.response.send_message("Server-only.", ephemeral=True)
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ Database not connected.", ephemeral=True)

        await interaction.response.defer()
        PAGE_SIZE = 10

        entries = await self.bot.db.levels.find(
            {"guild_id": interaction.guild.id}
        ).sort("xp", -1).to_list(200)

        if not entries:
            return await interaction.followup.send("No XP data recorded yet.")

        pages: list[discord.Embed] = []
        medal = ["🥇", "🥈", "🥉"]

        for i in range(0, len(entries), PAGE_SIZE):
            chunk = entries[i : i + PAGE_SIZE]
            embed = discord.Embed(
                title=f"📊 XP Leaderboard — {interaction.guild.name}",
                color=discord.Color(0x5865F2),
                timestamp=datetime.datetime.utcnow(),
            )
            lines = []
            for rank, entry in enumerate(chunk, start=i + 1):
                uid  = entry["user_id"]
                xp   = entry.get("xp", 0)
                lvl  = _level_from_xp(xp)[0]
                m    = medal[rank - 1] if rank <= 3 else f"`#{rank}`"
                member = interaction.guild.get_member(uid)
                name   = member.display_name if member else f"User {uid}"
                lines.append(f"{m} **{name}** — Level {lvl} (`{xp:,}` XP)")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Page {len(pages)+1}/{-(-len(entries)//PAGE_SIZE)}")
            pages.append(embed)

        if not pages:
            return await interaction.followup.send("No data.")

        start = max(0, min(page - 1, len(pages) - 1))
        view  = LeaderboardView(pages, interaction.user.id)
        view.current = start
        view._update()
        await interaction.followup.send(embed=pages[start], view=view)

    # ─────────────────────────────────────────────────────────────────────────
    # Admin overrides
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="givexp", description="[Admin] Give XP to a member.")
    @app_commands.describe(member="Target member.", amount="XP to give.")
    @app_commands.default_permissions(manage_guild=True)
    async def givexp(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.guild:
            return
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        await self._grant_xp(interaction.guild, member, amount)
        await interaction.response.send_message(
            f"✅ Gave **{amount:,} XP** to {member.mention}.", ephemeral=True
        )

    @app_commands.command(name="setlevel", description="[Admin] Force-set a member's level.")
    @app_commands.describe(member="Target member.", level="Level to set.")
    @app_commands.default_permissions(manage_guild=True)
    async def setlevel(self, interaction: discord.Interaction, member: discord.Member, level: int):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return
        if level < 0:
            return await interaction.response.send_message("❌ Level must be ≥ 0.", ephemeral=True)
        xp = _total_xp_for_level(level)
        await self.bot.db.levels.update_one(
            {"guild_id": interaction.guild.id, "user_id": member.id},
            {"$set": {"xp": xp}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Set {member.mention} to **Level {level}** (`{xp:,}` XP).", ephemeral=True
        )

    @app_commands.command(name="resetxp", description="[Admin] Reset a member's XP to zero.")
    @app_commands.describe(member="Target member.")
    @app_commands.default_permissions(manage_guild=True)
    async def resetxp(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return
        await self.bot.db.levels.delete_one(
            {"guild_id": interaction.guild.id, "user_id": member.id}
        )
        await interaction.response.send_message(
            f"✅ Reset XP for {member.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
