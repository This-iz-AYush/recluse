"""
utility.py  —  Recluse Bot  v2.0
Utility commands: calculator, reminder, weather, translate,
base64, hash, say, tts-text, timestamp, urban (alias), timer

Inspired by Miza's utility and math modules.
"""
import asyncio, base64, datetime, hashlib, math, time
import aiohttp, discord
from discord import app_commands
from discord.ext import commands

_REMINDER_PATTERN = __import__("re").compile(
    r"((?P<d>\d+)\s*d(?:ays?)?)?\s*((?P<h>\d+)\s*h(?:ours?)?)?\s*((?P<m>\d+)\s*m(?:in(?:utes?)?)?)?\s*((?P<s>\d+)\s*s(?:econds?)?)?",
    __import__("re").IGNORECASE,
)

def _parse_time(s: str) -> int | None:
    m = _REMINDER_PATTERN.match(s.strip())
    if not m or not any(m.groups()): return None
    d = int(m.group("d") or 0)
    h = int(m.group("h") or 0)
    mn= int(m.group("m") or 0)
    sc= int(m.group("s") or 0)
    total = d*86400 + h*3600 + mn*60 + sc
    return total if total > 0 else None

class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # All commands under /util group → 1 slot
    util = app_commands.Group(name="util", description="Utility tools: calculator, translate, base64, hash, timestamp & more.")

    async def _log(self, gid, cmd):
        if hasattr(self.bot, "db"):
            await self.bot.db.command_telemetry.update_one(
                {"guild_id": gid, "command": cmd, "date": datetime.datetime.utcnow().strftime("%Y-%m-%d")},
                {"$inc": {"uses": 1}}, upsert=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /calc  — safe expression evaluator
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="calc", description="Calculate a math expression.")
    @app_commands.describe(expression="Expression to evaluate e.g. 2**10, sqrt(144), sin(pi/2)")
    async def calc(self, interaction: discord.Interaction, expression: str):
        safe_ns = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
        safe_ns.update({"abs": abs, "round": round, "min": min, "max": max,
                         "sum": sum, "int": int, "float": float, "pow": pow})
        # Strip anything that looks dangerous
        cleaned = __import__("re").sub(r"[^0-9\.\+\-\*\/\(\)\s\%\^a-zA-Z_,]", "", expression)
        cleaned = cleaned.replace("^", "**")
        try:
            result = eval(cleaned, {"__builtins__": {}}, safe_ns)  # noqa: S307
            if isinstance(result, float):
                result = round(result, 10)
                if result == int(result): result = int(result)
            embed = discord.Embed(
                title="🔢 Calculator",
                color=discord.Color(0x3498DB),
            )
            embed.add_field(name="Input",  value=f"`{expression}`",  inline=False)
            embed.add_field(name="Result", value=f"**`{result}`**",  inline=False)
        except Exception as e:
            embed = discord.Embed(
                title="❌ Calculation Error",
                description=f"`{e}`",
                color=discord.Color.red(),
            )
        await interaction.response.send_message(embed=embed)
        if interaction.guild: await self._log(interaction.guild.id, "calc")
    
    # ─────────────────────────────────────────────────────────────────────────
    # /translate  — uses MyMemory free API (no key needed)
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="translate", description="Translate text to another language. Free — no key needed.")
    @app_commands.describe(
        text="Text to translate.",
        target="Target language code e.g. en, hi, es, fr, de, ja, ko.",
        source="Source language code (default: auto-detect).",
    )
    async def translate(self, interaction: discord.Interaction, text: str, target: str = "en", source: str = "auto"):
        await interaction.response.defer()
        lang_pair = f"{source}|{target}" if source != "auto" else f"en|{target}"
        url = f"https://api.mymemory.translated.net/get?q={text[:500]}&langpair={lang_pair}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
            translated = data["responseData"]["translatedText"]
            quality    = data["responseData"]["match"]
            embed = discord.Embed(title="🌐 Translation", color=discord.Color(0x1ABC9C))
            embed.add_field(name=f"Source ({source})", value=text[:1024],       inline=False)
            embed.add_field(name=f"→ {target.upper()}", value=translated[:1024], inline=False)
            embed.set_footer(text=f"Confidence: {int(float(quality)*100)}%  •  Powered by MyMemory")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Translation failed: `{type(e).__name__}`")
        if interaction.guild: await self._log(interaction.guild.id, "translate")

    # ─────────────────────────────────────────────────────────────────────────
    # /base64  — encode / decode
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="base64", description="Encode or decode Base64.")
    @app_commands.describe(text="Text to encode or decode.", decode="Decode instead of encode.")
    async def base64cmd(self, interaction: discord.Interaction, text: str, decode: bool = False):
        try:
            if decode:
                result = base64.b64decode(text.encode()).decode("utf-8")
                title  = "🔓 Base64 Decoded"
            else:
                result = base64.b64encode(text.encode()).decode("utf-8")
                title  = "🔒 Base64 Encoded"
            embed = discord.Embed(title=title, color=discord.Color(0x95A5A6))
            embed.add_field(name="Input",  value=f"`{text[:500]}`",   inline=False)
            embed.add_field(name="Output", value=f"`{result[:1000]}`", inline=False)
        except Exception as e:
            embed = discord.Embed(title="❌ Error", description=str(e), color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /hash  — generate hash
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="hash", description="Hash text using MD5, SHA1, SHA256, or SHA512.")
    @app_commands.choices(algorithm=[
        app_commands.Choice(name="MD5",    value="md5"),
        app_commands.Choice(name="SHA1",   value="sha1"),
        app_commands.Choice(name="SHA256", value="sha256"),
        app_commands.Choice(name="SHA512", value="sha512"),
    ])
    async def hashcmd(self, interaction: discord.Interaction, text: str,
                      algorithm: app_commands.Choice[str] = None):
        algo = algorithm.value if algorithm else "sha256"
        h    = hashlib.new(algo, text.encode()).hexdigest()
        embed = discord.Embed(title=f"🔐 {algo.upper()} Hash", color=discord.Color(0x2C3E50))
        embed.add_field(name="Input",  value=f"`{text[:200]}`",  inline=False)
        embed.add_field(name="Output", value=f"```{h}```",        inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /timestamp  — convert time to Discord timestamp
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="timestamp", description="Generate a Discord timestamp from a Unix epoch or date.")
    @app_commands.describe(value="Unix timestamp (e.g. 1700000000) or 'now'.")
    async def timestamp(self, interaction: discord.Interaction, value: str = "now"):
        if value.lower() == "now":
            ts = int(time.time())
        else:
            try:
                ts = int(value)
            except ValueError:
                return await interaction.response.send_message("❌ Provide a Unix timestamp or 'now'.", ephemeral=True)

        embed = discord.Embed(title="🕐 Discord Timestamps", color=discord.Color(0x7289DA))
        formats = {
            "Short Time":       f"<t:{ts}:t>",
            "Long Time":        f"<t:{ts}:T>",
            "Short Date":       f"<t:{ts}:d>",
            "Long Date":        f"<t:{ts}:D>",
            "Short Date+Time":  f"<t:{ts}:f>",
            "Long Date+Time":   f"<t:{ts}:F>",
            "Relative":         f"<t:{ts}:R>",
        }
        for name, fmt in formats.items():
            embed.add_field(name=name, value=f"{fmt} → `{fmt}`", inline=False)
        embed.set_footer(text=f"Unix: {ts}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.describe(message="What to say.", channel="Channel to send to (default: current).")
    @app_commands.default_permissions(manage_messages=True)
    # ─────────────────────────────────────────────────────────────────────────
    # /charinfo  — Unicode character info (Miza-style)
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="charinfo", description="Get Unicode info about characters.")
    @app_commands.describe(characters="Characters to inspect (max 10).")
    async def charinfo(self, interaction: discord.Interaction, characters: str):
        chars = characters[:10]
        lines = []
        for c in chars:
            cp   = ord(c)
            name = __import__("unicodedata").name(c, "Unknown")
            lines.append(f"`U+{cp:04X}` — {name} — `{c}`")
        embed = discord.Embed(
            title="🔣 Unicode Character Info",
            description="\n".join(lines) or "No characters.",
            color=discord.Color(0x7F8C8D),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /timer  — simple countdown  (sends a message when done)
    # ─────────────────────────────────────────────────────────────────────────

    @util.command(name="timer", description="Set a countdown timer visible to everyone.")
    @app_commands.describe(duration="Duration e.g. 5m, 30s, 1h", label="Label for this timer.")
    async def timer(self, interaction: discord.Interaction, duration: str, label: str = "Timer"):
        secs = _parse_time(duration)
        if not secs or secs > 3600:
            return await interaction.response.send_message(
                "❌ Invalid duration. Max 1 hour. Use e.g. `5m`, `30s`.", ephemeral=True
            )
        fire_at = int(time.time()) + secs
        embed = discord.Embed(
            title=f"⏱️ Timer: {label}",
            description=f"Ending <t:{fire_at}:R>",
            color=discord.Color(0xE74C3C),
        )
        await interaction.response.send_message(embed=embed)

        await asyncio.sleep(secs)
        try:
            await interaction.channel.send(
                f"⏰ **Timer done!** ⌛ **{label}** — {interaction.user.mention}"
            )
        except Exception:
            pass
        if interaction.guild: await self._log(interaction.guild.id, "timer")

async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
