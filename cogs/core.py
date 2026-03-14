import discord
from discord.ext import commands, tasks
import os
import time
import datetime
from itertools import cycle
from typing import Optional
import discord
from discord import app_commands

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
            embed.add_field(name="System Operations", value="> `/botinfo` - Retrieves application telemetry and metadata.\n> `/ping` - Network and websocket latency.", inline=False)
            
        elif selected == "Anime & Manga":
            embed = discord.Embed(title="🎌 Anime & Manga Commands", color=discord.Color.red())
            embed.add_field(name="Database Search", value="> `/anime <query>` - Queries MyAnimeList for anime.\n> `/manga <query>` - Queries MyAnimeList for manga.", inline=False)
            
        elif selected == "Sports":
            embed = discord.Embed(title="🏏 Sports Commands", color=discord.Color.orange())
            embed.add_field(name="Live Cricket Coverage", value="> `/score all` - Overview of all live matches.\n> `/score search <query>` - Find a specific match.\n> `/score live <query>` - Auto-updating match tracker.\n> `/score stop` - Halts active trackers in the channel.", inline=False)
            
        elif selected == "Moderation":
            embed = discord.Embed(title="🛡️ Moderation Commands", color=discord.Color.green())
            embed.add_field(name="🛑 Access Control", value="> `/ban` - Permanent removal.\n> `/tempban` - Temporary removal.\n> `/softban` - Ban & unban to clear recent messages.\n> `/kick` - Expel a member.\n> `/unban` - Revoke a ban via User ID.", inline=False)
            embed.add_field(name="🔇 Voice & Chat Restrictions", value="> `/tempmute` - Apply a native timeout.\n> `/unmute` - Remove a timeout.\n> `/vmute` / `/vunmute` - Server voice mute control.\n> `/vckick` - Disconnect a user from voice.", inline=False)
            embed.add_field(name="⚠️ Warning System", value="> `/warn` - Issue a formal warning.\n> `/warnings` - View a member's warning history.\n> `/delwarn` - Delete a specific warning ID.\n> `/clearwarns` - Wipe a user's entire record.\n> `/moderations` - List active timed mutes.", inline=False)
            embed.add_field(name="🛠️ Channel Management", value="> `/purge` / `/clean` - Bulk message deletion tools.\n> `/lock` / `/unlock` - Channel access control.\n> `/slowmode` - Set chat delay rate limits.", inline=False)
            embed.add_field(name="👥 Member Management", value="> `/role` - Toggle a role for a user.\n> `/nick` - Change or reset a user's nickname.\n> `/members` - List all members within a specific role.", inline=False)
            
        elif selected == "Miscellaneous":
            embed = discord.Embed(title="🗂️ Miscellaneous Commands", color=discord.Color.teal())
            embed.add_field(name="👤 User Utilities", value="> `/whois` - Pull a security profile on a user.\n> `/avatar` - Retrieve a high-res profile picture.\n> `/afk` - Set an away status for mentions.", inline=False)
            embed.add_field(name="🏢 Server Infrastructure", value="> `/serverinfo` - Network & security data for the server.\n> `/roleinfo` - Role permissions & member stats.\n> `/channelinfo` - Infrastructure details for a channel.\n> `/membercount` - Get the current server population.", inline=False)
            embed.add_field(name="🧰 General Tools", value="> `/poll` - Initiate a network-wide binary poll.\n> `/color` - Analyze a HEX color code and return its data.", inline=False)
            
        elif selected == "Generative AI":
            embed = discord.Embed(title="🎨 Generative AI Commands", color=discord.Color.blurple())
            embed.add_field(name="Image Creation", value="> `/imagine <prompt>` - Generates a high-quality image based on your text prompt.", inline=False)
            
        elif selected == "Conversational AI":
            embed = discord.Embed(title="🤖 Conversational AI Commands", color=discord.Color.purple())
            embed.add_field(name="Interaction", value="> `@Recluse <message>` - Ping the bot directly in any channel to chat!\n> `/choose_ai <model>` - Switch your AI brain (nexusify, gemini, sarvam).\n> `/clear_memory` - Wipes your conversation history to start fresh.", inline=False)
            
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
        self.uptime_heartbeat.start()

    def cog_unload(self):
        self.cycle_bot_status.cancel()
        self.uptime_heartbeat.cancel()

    # --- 🛡️ GATEKEEPER CHECK ---
    async def cog_check(self, ctx):
        if hasattr(self.bot, 'db'):
            is_blacklisted = await self.bot.db.global_blacklist.find_one({"target_id": ctx.author.id, "type": "user"})
            if is_blacklisted:
                try: await ctx.send("❌ **Access Denied:** You have been permanently blacklisted from the Recluse network.", ephemeral=True)
                except Exception: pass
                return False
        return True

    # --- AUTOCOMPLETE LOGIC FOR HELP COMMAND ---
    async def command_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Grabs all registered commands and filters them as the user types
        commands = [c.name for c in self.bot.commands if not c.hidden]
        matches = [cmd for cmd in commands if current.lower() in cmd.lower()]
        
        # Discord API limits autocomplete choices to 25 max
        return [app_commands.Choice(name=match, value=match) for match in matches[:25]]

    @commands.hybrid_command(name="help", description="Shows help info and commands.")
    @app_commands.autocomplete(command=command_autocomplete)
    async def custom_help(self, ctx, command: str = None):
        # If they just typed /help, show the category dropdown
        if not command:
            embed = discord.Embed(title="Recluse Help Desk", description="Please select a category below.", color=discord.Color.blurple())
            embed.add_field(name="🌐 Web Dashboard", value="[Visit Dashboard](https://recluse-1.onrender.com/)", inline=True)
            embed.add_field(name="📈 Uptime Status", value="[Check Status](https://sszvcg5v.status.cron-job.org)", inline=True)
            await ctx.send(embed=embed, view=HelpView())
            
        # If they typed /help <command>, show specific command info
        else:
            cmd = self.bot.get_command(command)
            if not cmd:
                return await ctx.send(f"❌ Could not find the command `{command}` in my registry.", ephemeral=True)

            embed = discord.Embed(
                title=f"Command: /{cmd.name}", 
                description=cmd.description or "No description provided.", 
                color=discord.Color.blurple()
            )

            # Automatically formats how to use the command based on its parameters
            usage = f"/{cmd.name} {cmd.signature}".strip()
            embed.add_field(name="Usage", value=f"`{usage}`", inline=False)

            if cmd.aliases:
                embed.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases), inline=False)

            await ctx.send(embed=embed)

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
                # Retrieve the original start time from the database
                db_start = uptime_data.get("start_time", int(time.time()))
                uptime_seconds = max(0, int(time.time()) - db_start)
        
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
                # First time booting up the tracker
                await self.bot.db.bot_telemetry.insert_one({
                    "id": "uptime", 
                    "start_time": current_time, 
                    "last_heartbeat": current_time
                })
            else:
                last_hb = data.get("last_heartbeat", current_time)
                
                # CRITICAL FIX: Increased tolerance to 20 minutes (1200 seconds)
                # Render free tier cold-boots can take a while. If the gap is larger than 20 mins, 
                # we assume the cron job failed and reset the clock.
                if current_time - last_hb > 1200:
                    await self.bot.db.bot_telemetry.update_one(
                        {"id": "uptime"}, 
                        {"$set": {"start_time": current_time, "last_heartbeat": current_time}}
                    )
                else:
                    # Update the pulse timestamp to prove it's still alive!
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
