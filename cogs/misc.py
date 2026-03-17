"""
misc.py  —  Recluse Bot  v2.0
Miscellaneous utility commands.

NSFW Censorship fix (matches desired behavior in screenshot):
  - Comprehensive 100+ word list
  - Replacement: first_char + asterisks + last_char  →  [p***s], [a*s]
  - Warning banner shown ONLY when censoring actually fired
  - In NSFW channels → full uncensored text, no banner
"""

import asyncio
import datetime
import io
import re

import aiohttp
import discord
from discord.ext import commands

# ═════════════════════════════════════════════════════════════════════════════
# Comprehensive NSFW word list
# ═════════════════════════════════════════════════════════════════════════════

_NSFW_WORDS: list[str] = sorted([
    # Multi-word phrases first (so they match before individual words)
    "sexual intercourse", "oral sex", "anal sex", "phone sex", "gang bang",
    "gangbang", "circle jerk", "sixty-nine", "sixty nine", "hand job",
    "blow job", "foot job", "tit job", "cum shot", "cream pie", "finger bang",
    "son of a bitch", "jerking off", "jacking off", "sex toy",
    # Body parts
    "penis", "vagina", "vulva", "clitoris", "scrotum", "testicles",
    "erection", "boner", "hard-on", "hardon", "asshole", "butthole",
    "nipples", "nipple", "breasts", "breast", "ballsack", "nutsack",
    "foreskin", "orgasm", "ejaculate", "ejaculation", "semen",
    "cock", "dick", "pussy", "cunt", "twat", "taint", "anus",
    "boobs", "boob", "tits", "titties", "titty", "balls", "shaft",
    "jizz", "cum", "sperm", "lube",
    # Sex acts
    "masturbation", "masturbate", "masturbating", "wanking", "fingering",
    "fisting", "rimming", "rimjob", "handjob", "blowjob", "edging",
    "squirting", "creampie", "cumshot", "bukakke", "bukkake", "facial",
    "threesome", "foursome", "orgy", "sexting", "sext",
    # Profanity
    "motherfucker", "motherfucking", "fucking", "fucked", "fucker", "fucks",
    "fuck", "bullshit", "shitting", "shitted", "shits", "shit",
    "bitches", "bitching", "bitch", "bastards", "bastard",
    "dumbass", "jackass", "smartass", "badass", "asshat", "asses", "ass",
    "crapping", "crap", "pissing", "pissed", "piss", "damned", "damn",
    "slutty", "slut", "whorish", "whore", "skank", "tramp", "thot", "hoe",
    "faggots", "faggot", "fag", "dyke",
    # Adult concepts
    "pornography", "porno", "porn", "hentai", "erotica", "erotic",
    "nudity", "nudes", "nude", "naked", "stripper", "stripping", "strip",
    "prostitution", "prostitute", "escort",
    "bondage", "dominatrix", "submissive", "bdsm", "fetish", "kinky", "kink",
    "dildo", "vibrator", "fleshlight",
    "horny", "aroused",
    "rape", "raping", "rapist", "molest", "molestation",
    "pedophile", "pedo", "incest",
    "crackwhore", "crackhead", "junkie",
    "sexy", "slutshame",
], key=len, reverse=True)


def _make_censored(word: str) -> str:
    """Build a censored replacement like [p***s] or [a*s]."""
    if len(word) <= 2:
        return f"[{'*' * len(word)}]"
    if len(word) == 3:
        return f"[{word[0]}*{word[-1]}]"
    return f"[{word[0]}{'*' * (len(word) - 2)}{word[-1]}]"


# Pre-compile all patterns once at import time for speed
_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE), _make_censored(w))
    for w in _NSFW_WORDS
]


def censor_text(text: str) -> tuple[str, bool]:
    """Apply NSFW censorship. Returns (result, was_censored)."""
    result, triggered = text, False
    for pattern, replacement in _COMPILED:
        new = pattern.sub(replacement, result)
        if new != result:
            triggered = True
            result    = new
    return result, triggered


def channel_is_nsfw(channel) -> bool:
    """Works for TextChannel, Thread, DMChannel, etc."""
    return getattr(channel, "nsfw", False) or bool(
        callable(getattr(channel, "is_nsfw", None)) and channel.is_nsfw()
    )


# ═════════════════════════════════════════════════════════════════════════════
# Paginator
# ═════════════════════════════════════════════════════════════════════════════

class SearchPaginator(discord.ui.View):
    def __init__(self, ctx, embeds):
        super().__init__(timeout=120)
        self.ctx          = ctx
        self.embeds       = embeds
        self.current_page = 0
        self._sync()

    def _sync(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.embeds) - 1
        self.prev_button.label    = f"◀ {self.current_page + 1}"
        self.next_button.label    = f"▶ {len(self.embeds)}"

    @discord.ui.button(style=discord.ButtonStyle.secondary, custom_id="ud_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("❌ Not your session.", ephemeral=True)
        self.current_page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary, custom_id="ud_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("❌ Not your session.", ephemeral=True)
        self.current_page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# Misc Cog
# ═════════════════════════════════════════════════════════════════════════════

class Misc(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def log_telemetry(self, guild_id: int, name: str):
        if hasattr(self.bot, "db"):
            await self.bot.db.command_telemetry.update_one(
                {"guild_id": guild_id, "command": name,
                 "date": datetime.datetime.utcnow().strftime("%Y-%m-%d")},
                {"$inc": {"uses": 1}}, upsert=True,
            )

    async def cog_check(self, ctx):
        if hasattr(self.bot, "db"):
            bl = await self.bot.db.global_blacklist.find_one(
                {"target_id": ctx.author.id, "type": "user"}
            )
            if bl:
                try:
                    await ctx.send("❌ **Access Denied:** Globally blacklisted.", ephemeral=True)
                except Exception:
                    pass
                return False
        if not ctx.guild:
            return True
        if hasattr(self.bot, "db"):
            s = await self.bot.db.guild_settings.find_one({"guild_id": ctx.guild.id})
            if s and not s.get("misc_enabled", True):
                await ctx.send("❌ Miscellaneous module disabled by admins.", ephemeral=True)
                return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # /search  — Urban Dictionary, properly censored in SFW channels
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="search",
        description="Search Urban Dictionary for the definition of a word or phrase.",
        usage="/search <term>",
        help="/search demicolon",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def search(self, ctx, *, term: str):
        await ctx.defer()

        url = f"https://api.urbandictionary.com/v0/define?term={term}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return await ctx.send("❌ Error contacting Urban Dictionary.")
                    data = await resp.json()
        except Exception:
            return await ctx.send("❌ Urban Dictionary is unreachable right now.")

        definitions = data.get("list", [])
        if not definitions:
            return await ctx.send(f"🔍 No definitions found for **{term}**.")

        nsfw = channel_is_nsfw(ctx.channel)
        embeds: list[discord.Embed] = []

        for i, entry in enumerate(definitions):
            raw_def = entry.get("definition", "") or ""
            raw_ex  = entry.get("example",    "") or ""
            author  = entry.get("author",     "Anonymous")
            up      = entry.get("thumbs_up",  0)
            down    = entry.get("thumbs_down", 0)

            # Strip Urban Dictionary's [word] link notation
            raw_def = re.sub(r"\[([^\]]+)\]", r"\1", raw_def).strip()
            raw_ex  = re.sub(r"\[([^\]]+)\]", r"\1", raw_ex).strip()

            if nsfw:
                # NSFW channel — show everything raw
                final_def    = raw_def
                final_ex     = raw_ex
                warning_text = ""
            else:
                # SFW channel — censor and add banner only if something was caught
                final_def, c1 = censor_text(raw_def)
                final_ex,  c2 = censor_text(raw_ex)
                if c1 or c2:
                    warning_text = (
                        "⚠️ **A few words may have been censored! "
                        "To view an uncensored version, use this command in a NSFW channel.** ⚠️\n\n"
                    )
                else:
                    warning_text = ""

            # Trim long content
            if len(final_def) > 900:
                final_def = final_def[:897] + "…"
            if len(final_ex) > 400:
                final_ex = final_ex[:397] + "…"

            description = warning_text + final_def
            if final_ex:
                description += f"\n\n*{final_ex}*"

            embed = discord.Embed(
                title=f"Definition of '{term}'",
                description=description,
                color=0x1D2439,
            )
            embed.set_footer(
                text=f"👍 {up}  👎 {down}  •  by {author}  •  {i + 1}/{len(definitions)}"
            )
            embeds.append(embed)

        if len(embeds) == 1:
            await ctx.send(embed=embeds[0])
        else:
            view         = SearchPaginator(ctx, embeds)
            view.message = await ctx.send(embed=embeds[0], view=view)

        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "search")

    # ─────────────────────────────────────────────────────────────────────────
    # /afk
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="afk", description="Set an AFK status.",
        usage="/afk [reason]", help="/afk Out for lunch",
    )
    async def afk(self, ctx, *, reason: str = "AFK"):
        if not hasattr(self.bot, "db"):
            return await ctx.send("❌ Database disconnected.")
        await self.bot.db.afk.update_one(
            {"user_id": ctx.author.id},
            {"$set": {"reason": reason, "timestamp": datetime.datetime.utcnow().timestamp()}},
            upsert=True,
        )
        await ctx.send(f"✅ {ctx.author.mention} — AFK status set: **{reason}**")
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "afk")

    # ─────────────────────────────────────────────────────────────────────────
    # /serverinfo
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="serverinfo", description="Comprehensive server data.",
        usage="/serverinfo", help="/serverinfo",
    )
    @commands.has_permissions(moderate_members=True)
    async def serverinfo(self, ctx):
        if not ctx.guild:
            return await ctx.send("❌ Server only.")
        g     = ctx.guild
        embed = discord.Embed(title=f"Server Dossier: {g.name}", color=0x2b2d31)
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        if g.banner:
            embed.set_image(url=g.banner.url)
        embed.add_field(name="👑 Owner",    value=f"{g.owner.mention} (`{g.owner.id}`)", inline=True)
        embed.add_field(name="🆔 ID",       value=f"`{g.id}`",                           inline=True)
        embed.add_field(name="📅 Created",  value=f"<t:{int(g.created_at.timestamp())}:R>", inline=True)
        bots   = sum(1 for m in g.members if m.bot)
        embed.add_field(
            name="👥 Members",
            value=f"Total: {g.member_count} | Humans: {g.member_count - bots} | Bots: {bots}",
            inline=False,
        )
        embed.add_field(
            name="🗂️ Channels",
            value=f"Text: {len(g.text_channels)} | Voice: {len(g.voice_channels)} | Roles: {len(g.roles)}",
            inline=False,
        )
        embed.add_field(name="🔒 Verification", value=str(g.verification_level).title(), inline=True)
        await ctx.send(embed=embed)
        await self.log_telemetry(ctx.guild.id, "serverinfo")

    # ─────────────────────────────────────────────────────────────────────────
    # /whois
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="whois", description="Pull a security profile on a user.",
        usage="/whois [user]", help="/whois @User",
    )
    @commands.has_permissions(moderate_members=True)
    async def whois(self, ctx, member: discord.Member = None):
        member  = member or ctx.author
        strikes = 0
        if hasattr(self.bot, "db"):
            rec = await self.bot.db.user_strikes.find_one(
                {"guild_id": ctx.guild.id, "user_id": member.id}
            )
            if rec:
                strikes = rec.get("strikes", 0)
        embed = discord.Embed(title=f"User Dossier: {member}", color=0x2b2d31)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="🆔 ID",         value=f"`{member.id}`",                         inline=True)
        embed.add_field(name="🤖 Bot",        value="Yes" if member.bot else "No",             inline=True)
        embed.add_field(name="⚠️ Strikes",   value=f"`{strikes}`",                            inline=True)
        embed.add_field(name="📅 Created",    value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        embed.add_field(
            name="📥 Joined",
            value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "?",
            inline=True,
        )
        embed.add_field(
            name="🎭 Top Role",
            value=member.top_role.mention if member.top_role != ctx.guild.default_role else "None",
            inline=True,
        )
        await ctx.send(embed=embed)
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "whois")

    # ─────────────────────────────────────────────────────────────────────────
    # /avatar
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="avatar", description="Retrieve a high-res profile picture.",
        usage="/avatar [user]", help="/avatar @User",
    )
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.defer()
        asset  = member.display_avatar.with_size(1024)
        try:
            data = await asset.read()
            fn   = f"avatar.{'gif' if asset.is_animated() else 'png'}"
            file = discord.File(io.BytesIO(data), filename=fn)
            embed = discord.Embed(title=f"Avatar: {member.name}", color=0x2b2d31)
            embed.set_image(url=f"attachment://{fn}")
            await ctx.send(embed=embed, file=file)
        except Exception:
            embed = discord.Embed(
                title=f"Avatar: {member.name}",
                description=f"[Direct Link]({member.display_avatar.url})",
                color=0x2b2d31,
            )
            embed.set_image(url=member.display_avatar.url)
            await ctx.send(embed=embed)
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "avatar")

    # ─────────────────────────────────────────────────────────────────────────
    # /roleinfo
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="roleinfo", description="Technical data on a role.",
        usage="/roleinfo <role>", help="/roleinfo @Mod",
    )
    @commands.has_permissions(moderate_members=True)
    async def roleinfo(self, ctx, role: discord.Role):
        perms     = [p[0].replace("_", " ").title() for p in role.permissions if p[1]]
        perms_str = ", ".join(perms) if perms else "None"
        embed     = discord.Embed(
            title=f"Role: {role.name}",
            color=role.color if role.color.value else 0x2b2d31,
        )
        embed.add_field(name="🆔 ID",      value=f"`{role.id}`",    inline=True)
        embed.add_field(name="🎨 Color",   value=f"`{role.color}`", inline=True)
        embed.add_field(name="👥 Members", value=str(len(role.members)), inline=True)
        embed.add_field(
            name="⚙️ Attributes",
            value=(
                f"Hoisted: {'Yes' if role.hoist else 'No'} | "
                f"Mentionable: {'Yes' if role.mentionable else 'No'}"
            ),
            inline=False,
        )
        embed.add_field(name="🛡️ Permissions", value=f"```{perms_str[:512]}```", inline=False)
        await ctx.send(embed=embed)
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "roleinfo")

    # ─────────────────────────────────────────────────────────────────────────
    # /channelinfo
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="channelinfo", description="Details for a channel.",
        usage="/channelinfo [channel]", help="/channelinfo #general",
    )
    @commands.has_permissions(manage_channels=True)
    async def channelinfo(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        embed   = discord.Embed(title=f"Channel: #{channel.name}", color=0x2b2d31)
        embed.add_field(name="🆔 ID",      value=f"`{channel.id}`",                                    inline=True)
        embed.add_field(name="📁 Category", value=channel.category.name if channel.category else "None", inline=True)
        embed.add_field(name="📺 Type",    value=str(channel.type).title(),                             inline=True)
        embed.add_field(
            name="💬 Settings",
            value=f"NSFW: {'Yes' if channel.is_nsfw() else 'No'} | Slowmode: {channel.slowmode_delay}s",
            inline=False,
        )
        await ctx.send(embed=embed)
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "channelinfo")

    # ─────────────────────────────────────────────────────────────────────────
    # /poll
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="poll", description="Create a multi-choice poll.",
        usage='/poll "Question" Choice1 Choice2 [Choice3-10]',
        help='/poll "Fav game?" Minecraft Valorant',
    )
    @commands.has_permissions(manage_messages=True)
    async def poll(
        self, ctx, message: str,
        choice1: str, choice2: str,
        choice3: str = None, choice4: str = None, choice5: str = None,
        choice6: str = None, choice7: str = None, choice8: str = None,
        choice9: str = None, choice10: str = None,
    ):
        await ctx.defer()
        choices = [c for c in [choice1, choice2, choice3, choice4, choice5,
                                choice6, choice7, choice8, choice9, choice10] if c]
        emojis  = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        desc    = f"**{message}**\n\n" + "\n\n".join(f"{emojis[i]} {c}" for i, c in enumerate(choices))
        embed   = discord.Embed(
            description=desc, color=0x2b2d31, timestamp=datetime.datetime.utcnow()
        )
        embed.set_footer(text=f"Poll by {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        for i in range(len(choices)):
            try:
                await poll_msg.add_reaction(emojis[i])
            except discord.Forbidden:
                pass
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "poll")

    # ─────────────────────────────────────────────────────────────────────────
    # /color
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="color", description="Analyse a HEX color.",
        usage="/color #FF5733", help="/color #FF5733",
    )
    async def color(self, ctx, hex_code: str):
        hex_code = hex_code.lstrip("#")
        if len(hex_code) != 6 or not all(c in "0123456789abcdefABCDEF" for c in hex_code):
            return await ctx.send("❌ Provide a valid 6-character HEX code e.g. `#FF5733`.")
        val   = int(hex_code, 16)
        r, g, b = (val >> 16) & 255, (val >> 8) & 255, val & 255
        embed = discord.Embed(title=f"Color: #{hex_code.upper()}", color=val)
        embed.add_field(name="HEX", value=f"`#{hex_code.upper()}`", inline=True)
        embed.add_field(name="RGB", value=f"`rgb({r}, {g}, {b})`",   inline=True)
        embed.set_thumbnail(url=f"https://dummyimage.com/100x100/{hex_code}/{hex_code}.png")
        await ctx.send(embed=embed)
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "color")

    # ─────────────────────────────────────────────────────────────────────────
    # /membercount
    # ─────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="membercount", description="Server population stats.",
        usage="/membercount", help="/membercount",
    )
    async def membercount(self, ctx):
        if not ctx.guild:
            return await ctx.send("❌ Server only.")
        bots  = sum(1 for m in ctx.guild.members if m.bot)
        embed = discord.Embed(title=f"👥 {ctx.guild.name}", color=0x2b2d31)
        embed.add_field(name="Total",  value=f"`{ctx.guild.member_count:,}`",         inline=True)
        embed.add_field(name="Humans", value=f"`{ctx.guild.member_count - bots:,}`",  inline=True)
        embed.add_field(name="Bots",   value=f"`{bots:,}`",                            inline=True)
        await ctx.send(embed=embed)
        if ctx.guild:
            await self.log_telemetry(ctx.guild.id, "membercount")

    # ─────────────────────────────────────────────────────────────────────────
    # AFK listener
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not hasattr(self.bot, "db") or not message.guild:
            return
        bl = await self.bot.db.global_blacklist.find_one(
            {"target_id": message.author.id, "type": "user"}
        )
        if bl:
            return
        s = await self.bot.db.guild_settings.find_one({"guild_id": message.guild.id})
        if s and not s.get("misc_enabled", True):
            return

        # Returning from AFK
        afk = await self.bot.db.afk.find_one({"user_id": message.author.id})
        if afk:
            await self.bot.db.afk.delete_one({"user_id": message.author.id})
            secs        = int((datetime.datetime.utcnow() -
                               datetime.datetime.utcfromtimestamp(afk.get("timestamp", 0))).total_seconds())
            m, s_rem    = divmod(secs, 60)
            h, m        = divmod(m, 60)
            time_str    = f"{h}h {m}m" if h else f"{m}m {s_rem}s"
            try:
                msg = await message.channel.send(
                    f"👋 Welcome back {message.author.mention}! You were AFK for {time_str}."
                )
                await msg.delete(delay=10)
            except Exception:
                pass

        # Mentioned someone who is AFK
        for u in message.mentions:
            if u.bot or u.id == message.author.id:
                continue
            row = await self.bot.db.afk.find_one({"user_id": u.id})
            if row:
                try:
                    await message.channel.send(
                        f"💤 **{u.display_name}** is AFK: {row.get('reason', 'AFK')} "
                        f"*(since <t:{int(row.get('timestamp', 0))}:R>)*"
                    )
                except Exception:
                    pass


async def setup(bot):
    await bot.add_cog(Misc(bot))
