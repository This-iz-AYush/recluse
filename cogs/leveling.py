"""
leveling.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
Dual-Track XP System (Text & Voice) with anti-AFK, quality filters, 
and highly customized Pillow rank cards.
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

def _level_from_xp(total_xp: int) -> tuple[int, int, int]:
    level = 0
    while total_xp >= _xp_for_level(level):
        total_xp -= _xp_for_level(level)
        level += 1
    return level, total_xp, _xp_for_level(level)

def format_xp(amount: int) -> str:
    """Formats 1100 to 1.1K"""
    if amount >= 1000:
        return f"{amount/1000:.1f}K".replace('.0K', 'K')
    return str(amount)


async def create_rank_card(
    member: discord.Member, guild_name: str, 
    t_lvl: int, t_cur: int, t_req: int, 
    v_lvl: int, v_cur: int, v_req: int, 
    rank_pos: int
) -> io.BytesIO:
    width, height = 800, 320
    bg_color = (25, 25, 30) 
    card = Image.new("RGBA", (width, height), bg_color)
    draw = ImageDraw.Draw(card)

    accent_color = member.color.to_rgb() if member.color.value else (88, 101, 242)

    # Fetch Avatar
    async with aiohttp.ClientSession() as session:
        async with session.get(member.display_avatar.with_format("png").with_size(256).url) as resp:
            avatar_bytes = await resp.read()
            
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((180, 180))
    mask = Image.new("L", (180, 180), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 180, 180), fill=255)
    avatar.putalpha(mask)
    
    draw.ellipse((36, 66, 224, 254), outline=accent_color, width=4)
    card.paste(avatar, (40, 70), avatar)

    # Fonts
    try:
        font_xl    = ImageFont.truetype("font.ttf", 46)
        font_large = ImageFont.truetype("font.ttf", 36)
        font_med   = ImageFont.truetype("font.ttf", 26)
        font_small = ImageFont.truetype("font.ttf", 22)
    except IOError:
        font_xl = font_large = font_med = font_small = ImageFont.load_default()

    # Top Text (Server, Username, Rank)
    draw.text((260, 45), guild_name.upper(), font=font_small, fill=(150, 150, 150))
    draw.text((260, 75), member.display_name, font=font_xl, fill=(255, 255, 255))
    
    rank_text = f"RANK #{rank_pos}"
    rank_bbox = draw.textbbox((0, 0), rank_text, font=font_large)
    draw.text((width - 50 - (rank_bbox[2] - rank_bbox[0]), 85), rank_text, font=font_large, fill=(200, 200, 200))

    # --- TEXT LEVEL TRACK ---
    t_lbl_y, t_bar_y = 155, 185
    
    # Text Icon (Chat Bubble)
    draw.rounded_rectangle([250, t_bar_y, 280, t_bar_y + 16], radius=5, fill=accent_color)
    draw.polygon([(255, t_bar_y + 15), (265, t_bar_y + 15), (255, t_bar_y + 22)], fill=accent_color)
    
    draw.text((300, t_lbl_y), f"TEXT LEVEL {t_lvl}", font=font_med, fill=accent_color)
    
    t_xp_text = f"{format_xp(t_cur)} / {format_xp(t_req)} XP"
    t_xp_bbox = draw.textbbox((0, 0), t_xp_text, font=font_small)
    draw.text((width - 50 - (t_xp_bbox[2] - t_xp_bbox[0]), t_lbl_y + 3), t_xp_text, font=font_small, fill=(180, 180, 180))

    draw.rounded_rectangle([300, t_bar_y, 750, t_bar_y + 16], radius=8, fill=(40, 40, 45))
    t_progress = max(0, min(1, t_cur / t_req))
    if int(450 * t_progress) > 15: 
        draw.rounded_rectangle([300, t_bar_y, 300 + int(450 * t_progress), t_bar_y + 16], radius=8, fill=accent_color)

    # --- VOICE LEVEL TRACK ---
    v_lbl_y, v_bar_y = 230, 260
    
    # Voice Icon (Microphone)
    draw.rounded_rectangle([260, v_bar_y - 2, 270, v_bar_y + 10], radius=4, fill=accent_color)
    draw.arc([254, v_bar_y, 276, v_bar_y + 14], start=0, end=180, fill=accent_color, width=2)
    draw.line([(265, v_bar_y + 14), (265, v_bar_y + 21)], fill=accent_color, width=2)
    draw.line([(258, v_bar_y + 21), (272, v_bar_y + 21)], fill=accent_color, width=2)

    draw.text((300, v_lbl_y), f"VOICE LEVEL {v_lvl}", font=font_med, fill=accent_color)
    
    v_xp_text = f"{format_xp(v_cur)} / {format_xp(v_req)} XP"
    v_xp_bbox = draw.textbbox((0, 0), v_xp_text, font=font_small)
    draw.text((width - 50 - (v_xp_bbox[2] - v_xp_bbox[0]), v_lbl_y + 3), v_xp_text, font=font_small, fill=(180, 180, 180))

    draw.rounded_rectangle([300, v_bar_y, 750, v_bar_y + 16], radius=8, fill=(40, 40, 45))
    v_progress = max(0, min(1, v_cur / v_req))
    if int(450 * v_progress) > 15: 
        draw.rounded_rectangle([300, v_bar_y, 300 + int(450 * v_progress), v_bar_y + 16], radius=8, fill=accent_color)

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
        if not doc:
            return {"text_xp": 0, "voice_xp": 0, "total_xp": 0}
        
        # Legacy migration check
        if "xp" in doc and "text_xp" not in doc:
            doc["text_xp"] = doc["xp"]
            doc["voice_xp"] = 0
            doc["total_xp"] = doc["xp"]
            
        return doc

    async def _grant_xp(self, guild: discord.Guild, member: discord.Member, amount: int, xp_type: str = "text"):
        if not hasattr(self.bot, "db"):
            return
            
        doc = await self._get_user(guild.id, member.id)
        
        old_text  = doc.get("text_xp", 0)
        old_voice = doc.get("voice_xp", 0)

        if xp_type == "text":
            new_text, new_voice = old_text + amount, old_voice
            old_lvl, new_lvl = _level_from_xp(old_text)[0], _level_from_xp(new_text)[0]
        else:
            new_text, new_voice = old_text, old_voice + amount
            old_lvl, new_lvl = _level_from_xp(old_voice)[0], _level_from_xp(new_voice)[0]

        total_xp = new_text + new_voice

        await self.bot.db.levels.update_one(
            {"guild_id": guild.id, "user_id": member.id},
            {
                "$set": {
                    "text_xp": new_text,
                    "voice_xp": new_voice,
                    "total_xp": total_xp,
                    "last_updated": datetime.datetime.utcnow().timestamp()
                },
                "$unset": {"xp": ""} # Clean up legacy data format
            },
            upsert=True,
        )

        if new_lvl > old_lvl:
            await self._on_level_up(guild, member, new_lvl, xp_type)

    async def _on_level_up(self, guild: discord.Guild, member: discord.Member, level: int, xp_type: str):
        cfg = await self._get_cfg(guild.id)
        ch_id = cfg.get("levelup_channel")
        type_str = "Text" if xp_type == "text" else "Voice"
        msg = cfg.get("levelup_message", "🎉 {user} just levelled up to **{type} Level {level}**!")
        msg = msg.replace("{user}", member.mention).replace("{level}", str(level)).replace("{type}", type_str)

        channel = guild.get_channel(ch_id) if ch_id else None
        if channel:
            try:
                await channel.send(msg)
            except discord.Forbidden:
                pass

        if hasattr(self.bot, "db"):
            role_doc = await self.bot.db.level_roles.find_one({"guild_id": guild.id, "level": level, "type": xp_type})
            if role_doc:
                role = guild.get_role(role_doc["role_id"])
                if role:
                    try:
                        await member.add_roles(role, reason=f"{type_str} Level {level} reward")
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
        await self._grant_xp(message.guild, message.author, amount, xp_type="text")

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
            await self._grant_xp(guild, member, amount, xp_type="voice")
            self._voice_joined[(gid, uid)] = now

    @voice_xp_ticker.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="rank", description="View your Text & Voice XP rank card.")
    @app_commands.describe(member="Member to look up (default: yourself).")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not interaction.guild:
            return await interaction.response.send_message("Server-only.", ephemeral=True)
            
        target = member or interaction.user
        if not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ Database not connected.", ephemeral=True)

        await interaction.response.defer()

        doc = await self._get_user(interaction.guild.id, target.id)
        
        # Parse Text Data
        t_xp = doc.get("text_xp", 0)
        t_lvl, t_cur, t_req = _level_from_xp(t_xp)
        
        # Parse Voice Data
        v_xp = doc.get("voice_xp", 0)
        v_lvl, v_cur, v_req = _level_from_xp(v_xp)

        # Global Server Rank based on total_xp
        cursor = self.bot.db.levels.find({"guild_id": interaction.guild.id}).sort("total_xp", -1)
        rank_pos = 1
        async for entry in cursor:
            if entry["user_id"] == target.id:
                break
            rank_pos += 1

        image_buffer = await create_rank_card(
            target, interaction.guild.name, 
            t_lvl, t_cur, t_req, 
            v_lvl, v_cur, v_req, 
            rank_pos
        )
        file = discord.File(fp=image_buffer, filename="rank.png")
        await interaction.followup.send(file=file)

    @app_commands.command(name="leaderboard", description="View the global XP leaderboard.")
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
        ).sort("total_xp", -1).to_list(200)

        if not entries:
            return await interaction.followup.send("No XP data recorded yet.")

        pages: list[discord.Embed] = []
        medal = ["🥇", "🥈", "🥉"]

        for i in range(0, len(entries), PAGE_SIZE):
            chunk = entries[i : i + PAGE_SIZE]
            embed = discord.Embed(
                title=f"📊 Global Leaderboard — {interaction.guild.name}",
                color=discord.Color(0x5865F2),
                timestamp=datetime.datetime.utcnow(),
            )
            lines = []
            for rank, entry in enumerate(chunk, start=i + 1):
                uid  = entry["user_id"]
                t_xp = entry.get("text_xp", 0)
                v_xp = entry.get("voice_xp", 0)
                tot  = entry.get("total_xp", t_xp + v_xp)
                m    = medal[rank - 1] if rank <= 3 else f"`#{rank}`"
                member = interaction.guild.get_member(uid)
                name   = member.display_name if member else f"User {uid}"
                lines.append(f"{m} **{name}** — `{format_xp(tot)}` Total XP (💬 {format_xp(t_xp)} | 🎙️ {format_xp(v_xp)})")
            
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
