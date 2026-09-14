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
        params = {
            'q': query,
            'limit': 1,
            'fields': 'id,title,alternative_titles,main_picture,synopsis,mean,rank,popularity,num_episodes,average_episode_duration,status,start_season,media_type,source,start_date,end_date,genres,studios'
        }
        
        # MAKE SURE THIS MATCHES YOUR DASHBOARD VARIABLE NAME
        client_id = os.getenv("MAL_CLIENT_ID") 
        
        if not client_id:
            return await ctx.send("❌ **Configuration Error:** API key is missing. Check your environment variables.")

        client_id = client_id.strip()

        headers = {
            "X-MAL-CLIENT-ID": client_id,
            "User-Agent": "Recluse Discord Bot"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        # Exposing HTTP errors (like 403 Forbidden or 400 Bad Request)
                        error_data = await response.text()
                        return await ctx.send(f"❌ **API Error {response.status}:** `{error_data}`")
                        
                    search_result = await response.json()

            data_list = search_result.get('data', [])
            if not data_list:
                return await ctx.send("❌ Query yielded no results from the MyAnimeList database.")
                
            node = data_list[0].get('node', {})
            mal_id = node.get('id')
            
            title = node.get('title', 'Unknown Title')
            
            # Safely handling potential null dicts from the API
            alt_titles = node.get('alternative_titles') or {}
            japanese_title = alt_titles.get('ja', 'N/A')
            site_url = f"https://myanimelist.net/anime/{mal_id}" if mal_id else None
            
            synopsis = self.clean_html(node.get('synopsis'))
            synopsis = synopsis[:2048] + '...' if len(synopsis) > 2048 else synopsis

            status = node.get('status', 'Unknown').replace('_', ' ').title()
            
            season_data = node.get('start_season') or {}
            season_str = f"{season_data.get('season', '').title()} {season_data.get('year', '')}".strip() or "Unknown"

            media_type = node.get('media_type', 'Unknown')
            media_type = media_type.upper() if media_type in ['tv', 'ova', 'ona'] else media_type.title()
            
            duration_secs = node.get('average_episode_duration') or 0
            duration_mins = duration_secs // 60 if duration_secs else 'Unknown'
            
            stats_block = (
                f"📺 **Type:** {media_type}\n"
                f"🎬 **Episodes:** {node.get('num_episodes', 'Unknown')}\n"
                f"⏳ **Duration:** {duration_mins} mins\n"
                f"⭐ **Score:** {node.get('mean', 'N/A')}\n"
                f"📈 **Rank:** #{node.get('rank', 'N/A')}\n"
                f"🔥 **Popularity:** #{node.get('popularity', 'N/A')}\n"
                f"📚 **Source:** {node.get('source', 'Unknown').replace('_', ' ').title()}"
            )

            dates_block = f"**Start:** {node.get('start_date', 'Unknown')}\n**End:** {node.get('end_date', 'Unknown')}"
            
            genres_data = node.get('genres') or []
            genres_list = [g['name'] for g in genres_data]
            genres_block = ", ".join(genres_list) if genres_list else "None"
            
            studios_data = node.get('studios') or []
            studios_list = [s['name'] for s in studios_data]
            studios_block = ", ".join(studios_list) if studios_list else "None"

            embed = discord.Embed(title=title, url=site_url, description=synopsis, color=0x3498db) 
            
            embed.add_field(name="Japanese Title", value=japanese_title, inline=False)
            embed.add_field(name="Broadcast", value=f"**Status:** {status}\n**Season:** {season_str}", inline=False)
            embed.add_field(name="Statistics", value=stats_block, inline=False)
            
            embed.add_field(name="Dates", value=dates_block, inline=True)
            embed.add_field(name="Genres", value=genres_block, inline=True)
            embed.add_field(name="Studios", value=studios_block, inline=False)

            pictures = node.get('main_picture') or {}
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            if pictures.get('large'):
                embed.set_image(url=pictures['large'])
            elif pictures.get('medium'):
                embed.set_image(url=pictures['medium'])
                
            embed.set_footer(text="Powered by MyAnimeList API")

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "anime")
            
        except Exception as e:
            # THIS WILL PRINT THE REAL ERROR TO DISCORD
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
                
            embed.set_footer(text="Powered by MyAnimeList API")

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "manga")
            
        except Exception as e:
            # 🛠️ FIXED: Exposing real crash errors to Discord
            import traceback
            print(traceback.format_exc())
            await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")

async def setup(bot):
    await bot.add_cog(Anime(bot))
