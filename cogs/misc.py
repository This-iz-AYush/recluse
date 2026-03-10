import discord
from discord.ext import commands
import datetime
import io

class Misc(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if not ctx.guild: return True
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": ctx.guild.id})
            if settings and settings.get("misc_enabled", True) is False:
                await ctx.send("❌ The **Miscellaneous** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    @commands.hybrid_command(name="ping", description="Ping the bot and get the response time in milliseconds.")
    async def ping(self, ctx):
        start_time = datetime.datetime.utcnow()
        message = await ctx.send("🏓 Pinging...")
        api_latency = round((datetime.datetime.utcnow() - start_time).total_seconds() * 1000)
        ws_latency = round(self.bot.latency * 1000)
        await message.edit(content=f"🏓 **Pong!**\n📡 API Latency: `{api_latency}ms`\n🖥️ Websocket Latency: `{ws_latency}ms`")

    @commands.hybrid_command(name="afk", description="Set an AFK status to display when you are mentioned.")
    async def afk(self, ctx, *, reason: str = "AFK"):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        timestamp = datetime.datetime.utcnow().timestamp()
        await self.bot.db.afk.update_one({"user_id": ctx.author.id}, {"$set": {"reason": reason, "timestamp": timestamp}}, upsert=True)
        await ctx.send(f"✅ {ctx.author.mention}, I've set your AFK status: **{reason}**")

    @commands.hybrid_command(name="avatar", description="Get the avatar of yourself or another user.")
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.defer() 
        asset = member.display_avatar.with_size(512)
        try:
            avatar_bytes = await asset.read()
            filename = f"avatar.{'gif' if asset.is_animated() else 'png'}"
            file = discord.File(io.BytesIO(avatar_bytes), filename=filename)
            embed = discord.Embed(title=f"{member.display_name}'s Avatar", color=member.color)
            embed.set_image(url=f"attachment://{filename}")
            await ctx.send(embed=embed, file=file)
        except Exception as e:
            embed = discord.Embed(title=f"{member.display_name}'s Avatar", description=f"[Click here to view full image]({member.display_avatar.url})", color=member.color)
            embed.set_image(url=member.display_avatar.url)
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="membercount", description="Get the membercount of the current server.")
    async def membercount(self, ctx):
        if not ctx.guild: return await ctx.send("❌ This command must be used inside a server.")
        total, bots = ctx.guild.member_count, sum(1 for m in ctx.guild.members if m.bot)
        embed = discord.Embed(title=f"👥 Member Count for {ctx.guild.name}", color=discord.Color.teal())
        embed.add_field(name="Total", value=str(total), inline=True)
        embed.add_field(name="Humans", value=str(total - bots), inline=True)
        embed.add_field(name="Bots", value=str(bots), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="serverinfo", description="Get information about the current server.")
    async def serverinfo(self, ctx):
        if not ctx.guild: return await ctx.send("❌ Server only command.")
        guild = ctx.guild
        embed = discord.Embed(title=f"Server Information: {guild.name}", color=discord.Color.gold())
        if guild.icon: embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="👑 Owner", value=guild.owner.mention, inline=True)
        embed.add_field(name="🆔 ID", value=guild.id, inline=True)
        embed.add_field(name="📅 Created On", value=f"<t:{int(guild.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="👥 Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="🎭 Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="💬 Channels", value=str(len(guild.channels)), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="whois", description="Get information about a user.")
    async def whois(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        embed = discord.Embed(title=f"User Info: {member}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=member.id, inline=False)
        embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
        roles = [role.mention for role in reversed(member.roles[1:])] 
        roles_str = " ".join(roles) if roles else "None"
        if len(roles_str) > 1024: roles_str = roles_str[:1020] + "..."
        embed.add_field(name=f"Roles [{len(roles)}]", value=roles_str, inline=False)
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not hasattr(self.bot, 'db') or not message.guild: return

        # Verify the Misc module is active before processing AFK logic
        settings = await self.bot.db.guild_settings.find_one({"guild_id": message.guild.id})
        if settings and settings.get("misc_enabled", True) is False:
            return
            
        afk_data = await self.bot.db.afk.find_one({"user_id": message.author.id})
        if afk_data:
            await self.bot.db.afk.delete_one({"user_id": message.author.id})
            duration = datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(afk_data.get("timestamp"))
            mins, secs = divmod(int(duration.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m {secs}s"
            welcome_msg = await message.channel.send(f"👋 Welcome back {message.author.mention}! You were AFK for {time_str}.")
            await welcome_msg.delete(delay=10) 
            
        for mentioned in message.mentions:
            afk_row = await self.bot.db.afk.find_one({"user_id": mentioned.id})
            if afk_row:
                await message.channel.send(f"💤 **{mentioned.display_name}** is currently AFK: {afk_row.get('reason')} *(since <t:{int(afk_row.get('timestamp'))}:R>)*")

async def setup(bot):
    await bot.add_cog(Misc(bot))
