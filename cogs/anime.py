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
        name="mal_link", 
        description="Link your public MyAnimeList username to Recluse.",
        usage="/mal_link <username>"
    )
    async def mal_link(self, ctx, username: str):
        """Stores the user's MAL username in MongoDB for personal tracking data."""
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ **Database Error:** MongoDB connection is not available.")
            
        await self.bot.db.mal_users.update_one(
            {"discord_id": ctx.author.id},
            {"$set": {"mal_username": username}},
            upsert=True
        )
        await ctx.send(f"✅ Successfully linked MyAnimeList account: **{username}**")

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

        # Check if the command is being run in an age-restricted channel
        is_nsfw_channel = ctx.channel.is_nsfw() if hasattr(ctx.channel, 'is_nsfw') else False

        params = {
            'q': query,
            'limit': 1,
            'nsfw': 'true' if is_nsfw_channel else 'false',
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
            
            # --- Personal List Fetching (Method 2 with limit 1000 fix) ---
            user_status_val = ""
            if hasattr(self.bot, 'db'):
                linked_user = await self.bot.db.mal_users.find_one({"discord_id": ctx.author.id})
                if linked_user:
                    username = linked_user.get("mal_username")
                    user_list_url = f"https://api.myanimelist.net/v2/users/{username}/animelist"
                    user_params = {'limit': 1000, 'fields': 'list_status'}
                    
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(user_list_url, params=user_params, headers=headers) as user_res:
                                if user_res.status == 200:
                                    user_data = await user_res.json()
                                    
                                    found_status = None
                                    for item in user_data.get('data', []):
                                        if item.get('node', {}).get('id') == mal_id:
                                            found_status = item.get('list_status', {})
                                            break
                                            
                                    if found_status:
                                        personal_status = found_status.get('status', 'unknown').replace('_', ' ').title()
                                        personal_score = found_status.get('score', 0)
                                        eps_watched = found_status.get('num_episodes_watched', 0)
                                        
                                        status_emoji = "🟢" if personal_status == "Watching" else "🔵" if personal_status == "Completed" else "🟡" if personal_status == "On Hold" else "🔴" if personal_status == "Dropped" else "⚪"
                                        
                                        user_status_val = f"{status_emoji} **Status:** {personal_status} ｜ ⭐ **Score:** {personal_score}/10 ｜ 🎬 **Watched:** {eps_watched} eps"
                                    else:
                                        user_status_val = "*This anime is not on your MAL list (or is buried past your 1000 most recent entries).*"
                    except Exception:
                        pass # Silently fail so the main embed still sends

            # --- Data Extraction & Formatting ---
            def code_fmt(val):
                return f"`{val}`" if val and str(val).strip() else "``"

            alt_titles = node.get('alternative_titles', {})
            en_title = alt_titles.get('en', '')
            ja_title = alt_titles.get('ja', '')
            synonyms = ", ".join(alt_titles.get('synonyms', []))
            
            season_data = node.get('start_season', {})
            premiered = f"{season_data.get('season', '').title()} {season_data.get('year', '')}".strip()
            broadcast = node.get('broadcast', {}).get('day_of_the_week', '').title()
            aired = f"{node.get('start_date', '')}"
            
            genres = ", ".join([g['name'] for g in node.get('genres', [])])
            media_type = node.get('media_type', 'Unknown').upper() if node.get('media_type') in ['tv', 'ova', 'ona'] else node.get('media_type', '').title()
            episodes = node.get('num_episodes', '')
            rating = node.get('rating', '').replace('_', ' ').title()
            score = node.get('mean', '')
            ranked = f"#{node.get('rank')}" if node.get('rank') else ""
            popularity = f"#{node.get('popularity')}" if node.get('popularity') else ""
            
            dur_secs = node.get('average_episode_duration', 0)
            dur_hours, dur_mins = divmod(dur_secs // 60, 60)
            duration = f"{dur_hours} hr. {dur_mins} min." if dur_hours > 0 else f"{dur_mins} min." if dur_mins > 0 else ""
            
            studios = ", ".join([s['name'] for s in node.get('studios', [])])
            members = f"{node.get('num_list_users', 0):,}"
            score_stats = f"scored by {node.get('num_scoring_users', 0):,} users"
            source = node.get('source', '').replace('_', ' ').title()
            status = node.get('status', '').replace('_', ' ').title()
            link = f"https://myanimelist.net/anime/{mal_id}/{title.replace(' ', '_')}" if mal_id else ""

            synopsis = self.clean_html(node.get('synopsis'))
            synopsis = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis

            # --- Embed Construction ---
            embed = discord.Embed(title=title, url=link, description=synopsis, color=0x3498db)
            
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
            
            # Row 4
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

            # Interactive Ticking Timestamp
            current_unix = int(datetime.datetime.now().timestamp())
            embed.add_field(name="Query Time", value=f"<t:{current_unix}:R>", inline=False)

            # Personal Tracking Data
            if user_status_val:
                embed.add_field(name=f"Your MAL Status ({linked_user.get('mal_username')})", value=user_status_val, inline=False)

            # Image Handling
            pictures = node.get('main_picture', {})
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            
            if pictures.get('large'):
                embed.set_image(url=pictures['large'])
            elif pictures.get('medium'):
                embed.set_image(url=pictures['medium'])
                
            # Footer matching the selected immersive design
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name} • Recluse Database • Powered by MAL API", 
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

        headers = {
            "X-MAL-CLIENT-ID": client_id.strip(),
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
            
            genres_data = node.get('genres') or []
            genres_list = [g.get('name', 'Unknown') for g in genres_data]
            genres_block = ", ".join(genres_list) if genres_list else "None"
            
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

            pictures = node.get('main_picture') or {}
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            if pictures.get('large'):
                embed.set_image(url=pictures['large'])
            elif pictures.get('medium'):
                embed.set_image(url=pictures['medium'])
                
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name} • Recluse by AYush • Powered by MAL API", 
                icon_url=ctx.author.display_avatar.url
            )

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "manga")
            
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")

async def setup(bot):
    await bot.add_cog(Anime(bot))
