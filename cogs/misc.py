import discord
from discord.ext import commands
import datetime
import io

class Misc(commands.Cog):
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
            if settings and settings.get("misc_enabled", True) is False:
                await ctx.send("❌ The **Miscellaneous** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    @commands.hybrid_command(name="ping", description="Ping the bot and get the response time in milliseconds.")
    async def ping(self, ctx):
        start_time = datetime.datetime.utcnow()
        message = await ctx.send("🏓 Pinging network...")
        api_latency = round((datetime.datetime.utcnow() - start_time).total_seconds() * 1000)
        ws_latency = round(self.bot.latency * 1000)
        await message.edit(content=f"📡 **Network Diagnostics**\nAPI Latency: `{api_latency}ms`\nGateway Websocket: `{ws_latency}ms`")
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "ping")

    @commands.hybrid_command(name="afk", description="Set an AFK status to display when you are mentioned.")
    async def afk(self, ctx, *, reason: str = "AFK"):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        timestamp = datetime.datetime.utcnow().timestamp()
        await self.bot.db.afk.update_one({"user_id": ctx.author.id}, {"$set": {"reason": reason, "timestamp": timestamp}}, upsert=True)
        await ctx.send(f"✅ {ctx.author.mention}, your status has been updated to: **{reason}**")
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "afk")

    @commands.hybrid_command(name="serverinfo", description="Get comprehensive security and telemetry data about the server.")
    @commands.has_permissions(moderate_members=True)
    async def serverinfo(self, ctx):
        if not ctx.guild: return await ctx.send("❌ Server only command.")
        guild = ctx.guild
        
        embed = discord.Embed(title=f"Server Dossier: {guild.name}", color=0x2b2d31)
        if guild.icon: embed.set_thumbnail(url=guild.icon.url)
        
        embed.add_field(name="🛡️ Authority", value=f"**Owner:** {guild.owner.mention}\n**ID:** `{guild.owner.id}`", inline=True)
        embed.add_field(name="🆔 Network ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="📅 Inception Date", value=f"<t:{int(guild.created_at.timestamp())}:f>\n(<t:{int(guild.created_at.timestamp())}:R>)", inline=False)
        
        bots = sum(1 for m in guild.members if m.bot)
        humans = guild.member_count - bots
        embed.add_field(name="👥 Population", value=f"**Total:** {guild.member_count}\n**Humans:** {humans}\n**Automata:** {bots}", inline=True)
        embed.add_field(name="🗂️ Infrastructure", value=f"**Text:** {len(guild.text_channels)}\n**Voice:** {len(guild.voice_channels)}\n**Roles:** {len(guild.roles)}", inline=True)
        
        security_level = str(guild.verification_level).title()
        embed.add_field(name="🔒 Security Level", value=f"`{security_level}`", inline=True)
        
        await ctx.send(embed=embed)
        await self.log_telemetry(ctx.guild.id, "serverinfo")

    @commands.hybrid_command(name="whois", description="Pull a security profile on a specific user.")
    @commands.has_permissions(moderate_members=True)
    async def whois(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        
        strikes = 0
        if hasattr(self.bot, 'db'):
            strike_record = await self.bot.db.user_strikes.find_one({"guild_id": ctx.guild.id, "user_id": member.id})
            if strike_record: strikes = strike_record.get("strikes", 0)

        embed = discord.Embed(title=f"User Dossier: {member.name}", color=0x2b2d31)
        embed.set_thumbnail(url=member.display_avatar.url)
        
        embed.add_field(name="Identity", value=f"**Mention:** {member.mention}\n**ID:** `{member.id}`\n**Bot:** {'Yes' if member.bot else 'No'}", inline=True)
        embed.add_field(name="Security Status", value=f"**Active Strikes:** `{strikes}`", inline=True)
        embed.add_field(name="Timeline", value=f"**Account Created:** <t:{int(member.created_at.timestamp())}:D> (<t:{int(member.created_at.timestamp())}:R>)\n**Joined Network:** <t:{int(member.joined_at.timestamp())}:D> (<t:{int(member.joined_at.timestamp())}:R>)", inline=False)
        
        roles = [role.mention for role in reversed(member.roles[1:])] 
        roles_str = " ".join(roles) if roles else "None"
        if len(roles_str) > 1024: roles_str = roles_str[:1020] + "..."
        embed.add_field(name=f"Clearance Roles [{len(roles)}]", value=roles_str, inline=False)
        
        await ctx.send(embed=embed)
        await self.log_telemetry(ctx.guild.id, "whois")

    @commands.hybrid_command(name="avatar", description="Retrieve the high-resolution avatar of a user.")
    @commands.has_permissions(moderate_members=True)
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.defer() 
        asset = member.display_avatar.with_size(512)
        try:
            avatar_bytes = await asset.read()
            filename = f"avatar.{'gif' if asset.is_animated() else 'png'}"
            file = discord.File(io.BytesIO(avatar_bytes), filename=filename)
            embed = discord.Embed(title=f"Target: {member.name}", color=0x2b2d31)
            embed.set_image(url=f"attachment://{filename}")
            await ctx.send(embed=embed, file=file)
        except Exception:
            embed = discord.Embed(title=f"Target: {member.name}", description=f"[Direct Image Link]({member.display_avatar.url})", color=0x2b2d31)
            embed.set_image(url=member.display_avatar.url)
            await ctx.send(embed=embed)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "avatar")

    # --- ADDITIONAL MISC COMMANDS ---
    @commands.hybrid_command(name="roleinfo", description="Pull technical and security data on a specific role.")
    @commands.has_permissions(moderate_members=True)
    async def roleinfo(self, ctx, role: discord.Role):
        embed = discord.Embed(title=f"Role Dossier: {role.name}", color=role.color if role.color.value else 0x2b2d31)
        embed.add_field(name="🆔 Role ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="🎨 Color", value=f"`{str(role.color)}`", inline=True)
        embed.add_field(name="👥 Personnel Assigned", value=f"`{len(role.members)}` members", inline=True)
        embed.add_field(name="📅 Creation Date", value=f"<t:{int(role.created_at.timestamp())}:D> (<t:{int(role.created_at.timestamp())}:R>)", inline=False)
        embed.add_field(name="⚙️ Attributes", value=f"**Hoisted:** {'Yes' if role.hoist else 'No'}\n**Mentionable:** {'Yes' if role.mentionable else 'No'}\n**Managed:** {'Yes' if role.managed else 'No'}", inline=True)
        
        perms = [perm[0].replace('_', ' ').title() for perm in role.permissions if perm[1]]
        perms_str = ", ".join(perms) if perms else "None"
        if len(perms_str) > 1024: perms_str = perms_str[:1020] + "..."
        embed.add_field(name="🛡️ Key Clearances", value=f"```{perms_str}```", inline=False)
        
        await ctx.send(embed=embed)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "roleinfo")

    @commands.hybrid_command(name="channelinfo", description="Retrieve infrastructure details for a specific channel.")
    @commands.has_permissions(manage_channels=True)
    async def channelinfo(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        embed = discord.Embed(title=f"Channel Dossier: {channel.name}", color=0x2b2d31)
        embed.add_field(name="🆔 Channel ID", value=f"`{channel.id}`", inline=True)
        embed.add_field(name="📁 Category", value=f"{channel.category.name if channel.category else 'None'}", inline=True)
        embed.add_field(name="📺 Type", value=f"`{str(channel.type).title()}`", inline=True)
        embed.add_field(name="📅 Creation Date", value=f"<t:{int(channel.created_at.timestamp())}:D> (<t:{int(channel.created_at.timestamp())}:R>)", inline=False)
        embed.add_field(name="💬 Chat Settings", value=f"**NSFW:** {'Yes' if channel.is_nsfw() else 'No'}\n**Slowmode:** `{channel.slowmode_delay}s`", inline=True)

        await ctx.send(embed=embed)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "channelinfo")

    @commands.hybrid_command(name="poll", description="Create a multiple-choice poll with up to 10 options.")
    @commands.has_permissions(manage_messages=True)
    async def poll(
        self, 
        ctx, 
        message: str, 
        choice1: str, 
        choice2: str, 
        choice3: str = None, 
        choice4: str = None, 
        choice5: str = None, 
        choice6: str = None, 
        choice7: str = None, 
        choice8: str = None, 
        choice9: str = None, 
        choice10: str = None
    ):
        await ctx.defer()
        raw_choices = [choice1, choice2, choice3, choice4, choice5, choice6, choice7, choice8, choice9, choice10]
        choices = [c for c in raw_choices if c is not None and c.strip() != ""]
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        description = f"**{message}**\n\n"
        for i, choice in enumerate(choices):
            description += f"{emojis[i]} {choice}\n\n"
            
        embed = discord.Embed(description=description.strip(), color=0x2b2d31, timestamp=datetime.datetime.utcnow())
        embed.set_footer(text=f"Poll by {ctx.author.display_name}")
        
        poll_msg = await ctx.send(embed=embed)
        
        for i in range(len(choices)):
            try:
                await poll_msg.add_reaction(emojis[i])
            except discord.Forbidden:
                pass
                
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "poll")

    @commands.hybrid_command(name="color", description="Analyze a HEX color code and return its data.")
    async def color(self, ctx, hex_code: str):
        hex_code = hex_code.lstrip('#')
        if len(hex_code) != 6 or not all(c in '0123456789abcdefABCDEF' for c in hex_code):
            return await ctx.send("❌ **Syntax Error:** Please provide a valid 6-character HEX code (e.g., `#FF5733`).")
            
        color_int = int(hex_code, 16)
        embed = discord.Embed(title=f"Color Analysis: #{hex_code.upper()}", color=color_int)
        embed.add_field(name="HEX", value=f"`#{hex_code.upper()}`", inline=True)
        
        r = (color_int >> 16) & 255
        g = (color_int >> 8) & 255
        b = color_int & 255
        embed.add_field(name="RGB", value=f"`rgb({r}, {g}, {b})`", inline=True)
        
        # Uses a reliable, fast dummy image generator for the color block
        embed.set_thumbnail(url=f"https://dummyimage.com/100x100/{hex_code}/{hex_code}.png")
        
        await ctx.send(embed=embed)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "color")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not hasattr(self.bot, 'db') or not message.guild: return
        
        # --- 🛡️ GATEKEEPER CHECK FOR AFK SYSTEM ---
        is_blacklisted = await self.bot.db.global_blacklist.find_one({"target_id": message.author.id, "type": "user"})
        if is_blacklisted: return

        settings = await self.bot.db.guild_settings.find_one({"guild_id": message.guild.id})
        if settings and settings.get("misc_enabled", True) is False: return
            
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
