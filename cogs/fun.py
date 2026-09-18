"""
fun.py  —  Recluse Bot  v2.0
ALL commands grouped under /fun <subcommand> → counts as 1 slash command slot.
"""
import asyncio, datetime, random, re, os, html
import aiohttp, discord
from discord import app_commands
from discord.ext import commands
from google import genai

# Initialize Gemini Client
# Fails safely to None if the key isn't provided, allowing fallbacks to take over
api_key = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=api_key) if api_key else None

async def generate_fun_text(prompt: str, fallback: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return fallback
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", fallback).strip()
    except Exception:
        pass
    return fallback

# --- STATIC FALLBACK LISTS ---
_8BALL = [
    "🟢 It is certain.","🟢 It is decidedly so.","🟢 Without a doubt.",
    "🟢 Yes, definitely.","🟢 You may rely on it.","🟢 Most likely.",
    "🟡 Reply hazy, try again.","🟡 Ask again later.","🟡 Cannot predict now.",
    "🔴 Don't count on it.","🔴 My reply is no.","🔴 Very doubtful.",
]
_COMPLIMENTS = [
    "You have an incredible ability to brighten everyone's day. ☀️",
    "Your dedication is genuinely inspiring. 💪",
    "The world is a better place with you in it. 🌍",
    "Your creativity is absolutely stunning. 🎨",
    "You are genuinely one of a kind. 💎",
    "You handle challenges with incredible grace. 🎯",
]
_ROASTS = [
    "If laughter is the best medicine, your face must be curing diseases worldwide.",
    "You're not stupid — you just have bad luck thinking.",
    "You're like a cloud — when you disappear, it's a beautiful day.",
    "I'd agree with you but then we'd both be wrong.",
    "You bring everyone so much joy — whenever you leave the room.",
]
_DADJOKES = [
    ("Why don't scientists trust atoms?","Because they make up everything!"),
    ("What do you call cheese that isn't yours?","Nacho cheese!"),
    ("Why can't you give Elsa a balloon?","She'll let it go."),
    ("Why did the scarecrow win an award?","He was outstanding in his field."),
    ("How do you organize a space party?","You planet."),
]
_FACTS = [
    "Honey never spoils. 3,000-year-old honey found in Egyptian tombs was still edible.",
    "Bananas are berries, but strawberries aren't.",
    "Octopuses have three hearts and blue blood.",
    "Sharks are older than trees.",
    "Sea otters hold hands while sleeping so they don't drift apart.",
    "Wombat poop is cube-shaped.",
]
_QUOTES = [
    ("The only way to do great work is to love what you do.","Steve Jobs"),
    ("In the middle of every difficulty lies opportunity.","Albert Einstein"),
    ("It always seems impossible until it's done.","Nelson Mandela"),
    ("Spread love everywhere you go.","Mother Teresa"),
]
_WYR = [
    ("Fight 100 duck-sized horses","Fight 1 horse-sized duck"),
    ("Always be 10 minutes late","Always be 20 minutes early"),
    ("Know when you'll die","Know how you'll die"),
    ("Speak every language","Talk to animals"),
]
_TRIVIA = [
    {"q":"Capital of Australia?","choices":["Sydney","Melbourne","Canberra","Brisbane"],"correct":2},
    {"q":"Sides on a hexagon?","choices":["5","6","7","8"],"correct":1},
    {"q":"Chemical symbol for gold?","choices":["Go","Gd","Au","Ag"],"correct":2},
    {"q":"The Red Planet?","choices":["Venus","Mars","Jupiter","Saturn"],"correct":1},
    {"q":"Who painted the Mona Lisa?","choices":["Picasso","Monet","Da Vinci","Rembrandt"],"correct":2},
    {"q":"Largest ocean?","choices":["Atlantic","Indian","Pacific","Arctic"],"correct":2},
    {"q":"WWII ended in?","choices":["1943","1944","1945","1946"],"correct":2},
    {"q":"Square root of 144?","choices":["10","11","12","13"],"correct":2},
]
_ACTIVE_TRIVIA: dict[int,str] = {}


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _log(self, gid, cmd):
        if hasattr(self.bot,"db"):
            await self.bot.db.command_telemetry.update_one(
                {"guild_id":gid,"command":cmd,"date":datetime.datetime.utcnow().strftime("%Y-%m-%d")},
                {"$inc":{"uses":1}},upsert=True)

    # ─── ALL commands live inside this single group ───────────────────────────
    fun = app_commands.Group(name="fun", description="Fun and entertainment commands.")

    @fun.command(name="8ball", description="Ask the magic 8-ball.")
    @app_commands.describe(question="Your yes/no question.")
    async def eightball(self, i: discord.Interaction, question: str):
        await i.response.defer()
        prompt = f"Act as a mystical magic 8-ball. The user asks: '{question}'. Give a short, creative, and mysterious yes/no/maybe response in 1 sentence."
        answer = await generate_fun_text(prompt, fallback=random.choice(_8BALL))
        
        e = discord.Embed(color=discord.Color(0x1a1a2e))
        e.add_field(name="🎱 Question", value=question, inline=False)
        e.add_field(name="Answer", value=answer, inline=False)
        await i.followup.send(embed=e)
        if i.guild: await self._log(i.guild.id, "8ball")

    @fun.command(name="coinflip", description="Flip a coin.")
    @app_commands.choices(bet=[app_commands.Choice(name="Heads",value="heads"),app_commands.Choice(name="Tails",value="tails")])
    async def coinflip(self, i: discord.Interaction, bet: app_commands.Choice[str]=None):
        r = random.choice(["Heads","Tails"])
        if bet:
            won = r.lower() == bet.value
            desc = f"**🪙 {r}**\nYour guess: `{bet.value}` — {'✅ You won!' if won else '❌ You lost!'}"
            color = discord.Color.green() if won else discord.Color.red()
        else:
            desc = f"**🪙 {r}!**"; color = discord.Color(0xF1C40F)
        await i.response.send_message(embed=discord.Embed(title="Coin Flip", description=desc, color=color))
        if i.guild: await self._log(i.guild.id, "coinflip")

    @fun.command(name="dice", description="Roll dice. e.g. 2d6")
    @app_commands.describe(notation="Dice notation e.g. 2d6, 1d20")
    async def dice(self, i: discord.Interaction, notation: str="1d6"):
        m = re.fullmatch(r"(\d+)d(\d+)", notation.lower().strip())
        if not m: return await i.response.send_message("❌ Use XdY e.g. `2d6`", ephemeral=True)
        c, s = int(m.group(1)), int(m.group(2))
        if not(1 <= c <= 20): return await i.response.send_message("❌ 1–20 dice.", ephemeral=True)
        if not(2 <= s <= 1000): return await i.response.send_message("❌ 2–1000 sides.", ephemeral=True)
        rolls = [random.randint(1,s) for _ in range(c)]
        desc = " + ".join(f"`{r}`" for r in rolls)
        if c > 1: desc += f"\n\n**Total: `{sum(rolls)}`**"
        await i.response.send_message(embed=discord.Embed(title=f"🎲 {notation}", description=desc, color=discord.Color(0xE74C3C)))
        if i.guild: await self._log(i.guild.id, "dice")

    @fun.command(name="choose", description="Pick from comma-separated options.")
    @app_commands.describe(options="Comma-separated options")
    async def choose(self, i: discord.Interaction, options: str):
        ch = [c.strip() for c in options.split(",") if c.strip()]
        if len(ch) < 2: return await i.response.send_message("❌ Need ≥2 options.", ephemeral=True)
        e = discord.Embed(title="🎯 I choose…", description=f"**{random.choice(ch)}**", color=discord.Color(0x9B59B6))
        e.set_footer(text=f"From: {', '.join(ch)}")
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "choose")

    @fun.command(name="reverse", description="Reverse text.")
    async def reverse(self, i: discord.Interaction, text: str):
        await i.response.send_message(f"🔄 `{text[::-1]}`")

    @fun.command(name="mock", description="SpongeBob mocking text.")
    async def mock(self, i: discord.Interaction, text: str):
        await i.response.send_message(f"🧽 `{''.join(c.upper() if idx%2 else c.lower() for idx,c in enumerate(text))}`")

    @fun.command(name="clap", description="Add 👏 between words.")
    async def clap(self, i: discord.Interaction, text: str):
        await i.response.send_message(" 👏 ".join(text.split()))

    @fun.command(name="joke", description="Get a random joke.")
    async def joke(self, i: discord.Interaction):
        await i.response.defer()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://v2.jokeapi.dev/joke/Any?blacklistFlags=nsfw,religious,political,racist,sexist,explicit", timeout=aiohttp.ClientTimeout(total=8)) as r:
                    d = await r.json()
            if d.get("type") == "twopart":
                e = discord.Embed(title="😂 Joke", color=discord.Color(0xF39C12))
                e.add_field(name="Setup", value=d["setup"], inline=False)
                e.add_field(name="Punchline", value=f"||{d['delivery']}||", inline=False)
            else:
                e = discord.Embed(title="😂 Joke", description=d.get("joke","…"), color=discord.Color(0xF39C12))
        except Exception:
            setup, delivery = random.choice(_DADJOKES)
            e = discord.Embed(title="😂 Dad Joke", color=discord.Color(0xF39C12))
            e.add_field(name="Setup", value=setup, inline=False)
            e.add_field(name="Punchline", value=f"||{delivery}||", inline=False)
        await i.followup.send(embed=e)
        if i.guild: await self._log(i.guild.id, "joke")

    @fun.command(name="dadjoke", description="Dad joke.")
    async def dadjoke(self, i: discord.Interaction):
        s, d = random.choice(_DADJOKES)
        e = discord.Embed(title="👨 Dad Joke", color=discord.Color(0xE67E22))
        e.add_field(name="Setup", value=s, inline=False)
        e.add_field(name="Punchline", value=f"||{d}||", inline=False)
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "dadjoke")

    @fun.command(name="fact", description="Random interesting fact.")
    async def fact(self, i: discord.Interaction):
        await i.response.defer()
        url = "https://uselessfacts.jsph.pl/api/v2/facts/random?language=en"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=5) as r:
                    if r.status == 200:
                        data = await r.json()
                        fact_text = data.get("text", random.choice(_FACTS))
                    else:
                        fact_text = random.choice(_FACTS)
        except Exception:
            fact_text = random.choice(_FACTS)
            
        await i.followup.send(embed=discord.Embed(title="💡 Fact", description=fact_text, color=discord.Color(0x1ABC9C)))
        if i.guild: await self._log(i.guild.id, "fact")

    @fun.command(name="quote", description="Inspiring quote.")
    async def quote(self, i: discord.Interaction):
        await i.response.defer()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://zenquotes.io/api/random", timeout=5) as r:
                    if r.status == 200:
                        data = await r.json()
                        q = data[0].get("q")
                        a = data[0].get("a")
                    else:
                        q, a = random.choice(_QUOTES)
        except Exception:
            q, a = random.choice(_QUOTES)
            
        await i.followup.send(embed=discord.Embed(description=f'*"{q}"*\n\n— **{a}**', color=discord.Color(0x3498DB)))
        if i.guild: await self._log(i.guild.id, "quote")

    @fun.command(name="compliment", description="Send someone a compliment.")
    async def compliment(self, i: discord.Interaction, member: discord.Member=None):
        await i.response.defer()
        t = member or i.user
        
        prompt = f"Write a short, heartwarming, and unique compliment for someone named '{t.display_name}'. Keep it to 1-2 sentences with an emoji."
        comp_text = await generate_fun_text(prompt, fallback=random.choice(_COMPLIMENTS))
        
        e = discord.Embed(title=f"💖 Compliment for {t.display_name}", description=comp_text, color=discord.Color(0xFF69B4))
        await i.followup.send(embed=e)
        if i.guild: await self._log(i.guild.id, "compliment")

    @fun.command(name="roast", description="Roast someone (fun only).")
    async def roast(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer()
        
        prompt = (
            f"Write a witty, clever, playful roast targeted at someone named '{member.display_name}'. "
            "Keep it strictly PG-13, lighthearted, funny, and no longer than 2 sentences. "
            "Do not use slurs or genuine toxicity."
        )
        roast_text = await generate_fun_text(prompt, fallback=random.choice(_ROASTS))
        
        e = discord.Embed(title=f"🔥 Roast: {member.display_name}", description=roast_text, color=discord.Color(0xFF4500))
        e.set_footer(text="All in good fun!")
        await i.followup.send(embed=e)
        if i.guild: await self._log(i.guild.id, "roast")

    @fun.command(name="rps", description="Rock Paper Scissors.")
    @app_commands.choices(choice=[app_commands.Choice(name="🪨 Rock",value="rock"),app_commands.Choice(name="📄 Paper",value="paper"),app_commands.Choice(name="✂️ Scissors",value="scissors")])
    async def rps(self, i: discord.Interaction, choice: app_commands.Choice[str]):
        em = {"rock":"🪨", "paper":"📄", "scissors":"✂️"}
        bp = random.choice(["rock", "paper", "scissors"])
        u = choice.value
        wins = {"rock":"scissors", "paper":"rock", "scissors":"paper"}
        
        if u == bp: res, col = "🤝 Tie!", discord.Color.yellow()
        elif wins[u] == bp: res, col = "✅ You win!", discord.Color.green()
        else: res, col = "❌ You lose!", discord.Color.red()
        
        e = discord.Embed(title="🎮 Rock Paper Scissors", color=col)
        e.add_field(name="You", value=f"{em[u]} {u.title()}", inline=True)
        e.add_field(name="Bot", value=f"{em[bp]} {bp.title()}", inline=True)
        e.add_field(name="Result", value=res, inline=False)
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "rps")

    @fun.command(name="would_you_rather", description="Would you rather…")
    async def wyr(self, i: discord.Interaction):
        await i.response.defer()
        
        prompt = (
            "Generate an entertaining and absurd 'Would You Rather' dilemma. "
            "Format your output strictly as: Option A | Option B. "
            "Do not add any other words."
        )
        raw_text = await generate_fun_text(prompt, fallback=f"{_WYR[0][0]} | {_WYR[0][1]}")
        
        if "|" in raw_text:
            a, b = [part.strip() for part in raw_text.split("|", 1)]
        else:
            a, b = random.choice(_WYR)

        e = discord.Embed(title="🤔 Would You Rather…", color=discord.Color(0x8E44AD))
        e.add_field(name="🅰️", value=a, inline=True)
        e.add_field(name="🅱️", value=b, inline=True)
        e.set_footer(text="React 🅰️ or 🅱️ to vote!")
        
        msg = await i.followup.send(embed=e)
        try:
            msg = await i.original_response()
            await msg.add_reaction("🅰️")
            await msg.add_reaction("🅱️")
        except Exception: pass
        if i.guild: await self._log(i.guild.id, "would_you_rather")

    @fun.command(name="trivia", description="Multiple-choice trivia with a 30s timer.")
    async def trivia(self, i: discord.Interaction):
        if i.channel.id in _ACTIVE_TRIVIA:
            return await i.response.send_message("❌ Active trivia already running here!", ephemeral=True)
        await i.response.defer()
        
        question_text = ""
        choices = []
        correct_idx = 0
        
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://opentdb.com/api.php?amount=1&type=multiple", timeout=5) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("results"):
                            item = data["results"][0]
                            question_text = html.unescape(item["question"])
                            correct_ans = html.unescape(item["correct_answer"])
                            inc_ans = [html.unescape(ans) for ans in item["incorrect_answers"]]
                            
                            choices = inc_ans + [correct_ans]
                            random.shuffle(choices)
                            correct_idx = choices.index(correct_ans)
        except Exception:
            pass
            
        if not choices:
            q = random.choice(_TRIVIA)
            question_text = q["q"]
            choices = q["choices"]
            correct_idx = q["correct"]
            
        _ACTIVE_TRIVIA[i.channel.id] = str(correct_idx)
        
        e = discord.Embed(title="🧠 Trivia!", description=question_text, color=discord.Color(0x2ECC71))
        opts = "\n".join(f"{'ABCD'[idx]}. {c}" for idx, c in enumerate(choices))
        e.add_field(name="Options", value=f"```\n{opts}\n```", inline=False)
        e.set_footer(text="Type A/B/C/D in chat • 30 seconds")
        await i.followup.send(embed=e)
        
        def chk(m):
            return m.channel == i.channel and m.content.upper() in "ABCD" and len(m.content) == 1 and not m.author.bot
            
        try:
            msg = await self.bot.wait_for("message", timeout=30.0, check=chk)
            idx = "ABCD".index(msg.content.upper())
            if idx == correct_idx:
                re_e = discord.Embed(title="✅ Correct!", description=f"{msg.author.mention} got it!\n**{choices[correct_idx]}**", color=discord.Color.green())
            else:
                re_e = discord.Embed(title="❌ Wrong!", description=f"{msg.author.mention} said **{choices[idx]}**\nAnswer: **{choices[correct_idx]}**", color=discord.Color.red())
            await i.channel.send(embed=re_e)
        except asyncio.TimeoutError:
            await i.channel.send(embed=discord.Embed(title="⏰ Time's Up!", description=f"Answer: **{choices[correct_idx]}**", color=discord.Color.orange()))
        finally:
            _ACTIVE_TRIVIA.pop(i.channel.id, None)
            
        if i.guild: await self._log(i.guild.id, "trivia")

    @fun.command(name="rate", description="Rate anything /10.")
    async def rate(self, i: discord.Interaction, thing: str):
        sc = (sum(ord(c) for c in thing.lower() + str(i.guild_id or 0))) % 11
        bar = "█"*sc + "░"*(10-sc)
        await i.response.send_message(embed=discord.Embed(title=f"⭐ {thing}", description=f"**{sc}/10**\n`[{bar}]`", color=discord.Color(0xF1C40F)))
        if i.guild: await self._log(i.guild.id, "rate")

    @fun.command(name="ship", description="Ship two people.")
    async def ship(self, i: discord.Interaction, person1: discord.Member, person2: discord.Member):
        sc = abs(hash(f"{min(person1.id,person2.id)}-{max(person1.id,person2.id)}")) % 101
        bar = "💗"*(sc//10) + "🖤"*(10-sc//10)
        mood = "💞 Soulmates!" if sc>=80 else "💕 Great match!" if sc>=60 else "💛 Potential!" if sc>=40 else "🤔 Uncertain…" if sc>=20 else "💔 Not meant to be."
        n1, n2 = person1.display_name, person2.display_name
        sn = n1[:len(n1)//2] + n2[len(n2)//2:]
        e = discord.Embed(title=f"💘 {n1} × {n2}", description=f"**Ship name:** {sn}\n**{sc}%** compatibility\n`{bar}`\n\n{mood}", color=discord.Color(0xFF69B4))
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "ship")

    @fun.command(name="iq", description="Fake IQ test.")
    async def iq(self, i: discord.Interaction, member: discord.Member=None):
        t = member or i.user; sc = max(1, (t.id + (i.guild_id or 0)) % 201)
        v = "🧠 Absolute genius!" if sc>=160 else "💡 Very intelligent." if sc>=130 else "📚 Above average." if sc>=110 else "🙂 Average." if sc>=90 else "🤷 Below average." if sc>=70 else "🪨 Room-temperature IQ."
        e = discord.Embed(title=f"🧠 IQ: {t.display_name}", description=f"**Score: `{sc}`**\n\n{v}", color=discord.Color(0x3498DB))
        e.set_thumbnail(url=t.display_avatar.url); e.set_footer(text="Comedy only!")
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "iq")

    @fun.command(name="howgay", description="Gay-o-meter (comedy).")
    async def howgay(self, i: discord.Interaction, member: discord.Member=None):
        t = member or i.user; p = (t.id * 7 + 42) % 101
        bar = "🏳️‍🌈"*(p//10) + "⬜"*(10-p//10)
        e = discord.Embed(title=f"🏳️‍🌈 {t.display_name}", description=f"**{p}% gay**\n{bar}", color=discord.Color(0xFF69B4))
        e.set_footer(text="Comedy only!")
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "howgay")

    @fun.command(name="pp", description="PP size (comedy, SFW).")
    async def pp(self, i: discord.Interaction, member: discord.Member=None):
        t = member or i.user; sz = (t.id + (i.guild_id or 0)) % 20
        e = discord.Embed(title=f"📏 PP: {t.display_name}", description=f"`8{'='*sz}D`\n\n**{sz} inches** (allegedly)", color=discord.Color(0xF39C12))
        e.set_footer(text="Comedy only!")
        await i.response.send_message(embed=e)
        if i.guild: await self._log(i.guild.id, "pp")


async def setup(bot):
    await bot.add_cog(Fun(bot))
