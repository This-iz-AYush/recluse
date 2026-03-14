import discord
from discord.ext import commands, tasks
import aiohttp
import xml.etree.ElementTree as ET
import datetime
import re

class Sports(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.live_trackers = {}
        self.sports_cache = {"items": [], "last_updated": None}
        self.update_sports_cache.start()

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
            if settings and settings.get("sports_enabled", True) is False:
                await ctx.send("❌ The **Sports** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    def cog_unload(self):
        self.update_sports_cache.cancel()
        self.refresh_live_scores.cancel()

    @tasks.loop(seconds=30)
    async def update_sports_cache(self):
        url = "http://static.cricinfo.com/rss/livescores.xml"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.text()
                        root = ET.fromstring(data)
                        self.sports_cache["items"] = root.findall('./channel/item')
                        self.sports_cache["last_updated"] = datetime.datetime.utcnow()
        except Exception as e: print(f"Sports Cache Update Error: {e}")

    @update_sports_cache.before_loop
    async def before_update_sports_cache(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_group(
        name="score", 
        fallback="menu", 
        description="Base command for sports module.",
        usage="/score",
        help="/score"
    )
    async def score(self, ctx):
        await ctx.send("🏏 **Sports Module**\nUse `/score search <match>` for a one-time search, or `/score live <match>` to auto-refresh the score every 30 seconds!")
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "score_menu")

    @score.command(
        name="all", 
        description="Fetches all live cricket match scores instantly.",
        usage="/score all",
        help="/score all"
    )
    async def score_all(self, ctx):
        items = self.sports_cache.get("items", [])
        if not items: return await ctx.send("❌ The sports cache is currently empty or no matches are being broadcasted.")
        
        embed = discord.Embed(title="🏏 All Live Cricket Scores", color=discord.Color.orange())
        for item in items[:10]:
            title = item.find('title').text if item.find('title') is not None else 'Unknown Match'
            description = item.find('description').text if item.find('description') is not None else 'No score data'
            embed.add_field(name=title, value=description, inline=False)
        
        if self.sports_cache["last_updated"]:
            embed.set_footer(text=f"Data retrieved from internal cache • Last updated: {self.sports_cache['last_updated'].strftime('%H:%M:%S')} UTC")
        await ctx.send(embed=embed)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "score_all")

    @score.command(
        name="search", 
        description="Fetches live cricket match scores with detailed extraction.",
        usage="/score search [query]",
        help="/score search India vs Pakistan"
    )
    async def score_search(self, ctx, *, query: str = None):
        items = self.sports_cache.get("items", [])
        if not items: return await ctx.send("❌ The sports cache is empty.")
        
        if query:
            items = [item for item in items if query.lower() in (item.find('title').text or "").lower() or query.lower() in (item.find('description').text or "").lower()]
            if not items: return await ctx.send(f"❌ No live matches found matching `{query}`.")
        
        embed = discord.Embed(title="🏏 Live Cricket Scores", color=discord.Color.orange())
        for item in items[:3]:
            title = item.find('title').text if item.find('title') is not None else 'Unknown Match'
            description = item.find('description').text if item.find('description') is not None else 'No score data'
            
            overs, batsman, bowler = "N/A", "N/A", "N/A"
            details_match = re.search(r'\((.*?)\)', description)
            if details_match:
                details = details_match.group(1).split(', ')
                overs = details[0] if len(details) > 0 else "N/A"
                batsman = details[1] if len(details) > 1 else "N/A"
                bowler = details[2] if len(details) > 2 else "N/A"
                
            main_score = re.sub(r'\(.*?\)', '', description).strip()
            formatted_stats = f"**Score:** {main_score}\n"
            if overs != "N/A": formatted_stats += f"**Overs/Status:** {overs}\n"
            if batsman != "N/A": formatted_stats += f"**Batsman:** {batsman}\n"
            if bowler != "N/A": formatted_stats += f"**Bowler:** {bowler}"
                
            embed.add_field(name=title, value=formatted_stats, inline=False)
        await ctx.send(embed=embed)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "score_search")

    @score.command(
        name="live", 
        description="Starts an auto-refreshing live score tracker.",
        usage="/score live <query>",
        help="/score live Australia"
    )
    async def score_live(self, ctx, *, query: str):
        tracker_msg = await ctx.send(embed=discord.Embed(title="🏏 Initializing Live Tracker...", description=f"Searching for `{query}`...", color=discord.Color.red()))
        real_msg = await ctx.channel.fetch_message(tracker_msg.id)

        self.live_trackers[real_msg.id] = {"message": real_msg, "query": query, "channel": ctx.channel, "start_time": datetime.datetime.utcnow()}
        if not self.refresh_live_scores.is_running(): self.refresh_live_scores.start()
        await ctx.send(f"✅ Live tracking started for `{query}`.", ephemeral=True)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "score_live")

    @score.command(
        name="stop", 
        description="Stops all active live score trackers in the current channel.",
        usage="/score stop",
        help="/score stop"
    )
    @commands.has_permissions(manage_messages=True)
    async def score_stop(self, ctx):
        stopped = 0
        for msg_id, data in list(self.live_trackers.items()):
            if data["channel"] == ctx.channel:
                del self.live_trackers[msg_id]
                stopped += 1
        if stopped > 0:
            await ctx.send(f"🛑 Terminated {stopped} live trackers.")
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "score_stop")
            if not self.live_trackers: self.refresh_live_scores.cancel() 
        else: await ctx.send("❌ No active trackers found.")

    @tasks.loop(seconds=30)
    async def refresh_live_scores(self):
        if not self.live_trackers: return self.refresh_live_scores.cancel()

        now = datetime.datetime.utcnow()
        items = self.sports_cache.get("items", [])
        if not items: return

        for msg_id, tracker_data in list(self.live_trackers.items()):
            query, message, start_time = tracker_data["query"], tracker_data["message"], tracker_data.get("start_time", now)
            
            if (now - start_time).total_seconds() > 14400:
                try: await message.edit(embed=discord.Embed(title=f"🏏 Tracker Ended", description="Expired after 4 hours.", color=discord.Color.dark_grey()))
                except discord.NotFound: pass 
                del self.live_trackers[msg_id]
                continue 
            
            filtered_items = [item for item in items if query.lower() in (item.find('title').text or "").lower() or query.lower() in (item.find('description').text or "").lower()]
            embed = discord.Embed(title=f"🏏 Live Tracker: {query.title()}", color=discord.Color.red())
            embed.set_footer(text="🟢 Live • Auto-refreshing via cache • Expires in 4 hrs")
            
            if not filtered_items:
                embed.description = "Match concluded or no live data found."
            else:
                for item in filtered_items[:3]:
                    title = item.find('title').text if item.find('title') is not None else 'Unknown Match'
                    description = item.find('description').text if item.find('description') is not None else 'No score data'
                    
                    overs, batsman, bowler = "N/A", "N/A", "N/A"
                    details_match = re.search(r'\((.*?)\)', description)
                    if details_match:
                        details = details_match.group(1).split(', ')
                        overs = details[0] if len(details) > 0 else "N/A"
                        batsman = details[1] if len(details) > 1 else "N/A"
                        bowler = details[2] if len(details) > 2 else "N/A"
                        
                    main_score = re.sub(r'\(.*?\)', '', description).strip()
                    formatted_stats = f"**Score:** {main_score}\n"
                    if overs != "N/A": formatted_stats += f"**Overs/Status:** {overs}\n"
                    if batsman != "N/A": formatted_stats += f"**Batsman:** {batsman}\n"
                    if bowler != "N/A": formatted_stats += f"**Bowler:** {bowler}"
                    
                    embed.add_field(name=title, value=formatted_stats, inline=False)
            try: await message.edit(embed=embed)
            except discord.NotFound: del self.live_trackers[msg_id]
            except Exception: pass

async def setup(bot):
    await bot.add_cog(Sports(bot))
