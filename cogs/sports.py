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
        except Exception as e:
            print(f"Sports Cache Update Error: {e}")

    @update_sports_cache.before_loop
    async def before_update_sports_cache(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name="score", fallback="menu", description="Base command for sports module.")
    async def score(self, ctx):
        await ctx.send("🏏 **Sports Module**\nUse `/score search <match>` for a one-time search, or `/score live <match>` to auto-refresh the score every 30 seconds!")

    @score.command(name="all", description="Fetches all live cricket match scores instantly.")
    async def score_all(self, ctx):
        items = self.sports_cache.get("items", [])
        if not items:
            return await ctx.send("❌ The sports cache is currently empty or no matches are being broadcasted.")
        
        embed = discord.Embed(title="🏏 All Live Cricket Scores", color=discord.Color.orange())
        for item in items[:10]:
            title = item.find('title').text if item.find('title') is not None else 'Unknown Match'
            description = item.find('description').text if item.find('description') is not None else 'No score data'
            embed.add_field(name=title, value=description, inline=False)
        
        if self.sports_cache["last_updated"]:
            embed.set_footer(text=f"Data retrieved from internal cache • Last updated: {self.sports_cache['last_updated'].strftime('%H:%M:%S')} UTC")
            
        await ctx.send(embed=embed)

    @score.command(name="search", description="Fetches live cricket match scores with detailed extraction.")
    async def score_search(self, ctx, *, query: str = None):
        items = self.sports_cache.get("items", [])
        if not items:
            return await ctx.send("❌ The sports cache is currently empty or no matches are being broadcasted.")
        
        if query:
            items = [
                item for item in items 
                if query.lower() in (item.find('title').text or "").lower() 
                or query.lower() in (item.find('description').text or "").lower()
            ]
            if not items:
                return await ctx.send(f"❌ No live matches found matching `{query}` in the current cache.")
        
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
            if overs != "N/A":
                if "ov" in overs.lower():
                    formatted_stats += f"**Overs:** {overs}\n"
                else:
                    formatted_stats += f"**Match Status:** {overs}\n"
            if batsman != "N/A":
                formatted_stats += f"**Batsman:** {batsman}\n"
            if bowler != "N/A":
                formatted_stats += f"**Bowler:** {bowler}"
                
            embed.add_field(name=title, value=formatted_stats, inline=False)
        
        if self.sports_cache["last_updated"]:
            embed.set_footer(text=f"Data retrieved from internal cache • Last updated: {self.sports_cache['last_updated'].strftime('%H:%M:%S')} UTC")
            
        await ctx.send(embed=embed)

    @score.command(name="live", description="Starts an auto-refreshing live score tracker.")
    async def score_live(self, ctx, *, query: str):
        embed = discord.Embed(title="🏏 Initializing Live Tracker...", description=f"Searching for `{query}`...", color=discord.Color.red())
        tracker_msg = await ctx.send(embed=embed)

        try:
            real_msg = await ctx.channel.fetch_message(tracker_msg.id)
        except:
            real_msg = tracker_msg

        self.live_trackers[real_msg.id] = {
            "message": real_msg,
            "query": query,
            "channel": ctx.channel,
            "start_time": datetime.datetime.utcnow() 
        }

        if not self.refresh_live_scores.is_running():
            self.refresh_live_scores.start()
            
        await ctx.send(f"✅ Live tracking started for `{query}`.", ephemeral=True)

    @score.command(name="stop", description="Stops all active live score trackers in the current channel.")
    async def score_stop(self, ctx):
        stopped = 0
        for msg_id, data in list(self.live_trackers.items()):
            if data["channel"] == ctx.channel:
                del self.live_trackers[msg_id]
                stopped += 1
                
        if stopped > 0:
            await ctx.send(f"🛑 Successfully terminated {stopped} active live score tracker(s) in this channel.")
            if not self.live_trackers:
                self.refresh_live_scores.cancel() 
        else:
            await ctx.send("❌ No active trackers found in this channel.")

    @tasks.loop(seconds=30)
    async def refresh_live_scores(self):
        if not self.live_trackers:
            self.refresh_live_scores.cancel()
            return

        now = datetime.datetime.utcnow()
        items = self.sports_cache.get("items", [])
        if not items: return

        for msg_id, tracker_data in list(self.live_trackers.items()):
            query = tracker_data["query"]
            message = tracker_data["message"]
            start_time = tracker_data.get("start_time", now)
            
            if (now - start_time).total_seconds() > 14400:
                try:
                    timeout_embed = discord.Embed(
                        title=f"🏏 Tracker Ended: {query.title()}",
                        description="This live tracker has automatically expired after 4 hours.",
                        color=discord.Color.dark_grey()
                    )
                    await message.edit(embed=timeout_embed)
                except discord.NotFound:
                    pass 
                del self.live_trackers[msg_id]
                continue 
            
            filtered_items = [
                item for item in items 
                if query.lower() in (item.find('title').text or "").lower() 
                or query.lower() in (item.find('description').text or "").lower()
            ]
            
            embed = discord.Embed(title=f"🏏 Live Tracker: {query.title()}", color=discord.Color.red())
            embed.set_footer(text="🟢 Live • Auto-refreshing via cache • Expires in 4 hrs")
            
            if not filtered_items:
                embed.description = "Match concluded or no live data found for this query right now."
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
                    if overs != "N/A":
                        if "ov" in overs.lower():
                            formatted_stats += f"**Overs:** {overs}\n"
                        else:
                            formatted_stats += f"**Match Status:** {overs}\n"
                    if batsman != "N/A":
                        formatted_stats += f"**Batsman:** {batsman}\n"
                    if bowler != "N/A":
                        formatted_stats += f"**Bowler:** {bowler}"
                        
                    embed.add_field(name=title, value=formatted_stats, inline=False)
            
            try:
                await message.edit(embed=embed)
            except discord.NotFound:
                del self.live_trackers[msg_id]
            except Exception as e:
                print(f"Failed to edit live tracker: {e}")

async def setup(bot):
    await bot.add_cog(Sports(bot))