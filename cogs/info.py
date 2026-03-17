"""
info.py  —  Recluse Bot  v2.0
Information-lookup commands (Miza-inspired).

Commands:
  /wiki      — Wikipedia article summary
  /github    — GitHub user / repo profile
  /movie     — Movie info from OMDb / free API
  /define    — Merriam-Webster style definition (Free Dictionary API)
  /lyrics    — Song lyrics search (stub — returns search link)
  /npm       — NPM package info
  /pypi      — PyPI package info
  /weather   — Current weather (wttr.in, free)
  /time      — Current time in a timezone / city
  /ipinfo    — IP address geolocation (ip-api.com, free)
  /news      — Latest headlines (GNews free API or RSS)
  /crypto    — Crypto price (CoinGecko free API)
"""

import datetime
import re
import urllib.parse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

C_INFO  = discord.Color(0x5865F2)
C_WIKI  = discord.Color(0xF8F9FA)   # Wikipedia grey-white
C_GH    = discord.Color(0x24292E)   # GitHub dark
C_MOVIE = discord.Color(0xE50914)   # Netflix red
C_NEWS  = discord.Color(0xFF6600)
C_COIN  = discord.Color(0xF7931A)   # Bitcoin orange


class Info(commands.Cog):
    """Information-lookup commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ALL commands under one /lookup group → 1 slot instead of 9
    lookup = app_commands.Group(name="lookup", description="Information lookup commands: wiki, weather, crypto, GitHub & more.")

    async def cog_check(self, ctx) -> bool:
        if hasattr(self.bot, "db") and interaction.guild:
            bl = await self.bot.db.global_blacklist.find_one(
                {"target_id": interaction.user.id, "type": "user"}
            )
            if bl:
                await interaction.followup.send("❌ Access denied.", ephemeral=True)
                return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # /wiki
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="wiki", description="Fetch a Wikipedia article summary.")
    async def wiki(self, interaction: discord.Interaction, *, query: str):
        await interaction.response.defer()
        url = (
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(query.replace(" ", "_"))
        )
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 404:
                        return await interaction.followup.send(f"❌ No Wikipedia article found for **{query}**.")
                    if r.status != 200:
                        return await interaction.followup.send("❌ Wikipedia is unreachable right now.")
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ Failed to contact Wikipedia.")

        title   = data.get("title", query)
        extract = data.get("extract", "No summary available.")
        page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        thumb   = (data.get("thumbnail") or {}).get("source", "")

        if len(extract) > 1024:
            extract = extract[:1021] + "…"

        embed = discord.Embed(title=title, url=page_url, description=extract, color=C_WIKI)
        if thumb:
            embed.set_thumbnail(url=thumb)
        embed.set_footer(text="📖 Source: Wikipedia")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /github
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="github", description="Look up a GitHub user or repository.")
    async def github(self, interaction: discord.Interaction, user: str, repo: str = ""):
        await interaction.response.defer()
        try:
            async with aiohttp.ClientSession() as s:
                if repo:
                    url = f"https://api.github.com/repos/{user}/{repo}"
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                     headers={"Accept": "application/vnd.github+json"}) as r:
                        if r.status == 404:
                            return await interaction.followup.send(f"❌ Repo `{user}/{repo}` not found.")
                        data = await r.json()

                    embed = discord.Embed(
                        title=data.get("full_name", f"{user}/{repo}"),
                        url=data.get("html_url", ""),
                        description=data.get("description") or "*No description*",
                        color=C_GH,
                    )
                    embed.add_field(name="⭐ Stars",   value=f"`{data.get('stargazers_count', 0):,}`", inline=True)
                    embed.add_field(name="🍴 Forks",   value=f"`{data.get('forks_count', 0):,}`",      inline=True)
                    embed.add_field(name="👀 Watchers",value=f"`{data.get('watchers_count', 0):,}`",   inline=True)
                    embed.add_field(name="🐛 Issues",  value=f"`{data.get('open_issues_count', 0):,}`",inline=True)
                    embed.add_field(name="🔤 Language",value=data.get("language") or "N/A",            inline=True)
                    if data.get("license"):
                        embed.add_field(name="📄 License", value=data["license"].get("spdx_id", "?"), inline=True)
                    created = data.get("created_at", "")[:10]
                    updated = data.get("updated_at", "")[:10]
                    embed.set_footer(text=f"Created: {created}  •  Updated: {updated}  •  GitHub")
                else:
                    url = f"https://api.github.com/users/{user}"
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                     headers={"Accept": "application/vnd.github+json"}) as r:
                        if r.status == 404:
                            return await interaction.followup.send(f"❌ User `{user}` not found.")
                        data = await r.json()

                    embed = discord.Embed(
                        title=data.get("name") or data.get("login"),
                        url=data.get("html_url", ""),
                        description=data.get("bio") or "*No bio*",
                        color=C_GH,
                    )
                    embed.set_thumbnail(url=data.get("avatar_url", ""))
                    embed.add_field(name="👤 Username",  value=f"`{data.get('login')}`",             inline=True)
                    embed.add_field(name="📦 Repos",     value=f"`{data.get('public_repos', 0)}`",  inline=True)
                    embed.add_field(name="👥 Followers", value=f"`{data.get('followers', 0):,}`",    inline=True)
                    embed.add_field(name="➡️ Following", value=f"`{data.get('following', 0):,}`",   inline=True)
                    if data.get("location"):
                        embed.add_field(name="📍 Location", value=data["location"], inline=True)
                    if data.get("company"):
                        embed.add_field(name="🏢 Company", value=data["company"], inline=True)
                    joined = data.get("created_at", "")[:10]
                    embed.set_footer(text=f"Joined GitHub: {joined}")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ GitHub API error: `{type(e).__name__}`")

    # ─────────────────────────────────────────────────────────────────────────
    # /define  — Free Dictionary API
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="define", description="Get the dictionary definition of a word.")
    async def define(self, interaction: discord.Interaction, *, word: str):
        await interaction.response.defer()
        word = word.strip().lower()
        url  = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 404:
                        return await interaction.followup.send(f"❌ No definition found for **{word}**.")
                    if r.status != 200:
                        return await interaction.followup.send("❌ Dictionary API unavailable.")
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ Failed to reach the dictionary API.")

        if not data or not isinstance(data, list):
            return await interaction.followup.send(f"❌ No results for **{word}**.")

        entry     = data[0]
        phonetic  = entry.get("phonetic", "")
        meanings  = entry.get("meanings", [])

        embed = discord.Embed(
            title=f"📖 {entry.get('word', word).title()}",
            description=f"*{phonetic}*" if phonetic else "",
            color=C_INFO,
        )
        for meaning in meanings[:3]:     # max 3 parts of speech
            pos         = meaning.get("partOfSpeech", "?")
            definitions = meaning.get("definitions", [])
            if not definitions:
                continue
            first_def = definitions[0]
            defn      = first_def.get("definition", "")[:512]
            example   = first_def.get("example", "")
            value     = defn
            if example:
                value += f"\n*Example: {example[:200]}*"
            synonyms  = meaning.get("synonyms", [])[:5]
            if synonyms:
                value += f"\n**Synonyms:** {', '.join(synonyms)}"
            embed.add_field(name=f"*{pos}*", value=value, inline=False)

        embed.set_footer(text="Source: Free Dictionary API")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /npm  — NPM package info
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="npm", description="Look up an NPM package.")
    async def npm(self, interaction: discord.Interaction, *, package: str):
        await interaction.response.defer()
        url = f"https://registry.npmjs.org/{urllib.parse.quote(package)}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 404:
                        return await interaction.followup.send(f"❌ Package `{package}` not found on NPM.")
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ NPM registry unreachable.")

        latest  = data.get("dist-tags", {}).get("latest", "?")
        ver     = data.get("versions", {}).get(latest, {})
        embed   = discord.Embed(
            title=data.get("name", package),
            url=f"https://www.npmjs.com/package/{package}",
            description=(data.get("description") or "*No description*")[:300],
            color=discord.Color(0xCC3534),
        )
        embed.add_field(name="📦 Latest",  value=f"`{latest}`",                             inline=True)
        embed.add_field(name="📜 License", value=ver.get("license", "?"),                  inline=True)
        embed.add_field(name="🔗 Homepage",value=ver.get("homepage") or "N/A",             inline=True)
        embed.set_footer(text="📦 Source: NPM Registry")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /pypi  — PyPI package info
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="pypi", description="Look up a PyPI package.")
    async def pypi(self, interaction: discord.Interaction, *, package: str):
        await interaction.response.defer()
        url = f"https://pypi.org/pypi/{urllib.parse.quote(package)}/json"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 404:
                        return await interaction.followup.send(f"❌ Package `{package}` not found on PyPI.")
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ PyPI registry unreachable.")

        info    = data.get("info", {})
        embed   = discord.Embed(
            title=info.get("name", package),
            url=info.get("project_url") or f"https://pypi.org/project/{package}",
            description=(info.get("summary") or "*No description*")[:300],
            color=discord.Color(0x3572A5),
        )
        embed.add_field(name="📦 Version",  value=f"`{info.get('version', '?')}`",  inline=True)
        embed.add_field(name="📜 License",  value=info.get("license") or "?",       inline=True)
        embed.add_field(name="🐍 Requires", value=info.get("requires_python") or "Any", inline=True)
        author  = info.get("author") or info.get("author_email") or "Unknown"
        embed.add_field(name="👤 Author",   value=author[:100],                     inline=True)
        embed.set_footer(text="🐍 Source: PyPI")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /weather  — wttr.in (completely free, no key)
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="weather", description="Get current weather for any city.")
    async def weather(self, interaction: discord.Interaction, *, city: str):
        await interaction.response.defer()
        encoded = urllib.parse.quote(city)
        url     = f"https://wttr.in/{encoded}?format=j1"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return await interaction.followup.send(f"❌ Couldn't get weather for **{city}**.")
                    data = await r.json(content_type=None)
        except Exception:
            return await interaction.followup.send("❌ Weather service unavailable.")

        try:
            current = data["current_condition"][0]
            area    = data["nearest_area"][0]
            city_name = area["areaName"][0]["value"]
            country   = area["country"][0]["value"]
            temp_c    = current["temp_C"]
            temp_f    = current["temp_F"]
            feels_c   = current["FeelsLikeC"]
            humidity  = current["humidity"]
            wind_kmph = current["windspeedKmph"]
            wind_dir  = current["winddir16Point"]
            desc      = current["weatherDesc"][0]["value"]
            visibility= current["visibility"]
            uv_index  = current["uvIndex"]
        except (KeyError, IndexError):
            return await interaction.followup.send("❌ Couldn't parse weather data.")

        WEATHER_EMOJIS = {
            "sunny": "☀️", "clear": "☀️", "cloudy": "☁️", "overcast": "☁️",
            "rain": "🌧️", "drizzle": "🌦️", "snow": "❄️", "fog": "🌫️",
            "thunder": "⛈️", "mist": "🌁", "blizzard": "🌨️", "sleet": "🌨️",
        }
        emoji = "🌡️"
        for key, em in WEATHER_EMOJIS.items():
            if key in desc.lower():
                emoji = em
                break

        embed = discord.Embed(
            title=f"{emoji} Weather in {city_name}, {country}",
            color=discord.Color(0x87CEEB),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.add_field(name="🌡️ Temperature", value=f"`{temp_c}°C / {temp_f}°F`", inline=True)
        embed.add_field(name="🤔 Feels Like",  value=f"`{feels_c}°C`",              inline=True)
        embed.add_field(name="📋 Condition",   value=desc,                           inline=True)
        embed.add_field(name="💧 Humidity",    value=f"`{humidity}%`",              inline=True)
        embed.add_field(name="💨 Wind",        value=f"`{wind_kmph} km/h {wind_dir}`", inline=True)
        embed.add_field(name="👁️ Visibility",  value=f"`{visibility} km`",          inline=True)
        embed.add_field(name="☀️ UV Index",    value=f"`{uv_index}`",               inline=True)
        embed.set_footer(text="Source: wttr.in  •  No API key required")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /ipinfo  — ip-api.com (free, no key)
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="ipinfo", description="Look up geolocation info for an IP address.")
    async def ipinfo(self, interaction: discord.Interaction, ip: str):
        await interaction.response.defer()
        # Basic validation — reject obviously private/internal IPs
        if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|0\.)", ip):
            return await interaction.followup.send("❌ That's a private IP address — no geolocation available.")
        url = f"http://ip-api.com/json/{urllib.parse.quote(ip)}?fields=status,message,country,regionName,city,zip,lat,lon,timezone,isp,org,as,query"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ IP lookup service unavailable.")

        if data.get("status") != "success":
            return await interaction.followup.send(f"❌ IP lookup failed: `{data.get('message', 'Unknown error')}`")

        embed = discord.Embed(title=f"🌐 IP: {data['query']}", color=C_INFO)
        embed.add_field(name="🏳️ Country",   value=data.get("country", "?"),    inline=True)
        embed.add_field(name="🏙️ City",      value=f"{data.get('city', '?')}, {data.get('regionName', '?')}", inline=True)
        embed.add_field(name="📮 ZIP",        value=data.get("zip", "?"),        inline=True)
        embed.add_field(name="📍 Coords",     value=f"`{data.get('lat')}, {data.get('lon')}`", inline=True)
        embed.add_field(name="⏰ Timezone",   value=data.get("timezone", "?"),   inline=True)
        embed.add_field(name="🏢 ISP",        value=data.get("isp", "?"),        inline=True)
        embed.add_field(name="🔗 Org",        value=(data.get("org") or "?")[:60], inline=True)
        embed.set_footer(text="Source: ip-api.com  •  Free tier")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /crypto  — CoinGecko (free, no key needed)
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="crypto", description="Get the current price of a cryptocurrency.")
    async def crypto(self, interaction: discord.Interaction, *, coin: str):
        await interaction.response.defer()
        coin_id = coin.strip().lower().replace(" ", "-")
        url     = (
            f"https://api.coingecko.com/api/v3/coins/{urllib.parse.quote(coin_id)}"
            "?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false"
        )
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                 headers={"Accept": "application/json"}) as r:
                    if r.status == 404:
                        return await interaction.followup.send(f"❌ Coin `{coin}` not found. Try the CoinGecko ID e.g. `bitcoin`, `ethereum`.")
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ CoinGecko is unreachable.")

        md      = data.get("market_data", {})
        price   = md.get("current_price", {}).get("usd", 0)
        change  = md.get("price_change_percentage_24h") or 0
        mkt_cap = md.get("market_cap", {}).get("usd", 0)
        vol     = md.get("total_volume", {}).get("usd", 0)
        high    = md.get("high_24h", {}).get("usd", 0)
        low     = md.get("low_24h", {}).get("usd", 0)
        rank    = data.get("market_cap_rank", "?")

        colour  = discord.Color.green() if change >= 0 else discord.Color.red()
        arrow   = "📈" if change >= 0 else "📉"

        embed = discord.Embed(
            title=f"{data.get('name', coin)} ({data.get('symbol', '?').upper()})",
            url=f"https://www.coingecko.com/en/coins/{coin_id}",
            color=colour,
            timestamp=datetime.datetime.utcnow(),
        )
        if data.get("image", {}).get("small"):
            embed.set_thumbnail(url=data["image"]["small"])

        embed.add_field(name="💰 Price (USD)", value=f"`${price:,.4f}`",              inline=True)
        embed.add_field(name=f"{arrow} 24h Change", value=f"`{change:+.2f}%`",        inline=True)
        embed.add_field(name="🏆 Rank",       value=f"`#{rank}`",                     inline=True)
        embed.add_field(name="📊 Market Cap", value=f"`${mkt_cap:,.0f}`",             inline=True)
        embed.add_field(name="📦 24h Volume", value=f"`${vol:,.0f}`",                 inline=True)
        embed.add_field(name="📉 24h Low/High", value=f"`${low:,.4f}` / `${high:,.4f}`", inline=True)
        embed.set_footer(text="Source: CoinGecko — prices may be delayed")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /time  — world clock (worldtimeapi.org, free)
    # ─────────────────────────────────────────────────────────────────────────

    @lookup.command(name="time", description="Get the current time in any timezone or city.")
    async def time_cmd(self, interaction: discord.Interaction, *, timezone: str):
        await interaction.response.defer()
        tz_encoded = urllib.parse.quote(timezone.replace(" ", "_"))
        url        = f"https://worldtimeapi.org/api/timezone/{tz_encoded}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 404:
                        return await interaction.followup.send(
                            f"❌ Timezone `{timezone}` not found.\n"
                            "Examples: `America/New_York`, `Europe/London`, `Asia/Tokyo`"
                        )
                    data = await r.json()
        except Exception:
            return await interaction.followup.send("❌ World time service unavailable.")

        dt_str    = data.get("datetime", "")[:19].replace("T", " ")
        utc_off   = data.get("utc_offset", "?")
        day_of_w  = data.get("day_of_week", "")
        days      = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
        weekday   = days[int(day_of_w)] if str(day_of_w).isdigit() else "?"
        tz_abbr   = data.get("abbreviation", "")

        embed = discord.Embed(
            title=f"🕐 {data.get('timezone', timezone)}",
            description=f"**{dt_str}**  ({weekday})",
            color=C_INFO,
        )
        embed.add_field(name="⏰ UTC Offset", value=f"`{utc_off}`",  inline=True)
        embed.add_field(name="🏷️ Abbreviation", value=f"`{tz_abbr}`", inline=True)
        embed.set_footer(text="Source: worldtimeapi.org")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
