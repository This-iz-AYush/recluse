import discord
from discord.ext import commands, tasks
import os
import time
import datetime
from itertools import cycle
from typing import Optional

class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Telemetry", description="Bot info and uptime stats", emoji="📊"),
            discord.SelectOption(label="Anime & Manga", description="Search MyAnimeList database", emoji="🎌"),
            discord.SelectOption(label="Moderation", description="Ban, mute, purge and more", emoji="🛡️"),
            discord.SelectOption(label="Sports", description="Live Cricket Score", emoji="🏏"),
            discord.SelectOption(label="Miscellaneous", description="Server info, avatars, ping, afk", emoji="🗂️"),
            discord.SelectOption(label="Generative AI", description="Create images from text", emoji="🎨"),
            discord.SelectOption(label="Conversational AI", description="Chat with Recluse", emoji="🤖"),
        ]
        super().__init__(placeholder="Choose a command category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        
        if selected == "Telemetry":
            embed = discord.Embed(title="📊 Telemetry Commands", color=discord.Color.blue())
            embed.add_field(name="`/botinfo`", value="Retrieves the application's telemetry and metadata.", inline=False)
        elif selected == "Anime & Manga":
            embed = discord.Embed(title="🎌 Anime & Manga Commands", color=discord.Color.red())
            embed.add_field(name="`/anime <query>`", value="Queries the MyAnimeList database for anime.", inline=False)
            embed.add_field(name="`/manga <query>`", value="Queries the MyAnimeList database for manga.", inline=False)
        elif selected == "Sports":
            embed = discord.Embed(title="🏏 Sports Commands", color=discord.Color.orange())
            embed.add_field(name="`/score all`", value="Fetches an overview of all live cricket matches.", inline=False)
            embed.add_field(name="`/score search <query>`", value="Searches a live match.", inline=False)
            embed.add_field(name="`/score live <query>`", value="Starts a tracker that auto-updates every 30 seconds.", inline=False)
            embed.add_field(name="`/score stop`", value="Stops all active live trackers in the channel.", inline=False)
        elif selected == "Moderation":
            embed = discord.Embed(title="🛡️ Moderation Commands", color=discord.Color.green())
            embed.add_field(name="`/ban <member> [reason]`", value="Permanently removes a member utilizing API-level bans.", inline=False)
            embed.add_field(name="`/tempban <member> <duration> [reason]`", value="Temporarily bans a member.", inline=False)
            embed.add_field(name="`/unban <user_id> [reason]`", value="Revokes a ban utilizing API-level unbans.", inline=False)
            embed.add_field(name="`/kick <member> [reason]`", value="Kicks a member from the server.", inline=False)
            embed.add_field(name="`/purge <limit>`", value="Executes a bulk-delete payload.", inline=False)
            embed.add_field(name="`/tempmute <member> <duration> [reason]`", value="Applies a native timeout.", inline=False)
            embed.add_field(name="`/unmute <member> [reason]`", value="Removes a timeout from a member.", inline=False)
            embed.add_field(name="`/vckick <member> [reason]`", value="Forcefully terminates a voice connection.", inline=False)
            embed.add_field(name="`/lock [channel]`", value="Locks the current or specified channel.", inline=False)
            embed.add_field(name="`/unlock [channel]`", value="Unlocks a previously locked channel.", inline=False)
            embed.add_field(name="`/slowmode <seconds>`", value="Sets chat delay for the current channel.", inline=False)
            embed.add_field(name="`/warn <member> [reason]`", value="Warns a member. Auto-mutes after 3 warnings.", inline=False)
            embed.add_field(name="`/warnings <member>`", value="View all warnings for a member.", inline=False)
            embed.add_field(name="`/delwarn <member> <warning_id>`", value="Removes a specific warning from a user.", inline=False)
            embed.add_field(name="`/moderations`", value="Lists active timed moderations (mutes) in the server.", inline=False)
            embed.add_field(name="`/members <role>`", value="Lists members in a specific role.", inline=False)
            embed.add_field(name="`/clean [limit]`", value="Cleans up the bot's own responses in the channel.", inline=False)
        elif selected == "Miscellaneous":
            embed = discord.Embed(title="🗂️ Miscellaneous Commands", color=discord.Color.teal())
            embed.add_field(name="`/ping`", value="Ping the bot and get the response time.", inline=False)
            embed.add_field(name="`/afk [reason]`", value="Set an AFK status to display when you are mentioned.", inline=False)
            embed.add_field(name="`/avatar [user]`", value="Get the avatar of yourself or another user.", inline=False)
            embed.add_field(name="`/membercount`", value="Get the membercount of the current server.", inline=False)
            embed.add_field(name="`/serverinfo`", value="Get information about the current server.", inline=False)
            embed.add_field(name="`/whois [user]`", value="Get information about a user.", inline=False)
        elif selected == "Generative AI":
            embed = discord.Embed(title="🎨 Generative AI Commands", color=discord.Color.blurple())
            embed.add_field(name="`/imagine <prompt>`", value="Generates a high-quality image based on your text prompt.", inline=False)
        elif selected == "Conversational AI":
            embed = discord.Embed(title="🤖 Conversational AI Commands", color=discord.Color.purple())
            embed.add_field(name="`@Recluse <message>`", value="Ping the bot directly in any channel to chat!", inline=False)
            embed.add_field(name="`/choose_ai <model>`", value="Switch your AI brain between nexusify, gemini, and sarvam.", inline=False)
            embed.add_field(name="`/clear_memory`", value="Wipes your conversation history with the bot to start fresh.", inline=False)
        else:
            embed = discord.Embed(title="Error", description="Category not found.", color=discord.Color.red())

        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpSelect())

class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.start_time = time.time()
        self.STATUS_MESSAGES = ['active in {servers} servers with {members} members', '/help']
        self.status_cycle = cycle(self.STATUS_MESSAGES)
        
        self.cycle_bot_status.start()
        self.uptime_heartbeat.start() # <-- Boots up the DB heartbeat

    def cog_unload(self):
        self.cycle_bot_status.cancel()
        self.uptime_heartbeat.cancel() # <-- Shuts down the heartbeat

    # --- 🛡️ GATEKEEPER CHECK ---
    async def cog_check(self, ctx):
        if hasattr(self.bot, 'db'):
            is_blacklisted = await self.bot.db.global_blacklist.find_one({"target_id": ctx.author.id, "type": "user"})
            if is_blacklisted:
                try: await ctx.send("❌ **Access Denied:** You have been permanently blacklisted from the Recluse network.", ephemeral=True)
                except Exception: pass
                return False
        return True

    @commands.hybrid_command(name="unblacklist", description="Developer Override: Removes an ID from the global blacklist.")
    @commands.is_owner()
    async def unblacklist(self, ctx, target_id: str):
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ **Database Error:** Disconnected.", ephemeral=True)
            
        try:
            target_id_int = int(target_id.strip())
        except ValueError:
            return await ctx.send("❌ **Error:** Please provide a valid numeric ID.", ephemeral=True)
            
        result = await self.bot.db.global_blacklist.delete_one({"target_id": target_id_int})
        if result.deleted_count > 0:
            await ctx.send(f"✅ **Override Successful:** ID `{target_id_int}` has been wiped from the global blacklist.", ephemeral=True)
        else:
            await ctx.send(f"❌ **Not Found:** ID `{target_id_int}` is not currently blacklisted.", ephemeral=True)

    @commands.hybrid_command(name="help", description="Generates and deploys the interactive dynamic help menu.")
    async def custom_help(self, ctx):
        embed = discord.Embed(title="Recluse Help Desk", description="Please select a category below.", color=discord.Color.blurple())
        embed.add_field(name="🌐 Web Dashboard", value="[Visit Dashboard](https://recluse-1.onrender.com/)", inline=True)
        embed.add_field(name="📈 Uptime Status", value="[Check Status](https://sszvcg5v.status.cron-job.org)", inline=True)
        await ctx.send(embed=embed, view=HelpView())

    @commands.hybrid_command(name="botinfo", description="Retrieves the application's telemetry and metadata.")
    async def botinfo(self, ctx):
        await ctx.defer() 
        active_ai = self.bot.get_cog('AI').user_ai_preference.get(ctx.author.id, "nexusify").title() if self.bot.get_cog('AI') else "Nexusify"
        app_info = await self.bot.application_info()
        
        embed = discord.Embed(title="System Telemetry", color=discord.Color.blue())
        embed.add_field(name="Registered Owner", value=str(app_info.owner), inline=True)
        embed.add_field(name="Websocket Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Your Active AI", value=f"🧠 **{active_ai}**", inline=True)
        
        # --- 📈 PERSISTENT UPTIME TRACKER ---
        uptime_seconds = max(0, int(time.time() - self.start_time)) # Local fallback
        
        if hasattr(self.bot, 'db'):
            uptime_data = await self.bot.db.bot_telemetry.find_one({"id": "uptime"})
            if uptime_data:
                # Calculate duration from the DB start time instead of Render's reset time
                uptime_seconds = max(0, int(time.time()) - uptime_data.get("start_time", int(time.time())))
        
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        uptime_display = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"
        status_display = "🟢 **Operational**"

        embed.add_field(name="Service Status", value=status_display, inline=True)
        embed.add_field(name="Continuous Uptime", value=uptime_display, inline=True)
        
        await ctx.send(embed=embed)

    # --- ❤️ DATABASE HEARTBEAT LOGIC ---
    @tasks.loop(minutes=1)
    async def uptime_heartbeat(self):
        """Records a heartbeat to MongoDB to calculate true uptime bypassing Render restarts."""
        if hasattr(self.bot, 'db'):
            current_time = int(time.time())
            data = await self.bot.db.bot_telemetry.find_one({"id": "uptime"})
            
            if not data:
                # First time booting up the tracker ever
                await self.bot.db.bot_telemetry.insert_one({
                    "id": "uptime", 
                    "start_time": current_time, 
                    "last_heartbeat": current_time
                })
            else:
                last_hb = data.get("last_heartbeat", current_time)
                
                # If it's been more than 5 minutes (300 seconds) since the last pulse, 
                # the cron job failed or the bot genuinely crashed. Reset the clock!
                if current_time - last_hb > 300:
                    await self.bot.db.bot_telemetry.update_one(
                        {"id": "uptime"}, 
                        {"$set": {"start_time": current_time, "last_heartbeat": current_time}}
                    )
                else:
                    # Otherwise, just update the pulse timestamp to prove it's still alive!
                    await self.bot.db.bot_telemetry.update_one(
                        {"id": "uptime"}, 
                        {"$set": {"last_heartbeat": current_time}}
                    )

    @uptime_heartbeat.before_loop
    async def before_uptime_heartbeat(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def cycle_bot_status(self):
        try:
            sports_cog = self.bot.get_cog('Sports')
            active_matches = len(sports_cog.live_trackers) if sports_cog else 0
            
            if active_matches > 0:
                activity = discord.Activity(type=discord.ActivityType.watching, name=f"{active_matches} live cricket match{'es' if active_matches > 1 else ''}")
            else:
                server_count = len(self.bot.guilds)
                member_count = sum(guild.member_count for guild in self.bot.guilds if guild.member_count)
                activity = discord.Game(name=next(self.status_cycle).replace("{servers}", str(server_count)).replace("{members}", str(member_count)))
                
            await self.bot.change_presence(activity=activity)
        except Exception: pass 

    @cycle_bot_status.before_loop
    async def before_cycle_bot_status(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Core(bot))
