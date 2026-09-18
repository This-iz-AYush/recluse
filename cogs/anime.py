import discord
from discord.ext import commands
import aiohttp
import datetime
import os
import re
from dotenv import load_dotenv

# Load environment variables directly in the cog
load_dotenv()

class Anime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def log_telemetry(self, guild_id: int, command_name: str):
        if hasattr(self.bot, 'db'):
            await self.bot.db.command_telemetry.update_one(
                {"guild_id": guild_id, "command": command_name, "date": datetime.datetime.utcnow().strftime('%Y-%m-%d')},
                {"$inc": {"uses": 1}},
                upsert=True
            )

    # --- 🛡️ GATEKEEPER CHECK ---
    async def cog_check(self, ctx):
        if hasattr(self.bot, 'db'):
            is_blacklisted = await self.bot.db.global_blacklist.find_one({"target_id": ctx.author.id, "type": "user"})
            if is_blacklisted:
                try: await ctx.send("❌ **Access Denied:** You have been permanently blacklisted from the Recluse network.", ephemeral=True)
                except Exception: pass
                return False
                
        if not ctx.guild: return True
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": ctx.guild.id})
            if settings and settings.get("anime_enabled", True) is False:
                await ctx.send("❌ The **Anime** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    def clean_html(self, raw_html):
        """Strips <br>, <i>, and other HTML tags from descriptions."""
        if not raw_html:
            return "No synopsis available."
        return re.sub(r'<[^>]+>', '', raw_html).strip()

    @commands.hybrid_command(
        name="anime", 
        description="Queries the MyAnimeList database for anime.",
        usage="/anime <query>",
        help="/anime attack on titan"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anime(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        url = 'https://api.myanimelist.net/v2/anime'
        # Added extra fields to match the requested layout (rating, broadcast, num_list_users, etc.)
        params = {
            'q': query,
            'limit': 1,
            'fields': 'id,title,alternative_titles,main_picture,synopsis,mean,rank,popularity,num_episodes,average_episode_duration,status,start_season,media_type,source,start_date,end_date,genres,studios,rating,broadcast,num_list_users,num_scoring_users'
        }
        
        client_id = os.getenv("MAL_CLIENT_ID") 
        
        if not client_id:
            return await ctx.send("❌ **Configuration Error:** API key is missing. Check your environment variables.")

        headers = {
            "X-MAL-CLIENT-ID": client_id.strip(),
            "User-Agent": "Recluse Discord Bot"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        error_data = await response.text()
                        return await ctx.send(f"❌ **API Error {response.status}:** `{error_data}`")
                        
                    search_result = await response.json()

            data_list = search_result.get('data', [])
            if not data_list:
                return await ctx.send("❌ Query yielded no results from the MyAnimeList database.")
                
            node = data_list[0].get('node', {})
            mal_id = node.get('id')
            title = node.get('title', 'Unknown Title')
            
            # --- Data Extraction & Formatting ---
            def code_fmt(val):
                """Helper to wrap values in backticks or return empty backticks if missing."""
                return f"`{val}`" if val and str(val).strip() else "``"

            # Titles & Synonyms
            alt_titles = node.get('alternative_titles', {})
            en_title = alt_titles.get('en', '')
            ja_title = alt_titles.get('ja', '')
            synonyms = ", ".join(alt_titles.get('synonyms', []))
            
            # Dates & Broadcast
            season_data = node.get('start_season', {})
            premiered = f"{season_data.get('season', '').title()} {season_data.get('year', '')}".strip()
            broadcast = node.get('broadcast', {}).get('day_of_the_week', '').title()
            aired = f"{node.get('start_date', '')}"
            
            # Stats & Details
            genres = ", ".join([g['name'] for g in node.get('genres', [])])
            media_type = node.get('media_type', 'Unknown').upper() if node.get('media_type') in ['tv', 'ova', 'ona'] else node.get('media_type', '').title()
            episodes = node.get('num_episodes', '')
            rating = node.get('rating', '').replace('_', ' ').title()
            score = node.get('mean', '')
            ranked = f"#{node.get('rank')}" if node.get('rank') else ""
            popularity = f"#{node.get('popularity')}" if node.get('popularity') else ""
            
            # Duration calculation
            dur_secs = node.get('average_episode_duration', 0)
            dur_hours, dur_mins = divmod(dur_secs // 60, 60)
            duration = f"{dur_hours} hr. {dur_mins} min." if dur_hours > 0 else f"{dur_mins} min." if dur_mins > 0 else ""
            
            studios = ", ".join([s['name'] for s in node.get('studios', [])])
            members = f"{node.get('num_list_users', 0):,}"
            score_stats = f"scored by {node.get('num_scoring_users', 0):,} users"
            source = node.get('source', '').replace('_', ' ').title()
            status = node.get('status', '').replace('_', ' ').title()
            link = f"https://myanimelist.net/anime/{mal_id}/{title.replace(' ', '_')}" if mal_id else ""

            # --- Embed Construction ---
            # Using the bright green color from the screenshot
            embed = discord.Embed(title=f"My Anime List search result for {query}", color=0x2ecc71)
            
            # Setting Thumbnail to top right
            pictures = node.get('main_picture', {})
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            elif pictures.get('large'):
                embed.set_thumbnail(url=pictures['large'])

            # Row 1
            embed.add_field(name="Premiered", value=code_fmt(premiered), inline=True)
            embed.add_field(name="Broadcast", value=code_fmt(broadcast), inline=True)
            embed.add_field(name="Genres", value=code_fmt(genres), inline=True)
            
            # Row 2
            embed.add_field(name="English Title", value=code_fmt(en_title), inline=True)
            embed.add_field(name="Japanese Title", value=code_fmt(ja_title), inline=True)
            embed.add_field(name="Type", value=code_fmt(media_type), inline=True)
            
            # Row 3
            embed.add_field(name="Episodes", value=code_fmt(episodes), inline=True)
            embed.add_field(name="Rating", value=code_fmt(rating), inline=True)
            embed.add_field(name="Aired", value=code_fmt(aired), inline=True)
            
            # Row 4 (Favorite might require a different endpoint, using N/A as placeholder if missing)
            embed.add_field(name="Score", value=code_fmt(score), inline=True)
            embed.add_field(name="Favorite", value=code_fmt(""), inline=True) 
            embed.add_field(name="Ranked", value=code_fmt(ranked), inline=True)
            
            # Row 5
            embed.add_field(name="Duration", value=code_fmt(duration), inline=True)
            embed.add_field(name="Studios", value=code_fmt(studios), inline=True)
            embed.add_field(name="Popularity", value=code_fmt(popularity), inline=True)
            
            # Row 6
            embed.add_field(name="Members", value=code_fmt(members), inline=True)
            embed.add_field(name="Score Stats", value=code_fmt(score_stats), inline=True)
            embed.add_field(name="Source", value=code_fmt(source), inline=True)
            
            # Row 7
            embed.add_field(name="Synonyms", value=code_fmt(synonyms), inline=True)
            embed.add_field(name="Status", value=code_fmt(status), inline=True)
            embed.add_field(name="Identifier", value=code_fmt(mal_id), inline=True)
            
            # Link at the bottom (inline=False so it sits on its own row)
            if link:
                embed.add_field(name="Link", value=link, inline=False)

            # Footer layout matching screenshot
            current_time = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name} • {current_time}", 
                icon_url=ctx.author.display_avatar.url
            )

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "anime")
            
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")
    @commands.hybrid_command(
        name="manga", 
        description="Queries the MyAnimeList database for textual publication data.",
        usage="/manga <query>",
        help="/manga berserk"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def manga(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        url = 'https://api.myanimelist.net/v2/manga'
        params = {
            'q': query,
            'limit': 1,
            'fields': 'id,title,alternative_titles,main_picture,synopsis,mean,rank,popularity,num_chapters,num_volumes,status,media_type,start_date,end_date,genres,authors'
        }
        
        client_id = os.getenv("MAL_CLIENT_ID")
        
        if not client_id:
            return await ctx.send("❌ **Configuration Error:** MAL_CLIENT_ID is missing from the .env file.")

        client_id = client_id.strip()

        headers = {
            "X-MAL-CLIENT-ID": client_id,
            "User-Agent": "Recluse Discord Bot"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        error_data = await response.text()
                        return await ctx.send(f"❌ **API Error {response.status}:** `{error_data}`")
                        
                    search_result = await response.json()

            data_list = search_result.get('data', [])
            if not data_list:
                return await ctx.send("❌ Query yielded no results from the MyAnimeList database.")
                
            node = data_list[0].get('node', {})
            mal_id = node.get('id')
            
            title = node.get('title', 'Unknown Title')
            
            # 🛠️ FIXED: Safe dictionary fallback for titles
            alt_titles = node.get('alternative_titles') or {}
            japanese_title = alt_titles.get('ja', 'N/A')
            site_url = f"https://myanimelist.net/manga/{mal_id}" if mal_id else None
            
            synopsis = self.clean_html(node.get('synopsis'))
            synopsis = synopsis[:2048] + '...' if len(synopsis) > 2048 else synopsis

            status = node.get('status', 'Unknown').replace('_', ' ').title()
            media_type = node.get('media_type', 'Unknown').title()
            
            stats_block = (
                f"📖 **Type:** {media_type}\n"
                f"📑 **Chapters:** {node.get('num_chapters', 'Unknown')}\n"
                f"📚 **Volumes:** {node.get('num_volumes', 'Unknown')}\n"
                f"⭐ **Score:** {node.get('mean', 'N/A')}\n"
                f"📈 **Rank:** #{node.get('rank', 'N/A')}\n"
                f"🔥 **Popularity:** #{node.get('popularity', 'N/A')}"
            )

            dates_block = f"**Start:** {node.get('start_date', 'Unknown')}\n**End:** {node.get('end_date', 'Unknown')}"
            
            # 🛠️ FIXED: Safe list fallback for genres
            genres_data = node.get('genres') or []
            genres_list = [g.get('name', 'Unknown') for g in genres_data]
            genres_block = ", ".join(genres_list) if genres_list else "None"
            
            # 🛠️ FIXED: Safe parsing for authors (handles missing names gracefully)
            authors_data = node.get('authors') or []
            authors_list = []
            for a in authors_data:
                author_node = a.get('node') or {}
                first = author_node.get('first_name') or ''
                last = author_node.get('last_name') or ''
                authors_list.append(f"{first} {last}".strip())
            authors_block = ", ".join(authors_list) if authors_list else "None"

            embed = discord.Embed(title=title, url=site_url, description=synopsis, color=0x2ecc71) 
            
            embed.add_field(name="Japanese Title", value=japanese_title, inline=False)
            embed.add_field(name="Status", value=f"**Publishing:** {status}", inline=False)
            embed.add_field(name="Statistics", value=stats_block, inline=False)
            
            embed.add_field(name="Dates", value=dates_block, inline=True)
            embed.add_field(name="Genres", value=genres_block, inline=True)
            
            embed.add_field(name="Authors", value=authors_block, inline=False)

            # 🛠️ FIXED: Safe dictionary fallback for pictures
            pictures = node.get('main_picture') or {}
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            if pictures.get('large'):
                embed.set_image(url=pictures['large'])
            elif pictures.get('medium'):
                embed.set_image(url=pictures['medium'])
                
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name} • Powered by MyAnimeList API", 
                icon_url=ctx.author.display_avatar.url
            )

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "manga")
            
        except Exception as e:
            # 🛠️ FIXED: Exposing real crash errors to Discord
            import traceback
            print(traceback.format_exc())
            await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")

async def setup(bot):
    await bot.add_cog(Anime(bot))
