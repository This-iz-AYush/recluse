import discord
from discord.ext import commands
from jikanpy import AioJikan

class Anime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        # Database check: Refuses to run if the server owner disabled Anime.
        if not ctx.guild: return True
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": ctx.guild.id})
            if settings and settings.get("anime_enabled", True) is False:
                await ctx.send("❌ The **Anime** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    @commands.hybrid_command(name="anime", description="Queries the MyAnimeList database for anime.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anime(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author):
            ctx.command.reset_cooldown(ctx)
            
        await ctx.defer()
        async with AioJikan() as jikan:
            try:
                search_result = await jikan.search('anime', query)
                if not search_result.get('data'):
                    return await ctx.send("❌ Query yielded no results from the external database.")
                
                data = search_result['data'][0]
                embed = discord.Embed(title=data.get('title', 'Unknown Title'), url=data.get('url'), color=discord.Color.red())
                
                synopsis = data.get('synopsis') or 'No synopsis available in database.'
                embed.description = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis
                
                try:
                    embed.set_image(url=data['images']['jpg']['large_image_url'])
                except (KeyError, TypeError):
                    pass
                
                embed.add_field(name="Community Score", value=str(data.get('score', 'N/A')))
                embed.add_field(name="Total Episodes", value=str(data.get('episodes', 'N/A')))
                embed.add_field(name="Broadcast Status", value=str(data.get('status', 'Unknown')))
                
                await ctx.send(embed=embed)
            except Exception as e:
                await self.bot.log_system_error(ctx, e) if hasattr(self.bot, 'log_system_error') else print(e)
                await ctx.send("❌ **API Timeout:** The MyAnimeList database is currently unreachable.")

    @commands.hybrid_command(name="manga", description="Queries the MyAnimeList database for textual publication data.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def manga(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author):
            ctx.command.reset_cooldown(ctx)
            
        await ctx.defer()
        async with AioJikan() as jikan:
            try:
                search_result = await jikan.search('manga', query)
                if not search_result.get('data'):
                    return await ctx.send("❌ Query yielded no results.")
                
                data = search_result['data'][0]
                embed = discord.Embed(title=data.get('title', 'Unknown Title'), url=data.get('url'), color=discord.Color.green())
                
                synopsis = data.get('synopsis') or 'No synopsis available.'
                embed.description = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis
                
                try:
                    embed.set_image(url=data['images']['jpg']['large_image_url'])
                except (KeyError, TypeError):
                    pass
                
                embed.add_field(name="Community Score", value=str(data.get('score', 'N/A')))
                embed.add_field(name="Published Chapters", value=str(data.get('chapters', 'N/A')))
                embed.add_field(name="Bound Volumes", value=str(data.get('volumes', 'N/A')))
                
                await ctx.send(embed=embed)
            except Exception as e:
                await self.bot.log_system_error(ctx, e) if hasattr(self.bot, 'log_system_error') else print(e)
                await ctx.send("❌ **API Timeout:** The MyAnimeList database is currently unreachable.")

async def setup(bot):
    await bot.add_cog(Anime(bot))
