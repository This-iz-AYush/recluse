import discord
from discord.ext import commands, tasks
import os
import aiohttp
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
        self.STATUS_MESSAGES = ['active in {servers} servers with {members} members', '/help']
        self.status_cycle = cycle(self.STATUS_MESSAGES)
        self.cycle_bot_status.start()

    def cog_unload(self):
        self.cycle_bot_status.cancel()

    @commands.hybrid_command(name="help", description="Generates and deploys the interactive dynamic help menu.")
    async def custom_help(self, ctx):
        embed = discord.Embed(title="Recluse Help Desk", description="Please select a category below.", color=discord.Color.blurple())
        
        # Added Dashboard and Uptime Status links
        embed.add_field(name="🌐 Web Dashboard", value="[Visit Dashboard](https://recluse-1.onrender.com/)", inline=True)
        embed.add_field(name="📈 Uptime Status", value="[Check Status](https://stats.uptimerobot.com/njZB3KSajf)", inline=True)
        
        await ctx.send(embed=embed, view=HelpView())

    @commands.hybrid_command(name="botinfo", description="Retrieves the application's telemetry and metadata.")
    async def botinfo(self, ctx):
        await ctx.defer() 
        import time # Added at the top to ensure time parsing works safely
        
        active_ai = self.bot.get_cog('AI').user_ai_preference.get(ctx.author.id, "nexusify").title() if self.bot.get_cog('AI') else "Nexusify"
        app_info = await self.bot.application_info()
        
        embed = discord.Embed(title="System Telemetry", color=discord.Color.blue())
        embed.add_field(name="Registered Owner", value=str(app_info.owner), inline=True)
        embed.add_field(name="Websocket Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Your Active AI", value=f"🧠 **{active_ai}**", inline=True)
        

        # --- Fetch UptimeRobot Stats ---
        api_key = os.getenv('UPTIMEROBOT_API_KEY')
        status_display = "⚪ **Unknown**"
        uptime_display = "Configure `UPTIMEROBOT_API_KEY` in .env"
        
        if api_key:
            try:
                async with aiohttp.ClientSession() as session:
                    url = "https://api.uptimerobot.com/v2/getMonitors"
                    payload = f"api_key={api_key.strip()}&format=json&logs=1"
                    headers = {
                        'content-type': "application/x-www-form-urlencoded",
                        'cache-control': "no-cache"
                    }
                    async with session.post(url, data=payload, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data.get("stat") == "ok" and data.get("monitors"):
                                monitor = data["monitors"][0]
                                status_code = int(monitor.get("status", 0))
                                
                                if status_code == 2:
                                    status_display = "🟢 **Operational**"
                                    
                                    # 1. Try to get uptime from the latest log
                                    logs = monitor.get("logs", [])
                                    last_up_timestamp = None
                                    
                                    for log in logs:
                                        if int(log.get("type", 0)) in [2, 98]:
                                            last_up_timestamp = log.get("datetime")
                                            break
                                            
                                    # 2. Fallback: If no logs exist (brand new monitor), use creation time
                                    if not last_up_timestamp:
                                        last_up_timestamp = monitor.get("create_datetime")
                                            
                                    if last_up_timestamp:
                                        uptime_seconds = max(0, int(time.time()) - int(last_up_timestamp))
                                        days, remainder = divmod(uptime_seconds, 86400)
                                        hours, remainder = divmod(remainder, 3600)
                                        minutes, seconds = divmod(remainder, 60)
                                        uptime_display = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"
                                    else:
                                        uptime_display = "Tracking..."

                                elif status_code in [8, 9]:
                                    status_display = "🔴 **Down**"
                                    uptime_display = "0d 0h 0m 0s"
                                else:
                                    status_display = "⚪ **Paused**"
                                    uptime_display = "N/A"
                            else:
                                uptime_display = "⚠️ Monitor data unavailable."
                        else:
                            uptime_display = f"⚠️ API Error: {response.status}"
            except Exception:
                uptime_display = "⚠️ Failed to connect to UptimeRobot."

        embed.add_field(name="Service Status", value=status_display, inline=True)
        embed.add_field(name="Continuous Uptime", value=uptime_display, inline=True)
        
        await ctx.send(embed=embed)

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

    # --- WICK-STYLE SECURITY HELPERS ---
    async def hierarchy_check(self, ctx, member: discord.Member) -> bool:
        """Ensures moderators cannot target users higher or equal to them in the role hierarchy."""
        if ctx.guild.owner == member:
            await ctx.send("❌ **Security Override:** You cannot target the server owner.", ephemeral=True)
            return False
        if ctx.author != ctx.guild.owner and ctx.author.top_role <= member.top_role:
            await ctx.send("❌ **Hierarchy Error:** You lack the clearance to target a member with an equal or higher role.", ephemeral=True)
            return False
        if ctx.guild.me.top_role <= member.top_role:
            await ctx.send("❌ **Execution Blocked:** My highest role is below the target's highest role. Move my bot role higher in server settings.", ephemeral=True)
            return False
        return True

    async def log_mod_action(self, ctx, action: str, target: discord.User, reason: str):
        """Pushes moderation telemetry to the database for the Web Dashboard Audit Logs."""
        if hasattr(self.bot, 'db'):
            log_data = {
                "guild_id": ctx.guild.id,
                "moderator_id": ctx.author.id,
                "moderator_name": str(ctx.author),
                "target_id": target.id,
                "target_name": str(target),
                "action": action,
                "reason": reason,
                "timestamp": datetime.datetime.utcnow().timestamp()
            }
            await self.bot.db.mod_logs.insert_one(log_data)

    # --- MODERATION COMMANDS ---

    @commands.hybrid_command(name="ban", description="Permanently removes a member utilizing API-level bans.")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: Optional[str] = "No reason provided."):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        # Wick-style: Attempt to DM the user before banning
        try:
            await member.send(f"🔨 You have been banned from **{ctx.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass # User has DMs off
            
        await member.ban(reason=f"Action by {ctx.author} | {reason}")
        await self.log_mod_action(ctx, "Ban", member, reason)
        
        embed = discord.Embed(title="🔨 Target Neutralized", description=f"Successfully banned {member.mention}.", color=discord.Color.red())
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="kick", description="Kicks a member from the server.")
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: Optional[str] = "No reason provided."):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        try:
            await member.send(f"👢 You have been kicked from **{ctx.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass
            
        await member.kick(reason=f"Action by {ctx.author} | {reason}")
        await self.log_mod_action(ctx, "Kick", member, reason)
        
        embed = discord.Embed(title="👢 Target Expelled", description=f"Successfully kicked {member.mention}.", color=discord.Color.orange())
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="purge", description="Executes a bulk-delete payload.")
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, amount: int):
        if amount < 1 or amount > 1000:
            return await ctx.send("❌ Please specify an amount between 1 and 1000.", ephemeral=True)
            
        await ctx.defer(ephemeral=True) # Hide the command execution
        deleted = await ctx.channel.purge(limit=amount)
        
        await self.log_mod_action(ctx, "Purge", ctx.author, f"Purged {len(deleted)} messages in #{ctx.channel.name}")
        await ctx.send(f"✅ Successfully sanitized **{len(deleted)}** messages.", ephemeral=True)

    @commands.hybrid_command(name="warn", description="Issues a formal warning. Auto-punishes on thresholds.")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ **Database Error:** Cannot process warnings right now.")
            
        # Insert warning to database
        warning_id = str(ctx.message.id) if ctx.message else str(datetime.datetime.utcnow().timestamp())
        warning_data = {
            "guild_id": ctx.guild.id,
            "user_id": member.id,
            "moderator_id": ctx.author.id,
            "reason": reason,
            "timestamp": datetime.datetime.utcnow().timestamp(),
            "warning_id": warning_id
        }
        await self.bot.db.warnings.insert_one(warning_data)
        await self.log_mod_action(ctx, "Warn", member, reason)
        
        # Calculate total warnings for Wick-style escalation
        total_warns = await self.bot.db.warnings.count_documents({"guild_id": ctx.guild.id, "user_id": member.id})
        
        try:
            await member.send(f"⚠️ You have been formally warned in **{ctx.guild.name}**.\n**Reason:** {reason}\n*You now have {total_warns} total warnings.*")
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="⚠️ Warning Issued", description=f"{member.mention} has been warned.", color=discord.Color.yellow())
        embed.add_field(name="Reason", value=reason)
        embed.set_footer(text=f"User now has {total_warns} warnings.")
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Core(bot))
