"""
leveling.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
Custom XP & Ranking system with anti-AFK, quality filters, and image cards.
═══════════════════════════════════════════════════════════════════════
"""

import datetime
import random
import time
import io
import aiohttp

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

XP_PER_MSG_DEFAULT = 15
COOLDOWN_DEFAULT   = 60


def _xp_for_level(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100

def _total_xp_for_level(level: int) -> int:
    return sum(_xp_for_level(i) for i in range(level))

def _level_from_xp(total_xp: int) -> tuple[int, int, int]:
    level = 0
    while total_xp >= _xp_for_level(level):
        total_xp -= _xp_for_level(level)
        level += 1
    return level, total_xp, _xp_for_level(level)


async def create_rank_card(member: discord.Member, level: int, cur_xp: int, needed_xp: int, rank_pos: int) -> io.BytesIO:
    width, height = 800, 250
    bg_color = (25, 25, 30) 
    card = Image.new("RGBA", (width, height), bg_color)
    draw = ImageDraw.Draw(card)

    accent_color = member.color.to_rgb() if member.color.value else (88, 101, 242)

    # Fetch Avatar
    async with aiohttp.ClientSession() as session:
        async with session.get(member.display_avatar.with_format("png").with_size(256).url) as resp:
            avatar_bytes = await resp.read()
            
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((160, 160))
    mask = Image.new("L", (160, 160), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 160, 160), fill=255)
    avatar.putalpha(mask)
    
    draw.ellipse((45, 45, 215, 215), outline=accent_color, width=4)
    card.paste(avatar, (50, 50), avatar)

    # Fonts - assumes font.ttf is in the same directory as bot.py
    try:
        font_large = ImageFont.truetype("font.ttf", 42)
        font_medium = ImageFont.truetype("font.ttf", 32)
        font_small = ImageFont.truetype("font.ttf", 24)
    except IOError:
        font_large = font_medium = font_small = ImageFont.load_default()

    # Text
    draw.text((250, 60), member.display_name, font=font_large, fill=(255, 255, 255))
    draw.text((250, 115), f"RANK #{rank_pos}", font=font_medium, fill=(200, 200, 200))
    draw.text((450, 115), f"LEVEL {level}", font=font_medium, fill=accent_color)
    
    xp_text = f"{cur_xp:,} / {needed_xp:,} XP"
    xp_bbox = draw.textbbox((0, 0), xp_text, font=font_small)
    xp_width = xp_bbox[2] - xp_bbox[0]
    draw.text((width - 50 - xp_width, 140), xp_text, font=font_small, fill=(180, 180, 180))

    # Progress Bar
    bar_x, bar_y = 250, 175
    bar_width, bar_height = 500, 25
    
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height], radius=12, fill=(40, 40, 45))
    
    progress = max(0, min(1, cur_xp / needed_xp))
    fill_width = int(bar_width * progress)
    if fill_width > 15: 
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_width, bar_y + bar_height], radius=12, fill=accent_color)

    buffer = io.BytesIO()
    card.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


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
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._xp_cd: dict[tuple[int, int], float] = {}
        self._voice_joined: dict[tuple[int, int], float] = {}
        self.voice_xp_ticker.start()

    def cog_unload(self):
        self.voice_xp_ticker.cancel()

    async def _get_cfg(self, guild_id: int) -> dict:
        if not hasattr(self.bot, "db"):
            return {}
        doc = await self.bot.db.guild_settings.find_one({"guild_id": guild_id})
        return doc or {}

    async def _get_user(self, guild_id: int, user_id: int) -> dict:
        doc = await self.bot.db.levels.find_one({"guild_id": guild_id, "user_id": user_id})
        return doc or {"guild_id": guild_id, "user_id": user_id, "xp": 0}

    async def _grant_xp(self, guild: discord.Guild, member: discord.Member, amount: int):
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
        ch_id = cfg.get("levelup_channel")
        msg   = cfg.get("levelup_message", "🎉 {user} just levelled up to **Level {level}**!")
        msg = msg.replace("{user}", member.mention).replace("{level}", str(level))

        channel = guild.get_channel(ch_id) if ch_id else None
        if channel:
            try:
                await channel.send(msg)
            except discord.Forbidden:
                pass

        if hasattr(self.bot, "db"):
            role_doc = await self.bot.db.level_roles.find_one({"guild_id": guild.id, "level": level})
            if role_doc:
                role = guild.get_role(role_doc["role_id"])
                if role:
                    try:
                        await member.add_roles(role, reason=f"Level {level} reward")
                    except discord.Forbidden:
                        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not hasattr(self.bot, "db"):
            return

        cfg = await self._get_cfg(message.guild.id)
        if not cfg.get("leveling_enabled", True):
            return

        clean_text = message.content.strip()
        if len(clean_text) < 5 and not message.attachments:
            return

        key      = (message.guild.id, message.author.id)
        now      = time.monotonic()
        cooldown = cfg.get("xp_cooldown", COOLDOWN_DEFAULT)

        if now - self._xp_cd.get(key, 0) < cooldown:
            return
        self._xp_cd[key] = now

        base = random.randint(15, 25)
        if message.attachments:
            base += 10 
        elif len(clean_text) > 80:
            base += 5   

        multiplier = float(cfg.get("xp_multiplier", 1.0))
        amount     = max(1, int(base * multiplier))
        await self._grant_xp(message.guild, message.author, amount)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
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
                
            voice_state = member.voice
            if not voice_state or not voice_state.channel:
                self._voice_joined.pop((gid, uid), None)
                continue

            humans_in_vc = [m for m in voice_state.channel.members if not m.bot]
            if len(humans_in_vc) < 2:
                continue

            if voice_state.self_deaf or voice_state.deaf:
                continue

            cfg = await self._get_cfg(gid)
            if not cfg.get("leveling_enabled", True):
                continue

            mult = float(cfg.get("xp_multiplier", 1.0))
            if voice_state.self_stream or voice_state.self_video:
                mult *= 1.25
                
            amount = max(1, int(15 * mult))
            await self._grant_xp(guild, member, amount)
            self._voice_joined[(gid, uid)] = now

    @voice_xp_ticker.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

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
        xp = doc.get("xp", 0)
        lvl, cur_xp, needed = _level_from_xp(xp)

        cursor = self.bot.db.levels.find({"guild_id": interaction.guild.id}).sort("xp", -1)
        rank_pos = 1
        async for entry in cursor:
            if entry["user_id"] == target.id:
                break
            rank_pos += 1

        image_buffer = await create_rank_card(target, lvl, cur_xp, needed, rank_pos)
        file = discord.File(fp=image_buffer, filename="rank.png")
        await interaction.followup.send(file=file)


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
