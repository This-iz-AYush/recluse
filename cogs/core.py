import discord
from discord.ext import commands, tasks
import datetime
from itertools import cycle

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
            embed.add_field(name="`/score stop`", value="Stops all active live trackers.", inline=False)
        elif selected == "Moderation":
            embed = discord.Embed(title="🛡️ Moderation Commands", color=discord.Color.green())
            embed.add_field(name="`/ban`, `/kick`, `/tempmute`", value="Essential disciplinary commands.", inline=False)
            embed.add_field(name="`/warn`, `/warnings`, `/delwarn`", value="Manage server strikes.", inline=False)
            embed.add_field(name="`/purge <limit>`", value="Executes a bulk-delete payload.", inline=False)
            embed.add_field(name="`/lock`, `/unlock`, `/slowmode`", value="Manage channel states.", inline=False)
        elif selected == "Miscellaneous":
            embed = discord.Embed(title="🗂️ Miscellaneous Commands", color=discord.Color.teal())
            embed.add_field(name="`/ping`, `/afk`, `/avatar`", value="User utilities.", inline=False)
            embed.add_field(name="`/serverinfo`, `/whois`, `/membercount`", value="Telemetry tools.", inline=False)
        elif selected == "Generative AI":
            embed = discord.Embed(title="🎨 Generative AI Commands", color=discord.Color.blurple())
            embed.add_field(name="`/imagine <prompt>`", value="Generates an image based on your text.", inline=False)
        elif selected == "Conversational AI":
            embed = discord.Embed(title="🤖 Conversational AI Commands", color=discord.Color.purple())
            embed.add_field(name="`@Recluse <message>`", value="Ping the bot to chat!", inline=False)
            embed.add_field(name="`/choose_ai <model>`", value="Switch your AI brain.", inline=False)
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
        await ctx.send(embed=embed, view=HelpView())

    @commands.hybrid_command(name="botinfo", description="Retrieves the application's telemetry and metadata.")
    async def botinfo(self, ctx):
        delta_uptime = datetime.datetime.utcnow() - self.bot.launch_time
        hours, remainder = divmod(int(delta_uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        
        active_ai = self.bot.get_cog('AI').user_ai_preference.get(ctx.author.id, "nexusify").title() if self.bot.get_cog('AI') else "Nexusify"
        app_info = await self.bot.application_info()
        
        embed = discord.Embed(title="System Telemetry", color=discord.Color.blue())
        embed.add_field(name="Registered Owner", value=str(app_info.owner), inline=True)
        embed.add_field(name="Websocket Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Your Active AI", value=f"🧠 **{active_ai}**", inline=True)
        embed.add_field(name="Continuous Uptime", value=f"{days}d {hours}h {minutes}m {seconds}s", inline=False)
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

async def setup(bot):
    await bot.add_cog(Core(bot))
