"""
leveling.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
Dual-Track XP System (Text & Voice) with anti-AFK, quality filters, 
and highly customized premium Pillow rank cards.
Now featuring Hybrid Commands (Slash + Prefix) & Global Backgrounds.
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
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
    rank_pos: int, bg_url: str | None = None
) -> io.BytesIO:
    width, height = 800, 350
    bg_color = (30, 31, 34) 
    
    if bg_url:
        try:
            # Add a browser User-Agent to bypass hotlink protection (403 Forbidden)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(bg_url, headers=headers) as resp:
                    resp.raise_for_status()
                    bg_bytes = await resp.read()
            
            base_img = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
            
            img_ratio = base_img.width / base_img.height
            target_ratio = width / height
            
            if img_ratio > target_ratio:
                new_w = int(height * img_ratio)
                base_img = base_img.resize((new_w, height))
                offset = (new_w - width) // 2
                base_img = base_img.crop((offset, 0, offset + width, height))
            else:
                new_h = int(width / img_ratio)
                base_img = base_img.resize((width, new_h))
                offset = (new_h - height) // 2
                base_img = base_img.crop((0, offset, width, offset + height))
            
            overlay = Image.new("RGBA", (width, height), (30, 31, 34, 150))
            card = Image.alpha_composite(base_img, overlay)
            
        except Exception as e:
            # Print the error to your console so you know exactly why it failed
            print(f"Failed to load background ({bg_url}): {e}")
            card = Image.new("RGBA", (width, height), bg_color)
    else:
        card = Image.new("RGBA", (width, height), bg_color)
    
    accent_color = member.color.to_rgb() if member.color.value else (88, 101, 242)

    glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.ellipse((-20, 20, 280, 320), fill=(accent_color[0], accent_color[1], accent_color[2], 40))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(50))
    card.alpha_composite(glow_layer)

    draw = ImageDraw.Draw(card)

    async with aiohttp.ClientSession() as session:
        async with session.get(member.display_avatar.with_format("png").with_size(256).url) as resp:
            avatar_bytes = await resp.read()
            
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((180, 180))
    mask = Image.new("L", (180, 180), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 180, 180), fill=255)
    avatar.putalpha(mask)
    
    draw.ellipse((37, 82, 223, 268), outline=accent_color, width=4)
    card.paste(avatar, (40, 85), avatar)

    try:
        font_xl    = ImageFont.truetype("font.ttf", 44)
        font_large = ImageFont.truetype("font.ttf", 36)
        font_med   = ImageFont.truetype("font.ttf", 22)
        font_small = ImageFont.truetype("font.ttf", 18)
    except IOError:
        font_xl = font_large = font_med = font_small = ImageFont.load_default()

    draw.text((270, 50), guild_name.upper(), font=font_small, fill=(150, 154, 160))
    draw.text((270, 75), member.display_name, font=font_xl, fill=(255, 255, 255))
    
    rank_text = f"RANK #{rank_pos}"
    rank_bbox = draw.textbbox((0, 0), rank_text, font=font_large)
    draw.text((width - 50 - (rank_bbox[2] - rank_bbox[0]), 85), rank_text, font=font_large, fill=(180, 184, 190))

    bar_x = 300
    bar_w = 450
    bar_h = 24
    icon_x = 265
    icon_c = (180, 184, 190)

    # ─────────────────────────────────────────────────────────
    # MESSAGE LEVEL TRACK
    # ─────────────────────────────────────────────────────────
    t_bar_y = 180
    t_center = t_bar_y + (bar_h // 2)
    
    plane_pts = [
        (icon_x - 12, t_center - 2),
        (icon_x + 12, t_center - 8),
        (icon_x + 6,  t_center + 10),
        (icon_x - 2,  t_center + 2)
    ]
    draw.polygon(plane_pts, fill=icon_c)
    
    draw.text((bar_x, t_bar_y - 30), f"Message Level: {t_lvl}", font=font_med, fill=(240, 242, 245))
    t_xp_text = f"{format_xp(t_cur)} / {format_xp(t_req)} XP"
    t_xp_bbox = draw.textbbox((0, 0), t_xp_text, font=font_small)
    draw.text((bar_x + bar_w - (t_xp_bbox[2] - t_xp_bbox[0]), t_bar_y - 28), t_xp_text, font=font_small, fill=(150, 154, 160))

    draw.rounded_rectangle([bar_x, t_bar_y, bar_x + bar_w, t_bar_y + bar_h], radius=bar_h//2, fill=(43, 45, 49))
    t_prog = max(0, min(1, t_cur / max(1, t_req)))
    t_fill_w = max(bar_h, int(bar_w * t_prog))
    draw.rounded_rectangle([bar_x, t_bar_y, bar_x + t_fill_w, t_bar_y + bar_h], radius=bar_h//2, fill=accent_color)

    # ─────────────────────────────────────────────────────────
    # VOICE LEVEL TRACK
    # ─────────────────────────────────────────────────────────
    v_bar_y = 270
    v_center = v_bar_y + (bar_h // 2)
    
    draw.rounded_rectangle([icon_x - 4, v_center - 10, icon_x + 4, v_center + 4], radius=4, fill=icon_c)
    draw.arc([icon_x - 8, v_center - 6, icon_x + 8, v_center + 8], start=0, end=180, fill=icon_c, width=2)
    draw.line([(icon_x, v_center + 8), (icon_x, v_center + 14)], fill=icon_c, width=2)
    draw.line([(icon_x - 6, v_center + 14), (icon_x + 6, v_center + 14)], fill=icon_c, width=2)

    draw.text((bar_x, v_bar_y - 30), f"Voice Level: {v_lvl}", font=font_med, fill=(240, 242, 245))
    v_xp_text = f"{format_xp(v_cur)} / {format_xp(v_req)} XP"
    v_xp_bbox = draw.textbbox((0, 0), v_xp_text, font=font_small)
    draw.text((bar_x + bar_w - (v_xp_bbox[2] - v_xp_bbox[0]), v_bar_y - 28), v_xp_text, font=font_small, fill=(150, 154, 160))

    draw.rounded_rectangle([bar_x, v_bar_y, bar_x + bar_w, v_bar_y + bar_h], radius=bar_h//2, fill=(43, 45, 49))
    v_prog = max(0, min(1, v_cur / max(1, v_req)))
    v_fill_w = max(bar_h, int(bar_w * v_prog))
    draw.rounded_rectangle([bar_x, v_bar_y, bar_x + v_fill_w, v_bar_y + bar_h], radius=bar_h//2, fill=accent_color)

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


class BackgroundSelector(discord.ui.View):
    def __init__(self, backgrounds: list[dict], author_id: int, db):
        super().__init__(timeout=180)
        self.backgrounds = backgrounds
        self.author_id = author_id
        self.db = db
        self.current = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.backgrounds) - 1

    async def get_current_embed(self) -> discord.Embed:
        bg = self.backgrounds[self.current]
        embed = discord.Embed(
            title=f"Background Preview ({self.current + 1} / {len(self.backgrounds)})",
            description=f"**Name:** {bg['name']}",
            color=discord.Color(0x5865F2)
        )
        embed.set_image(url=bg['url'])
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=await self.get_current_embed(), view=self)

    @discord.ui.button(label="Select This Background", style=discord.ButtonStyle.primary)
    async def select_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        bg = self.backgrounds[self.current]
        
        await self.db.users.update_one(
            {"user_id": self.author_id},
            {"$set": {"bg_url": bg["url"]}},
            upsert=True
        )
        await interaction.response.edit_message(content=f"✅ Your rank background has been set to **{bg['name']}**!", embed=None, view=None)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=await self.get_current_embed(), view=self)


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
                "$unset": {"xp": ""} 
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

        # Do not process XP if the message is a command
        ctx = await self.bot.get_context(message)
        if ctx.valid:
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


    @commands.hybrid_command(name="rank", description="View your Text & Voice XP rank card.")
    @app_commands.describe(member="Member to look up (default: yourself).")
    async def rank(self, ctx: commands.Context, member: discord.Member | None = None):
        if not ctx.guild:
            return await ctx.send("Server-only.", ephemeral=True)
            
        target = member or ctx.author
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database not connected.", ephemeral=True)

        # Tell Discord to expect a delay (avoids 'Interaction Failed' on slow downloads)
        await ctx.defer()

        doc = await self._get_user(ctx.guild.id, target.id)
        
        user_doc = await self.bot.db.users.find_one({"user_id": target.id})
        bg_url = user_doc.get("bg_url") if user_doc else None
        
        t_xp = doc.get("text_xp", 0)
        t_lvl, t_cur, t_req = _level_from_xp(t_xp)
        
        v_xp = doc.get("voice_xp", 0)
        v_lvl, v_cur, v_req = _level_from_xp(v_xp)

        cursor = self.bot.db.levels.find({"guild_id": ctx.guild.id}).sort("total_xp", -1)
        rank_pos = 1
        async for entry in cursor:
            if entry["user_id"] == target.id:
                break
            rank_pos += 1

        image_buffer = await create_rank_card(
            target, ctx.guild.name, 
            t_lvl, t_cur, t_req, 
            v_lvl, v_cur, v_req, 
            rank_pos, bg_url
        )
        file = discord.File(fp=image_buffer, filename="rank.png")
        await ctx.send(file=file)

    @commands.hybrid_command(name="leaderboard", description="View the global XP leaderboard.")
    @app_commands.describe(page="Page number to jump to.")
    async def leaderboard(self, ctx: commands.Context, page: int = 1):
        if not ctx.guild:
            return await ctx.send("Server-only.", ephemeral=True)
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database not connected.", ephemeral=True)

        await ctx.defer()
        PAGE_SIZE = 10

        entries = await self.bot.db.levels.find(
            {"guild_id": ctx.guild.id}
        ).sort("total_xp", -1).to_list(200)

        if not entries:
            return await ctx.send("No XP data recorded yet.")

        pages: list[discord.Embed] = []
        medal = ["🥇", "🥈", "🥉"]

        for i in range(0, len(entries), PAGE_SIZE):
            chunk = entries[i : i + PAGE_SIZE]
            embed = discord.Embed(
                title=f"📊 Global Leaderboard — {ctx.guild.name}",
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
                member = ctx.guild.get_member(uid)
                name   = member.display_name if member else f"User {uid}"
                lines.append(f"{m} **{name}** — `{format_xp(tot)}` Total XP (💬 {format_xp(t_xp)} | 🎙️ {format_xp(v_xp)})")
            
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Page {len(pages)+1}/{-(-len(entries)//PAGE_SIZE)}")
            pages.append(embed)

        if not pages:
            return await ctx.send("No data.")

        start = max(0, min(page - 1, len(pages) - 1))
        view  = LeaderboardView(pages, ctx.author.id)
        view.current = start
        view._update()
        await ctx.send(embed=pages[start], view=view)

    # =======================================================
    # BACKGROUND MANAGEMENT COMMANDS
    # =======================================================

    @commands.hybrid_command(name="addbg", description="Add a new background to the public gallery (Owner Only).")
    @app_commands.describe(name="Name for the background", url="Permanent direct image link (e.g., Imgur)")
    async def add_background(self, ctx: commands.Context, name: str, url: str):
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ This command is restricted to the bot owner.", ephemeral=True)
            
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database not connected.", ephemeral=True)
            
        if not (url.startswith("http://") or url.startswith("https://")):
            return await ctx.send("Please provide a valid image URL.", ephemeral=True)

        await self.bot.db.backgrounds.update_one(
            {"name": name},
            {"$set": {"url": url}},
            upsert=True
        )
        await ctx.send(f"✅ Background **{name}** added to the public gallery!", ephemeral=True)

    @commands.hybrid_command(name="removebg", description="Remove a background from the public gallery (Owner Only).")
    async def remove_background(self, ctx: commands.Context, name: str):
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ This command is restricted to the bot owner.", ephemeral=True)
            
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database not connected.", ephemeral=True)
            
        result = await self.bot.db.backgrounds.delete_one({"name": name})
        
        if result.deleted_count > 0:
            await ctx.send(f"🗑️ Background **{name}** has been removed.", ephemeral=True)
        else:
            await ctx.send(f"⚠️ Could not find a background named **{name}**.", ephemeral=True)

    @commands.hybrid_command(name="setbg", description="Choose a custom background for your rank card from the gallery.")
    async def set_background(self, ctx: commands.Context):
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database not connected.", ephemeral=True)

        backgrounds = await self.bot.db.backgrounds.find().to_list(100)
        
        if not backgrounds:
            return await ctx.send("No custom backgrounds are currently available in the gallery.", ephemeral=True)
            
        view = BackgroundSelector(backgrounds, ctx.author.id, self.bot.db)
        embed = await view.get_current_embed()
        
        await ctx.send(embed=embed, view=view, ephemeral=True)

    @commands.hybrid_command(name="resetbg", description="Remove your custom background and revert to the default theme.")
    async def reset_background(self, ctx: commands.Context):
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database not connected.", ephemeral=True)

        result = await self.bot.db.users.update_one(
            {"user_id": ctx.author.id},
            {"$unset": {"bg_url": ""}}
        )
        
        if result.modified_count > 0:
            await ctx.send("♻️ Your background has been reset to the default theme.", ephemeral=True)
        else:
            await ctx.send("ℹ️ You already have the default background.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
