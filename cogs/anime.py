import discord
from discord.ext import commands
import aiohttp
import datetime
import urllib.parse
import asyncio

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
        
        encoded_query = urllib.parse.quote(query)
        url = f"https://api.jikan.moe/v4/anime?q={encoded_query}&sfw=true"
        
        # This header is the magic key to bypass Cloudflare 504 errors
        headers = {
            "User-Agent": "Recluse Discord Bot (Created by AYush)"
        }
        
        search_result = None
        try:
            async with aiohttp.ClientSession() as session:
                for attempt in range(3):
                    async with session.get(url, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            search_result = await response.json()
                            break
                        elif response.status >= 500:
                            if attempt == 2:
                                return await ctx.send(f"❌ **API Error:** The database returned a {response.status} status. The API is temporarily down.")
                            await asyncio.sleep(2)
                        else:
                            return await ctx.send(f"❌ **API Error:** The database returned a {response.status} status.")
                    
            if not search_result or not search_result.get('data'):
                return await ctx.send("❌ Query yielded no results from the external database.")
                
            data = search_result['data'][0]
            embed = discord.Embed(title=data.get('title', 'Unknown Title'), url=data.get('url'), color=discord.Color.red())
            
            # Clean up missing synopsis
            synopsis = data.get('synopsis')
            if synopsis:
                # Some MAL entries have a "[Written by MAL Rewrite]" tag, you can optionally strip it, but standard truncating works well
                embed.description = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis
            else:
                embed.description = 'No synopsis available in database.'
            
            try: embed.set_image(url=data['images']['jpg']['large_image_url'])
            except (KeyError, TypeError): pass
            
            # MAL scores are natively out of 10
            score = data.get('score')
            embed.add_field(name="Community Score", value=f"{score}/10" if score else 'N/A')
            embed.add_field(name="Total Episodes", value=str(data.get('episodes', 'N/A')))
            embed.add_field(name="Broadcast Status", value=str(data.get('status', 'Unknown')))
            
            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "anime")
            
        except Exception as e:
            if hasattr(self.bot, 'db') and ctx.guild:
                await self.bot.db.system_health.insert_one({"guild_id": ctx.guild.id, "module": "Anime_API", "error": type(e).__name__, "timestamp": datetime.datetime.utcnow().timestamp()})
            await ctx.send("❌ **API Timeout:** The MyAnimeList database is currently unreachable.")

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
        
        encoded_query = urllib.parse.quote(query)
        url = f"https://api.jikan.moe/v4/manga?q={encoded_query}&sfw=true"
        
        headers = {
            "User-Agent": "Recluse Discord Bot (Created by AYush)"
        }
        
        search_result = None
        try:
            async with aiohttp.ClientSession() as session:
                for attempt in range(3):
                    async with session.get(url, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            search_result = await response.json()
                            break
                        elif response.status >= 500:
                            if attempt == 2:
                                return await ctx.send(f"❌ **API Error:** The database returned a {response.status} status. The API is temporarily down.")
                            await asyncio.sleep(2)
                        else:
                            return await ctx.send(f"❌ **API Error:** The database returned a {response.status} status.")

            if not search_result or not search_result.get('data'): 
                return await ctx.send("❌ Query yielded no results.")
                
            data = search_result['data'][0]
            embed = discord.Embed(title=data.get('title', 'Unknown Title'), url=data.get('url'), color=discord.Color.green())
            
            synopsis = data.get('synopsis')
            if synopsis:
                embed.description = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis
            else:
                embed.description = 'No synopsis available in database.'
            
            try: embed.set_image(url=data['images']['jpg']['large_image_url'])
            except (KeyError, TypeError): pass
            
            score = data.get('score')
            embed.add_field(name="Community Score", value=f"{score}/10" if score else 'N/A')
            embed.add_field(name="Published Chapters", value=str(data.get('chapters', 'N/A')))
            embed.add_field(name="Bound Volumes", value=str(data.get('volumes', 'N/A')))
            
            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "manga")
            
        except Exception as e:
            if hasattr(self.bot, 'db') and ctx.guild:
                await self.bot.db.system_health.insert_one({"guild_id": ctx.guild.id, "module": "Manga_API", "error": type(e).__name__, "timestamp": datetime.datetime.utcnow().timestamp()})
            await ctx.send("❌ **API Timeout:** The MyAnimeList database is currently unreachable.")

async def setup(bot):
    await bot.add_cog(Anime(bot))
