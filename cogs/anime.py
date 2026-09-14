import discord
from discord.ext import commands
import aiohttp
import datetime

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
        description="Queries the AniList database for anime.",
        usage="/anime <query>",
        help="/anime attack on titan"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anime(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        url = 'https://graphql.anilist.co'
        
        # GraphQL query specifically for Anime
        graphql_query = '''
        query ($search: String) {
          Media (search: $search, type: ANIME) {
            title { romaji english }
            siteUrl
            description(asHtml: false)
            coverImage { large }
            averageScore
            episodes
            status
          }
        }
        '''
        variables = {'search': query}
        headers = {"User-Agent": "Recluse Discord Bot (Created by AYush)"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={'query': graphql_query, 'variables': variables}, headers=headers, timeout=10) as response:
                    if response.status != 200:
                        return await ctx.send(f"❌ **API Error:** The database returned a {response.status} status.")
                    
                    search_result = await response.json()

            data = search_result.get('data', {}).get('Media')
            if not data:
                return await ctx.send("❌ Query yielded no results from the external database.")
                
            # Prefer English title if available, fallback to Romaji
            title = data['title'].get('english') or data['title'].get('romaji') or 'Unknown Title'
            
            embed = discord.Embed(title=title, url=data.get('siteUrl'), color=discord.Color.red())
            
            synopsis = data.get('description') or 'No synopsis available in database.'
            embed.description = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis
            
            if data.get('coverImage') and data['coverImage'].get('large'):
                embed.set_image(url=data['coverImage']['large'])
            
            score = f"{data['averageScore']}/100" if data.get('averageScore') else 'N/A'
            embed.add_field(name="Community Score", value=score)
            embed.add_field(name="Total Episodes", value=str(data.get('episodes', 'N/A')))
            embed.add_field(name="Broadcast Status", value=str(data.get('status', 'Unknown')).replace('_', ' ').title())
            
            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "anime")
            
        except Exception as e:
            if hasattr(self.bot, 'db') and ctx.guild:
                await self.bot.db.system_health.insert_one({"guild_id": ctx.guild.id, "module": "Anime_API", "error": type(e).__name__, "timestamp": datetime.datetime.utcnow().timestamp()})
            await ctx.send("❌ **API Timeout:** The AniList database is currently unreachable.")

    @commands.hybrid_command(
        name="manga", 
        description="Queries the AniList database for textual publication data.",
        usage="/manga <query>",
        help="/manga berserk"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def manga(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        url = 'https://graphql.anilist.co'
        
        # GraphQL query specifically for Manga
        graphql_query = '''
        query ($search: String) {
          Media (search: $search, type: MANGA) {
            title { romaji english }
            siteUrl
            description(asHtml: false)
            coverImage { large }
            averageScore
            chapters
            volumes
            status
          }
        }
        '''
        variables = {'search': query}
        headers = {"User-Agent": "Recluse Discord Bot (Created by AYush)"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={'query': graphql_query, 'variables': variables}, headers=headers, timeout=10) as response:
                    if response.status != 200:
                        return await ctx.send(f"❌ **API Error:** The database returned a {response.status} status.")
                    
                    search_result = await response.json()

            data = search_result.get('data', {}).get('Media')
            if not data:
                return await ctx.send("❌ Query yielded no results.")
                
            title = data['title'].get('english') or data['title'].get('romaji') or 'Unknown Title'
            
            embed = discord.Embed(title=title, url=data.get('siteUrl'), color=discord.Color.green())
            
            synopsis = data.get('description') or 'No synopsis available.'
            embed.description = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis
            
            if data.get('coverImage') and data['coverImage'].get('large'):
                embed.set_image(url=data['coverImage']['large'])
            
            score = f"{data['averageScore']}/100" if data.get('averageScore') else 'N/A'
            embed.add_field(name="Community Score", value=score)
            embed.add_field(name="Published Chapters", value=str(data.get('chapters', 'N/A')))
            embed.add_field(name="Bound Volumes", value=str(data.get('volumes', 'N/A')))
            
            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "manga")
            
        except Exception as e:
            if hasattr(self.bot, 'db') and ctx.guild:
                await self.bot.db.system_health.insert_one({"guild_id": ctx.guild.id, "module": "Manga_API", "error": type(e).__name__, "timestamp": datetime.datetime.utcnow().timestamp()})
            await ctx.send("❌ **API Timeout:** The AniList database is currently unreachable.")

async def setup(bot):
    await bot.add_cog(Anime(bot))
