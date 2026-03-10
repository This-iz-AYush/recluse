import discord
from discord.ext import commands
import datetime
import re
import asyncio

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if not ctx.guild: return True
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": ctx.guild.id})
            if settings and settings.get("mod_enabled", True) is False:
                await ctx.send("❌ The **Moderation** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    @commands.hybrid_command(name="ban", description="Permanently removes a member utilizing API-level bans.")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner: return await ctx.send("❌ **Execution denied:** Hierarchy violation.")
        try:
            await ctx.guild.ban(member, reason=reason)
            await ctx.send(f"✅ {member.mention} banned. Reason: {reason}")
        except Exception as e: await ctx.send(f"❌ **Error:** Failed to ban member. `{e}`")

    @commands.hybrid_command(name="tempban", description="Temporarily removes a member from the server.")
    @commands.has_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner: return await ctx.send("❌ **Execution denied:** Hierarchy violation.")
        match = re.match(r"((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?", duration)
        if not match or not any(match.groups()): return await ctx.send("❌ **Syntax Error:** Invalid format (e.g. `1d`, `2h`).")
        
        total_seconds = int(match.group('days') or 0)*86400 + int(match.group('hours') or 0)*3600 + int(match.group('minutes') or 0)*60 + int(match.group('seconds') or 0)
        try:
            await ctx.guild.ban(member, reason=f"Tempban ({duration}): {reason}")
            await ctx.send(f"✅ {member.mention} banned for {duration}.")
        except Exception as e: return await ctx.send(f"❌ **Error:** `{e}`")
            
        await asyncio.sleep(total_seconds)
        try: await ctx.guild.unban(discord.Object(id=member.id), reason="Tempban expired.")
        except Exception: pass

    @commands.hybrid_command(name="purge", description="Executes a bulk-delete API payload to eradicate messages.")
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, limit: int):
        if limit <= 0 or limit > 1000: return await ctx.send("❌ Limit must be between 1 and 1000.")
        await ctx.defer(ephemeral=True) 
        try:
            deleted = await ctx.channel.purge(limit=limit if ctx.interaction else limit + 1)
            await ctx.send(f"✅ Successfully eradicated {max(0, len(deleted) if ctx.interaction else len(deleted)-1)} messages.", ephemeral=True, delete_after=5)
        except Exception: await ctx.send("❌ **API Error:** Failed to purge (messages might be older than 14 days).", ephemeral=True)

    @commands.hybrid_command(name="tempmute", description="Applies a native API-level timeout.")
    @commands.has_permissions(moderate_members=True)
    async def tempmute(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner: return await ctx.send("❌ **Execution denied:** Hierarchy violation.")
        match = re.match(r"((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?", duration)
        if not match or not any(match.groups()): return await ctx.send("❌ **Syntax Error:** Invalid format.")
        
        delta = datetime.timedelta(days=int(match.group('days') or 0), hours=int(match.group('hours') or 0), minutes=int(match.group('minutes') or 0), seconds=int(match.group('seconds') or 0))
        if delta.days > 28: return await ctx.send("❌ **API Restriction:** Max timeout is 28 days.")
        
        try:
            await member.timeout(delta, reason=reason)
            await ctx.send(f"✅ {member.mention} silenced for {duration}.")
        except Exception as e: await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(name="unmute", description="Removes a timeout from a member.")
    @commands.has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not member.is_timed_out(): return await ctx.send(f"❌ {member.mention} is not muted.")
        try:
            await member.timeout(None, reason=reason)
            await ctx.send(f"✅ Unmuted {member.mention}.")
        except Exception as e: await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(name="vckick", description="Forcefully terminates a user's voice connection.")
    async def vckick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not (ctx.author == ctx.guild.owner or ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.move_members): return await ctx.send("❌ **Denied:** Missing `Move Members` permission.")
        if member.voice and member.voice.channel:
            try:
                await member.move_to(None, reason=reason)
                await ctx.send(f"✅ {member.mention} disconnected from voice.")
            except Exception: await ctx.send("❌ **Error:** Failed to disconnect.")
        else: await ctx.send("❌ **Error:** Target not in a voice channel.")

    @commands.hybrid_command(name="kick", description="Kicks a member from the server.")
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner: return await ctx.send("❌ **Execution denied:** Hierarchy violation.")
        try:
            await ctx.guild.kick(member, reason=reason)
            await ctx.send(f"✅ {member.mention} kicked.")
        except Exception as e: await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(name="unban", description="Unbans a user via their User ID.")
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: str, *, reason: str = "No reason provided"):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user, reason=reason)
            await ctx.send(f"✅ Unbanned **{user.name}**.")
        except Exception: await ctx.send("❌ **Failure:** User not found or not banned.")

    @commands.hybrid_command(name="lock", description="Locks the current channel.")
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
            await ctx.send(f"🔒 {channel.mention} locked.")
        except Exception: await ctx.send("❌ **Failure:** Permission denied.")

    @commands.hybrid_command(name="unlock", description="Unlocks a previously locked channel.")
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
            await ctx.send(f"🔓 {channel.mention} unlocked.")
        except Exception: await ctx.send("❌ **Failure:** Permission denied.")

    @commands.hybrid_command(name="warn", description="Warns a member. Auto-mutes after 3 warnings.")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner: return await ctx.send("❌ **Execution denied:** Hierarchy violation.")
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        
        warn_id = int(datetime.datetime.utcnow().timestamp())
        await self.bot.db.warnings.insert_one({'guild_id': ctx.guild.id, 'user_id': member.id, 'warn_id': warn_id, 'reason': reason, 'timestamp': warn_id, 'moderator': ctx.author.id})
        total_warns = await self.bot.db.warnings.count_documents({"guild_id": ctx.guild.id, "user_id": member.id})
        
        msg = f"⚠️ {member.mention} warned: **{reason}** (#{total_warns})"
        if total_warns > 0 and total_warns % 3 == 0:
            try:
                await member.timeout(datetime.timedelta(hours=1), reason=f"Auto-Mod: {total_warns} warnings.")
                msg += f"\n🔇 **Auto-Mod:** Muted for 1 hour for reaching {total_warns} warnings."
            except Exception: msg += "\n❌ **Auto-Mod:** Missing permissions to apply timeout."
        await ctx.send(msg)

    @commands.hybrid_command(name="warnings", description="View all warnings for a member.")
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx, member: discord.Member):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        user_warns = await self.bot.db.warnings.find({"guild_id": ctx.guild.id, "user_id": member.id}).sort("timestamp", 1).to_list(length=100)
        
        if not user_warns: return await ctx.send(f"✅ Clean record for {member.display_name}.")
        embed = discord.Embed(title=f"Warnings for {member.display_name}", color=discord.Color.orange())
        for w in user_warns: embed.add_field(name=f"ID: {w['warn_id']}", value=f"**Reason:** {w['reason']}\n<t:{int(w['timestamp'])}:R> | Mod: <@{w['moderator']}>", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="delwarn", description="Removes a specific warning.")
    @commands.has_permissions(moderate_members=True)
    async def delwarn(self, ctx, member: discord.Member, warning_id: int):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        result = await self.bot.db.warnings.delete_one({"guild_id": ctx.guild.id, "user_id": member.id, "warn_id": warning_id})
        await ctx.send(f"✅ Deleted warning `{warning_id}`." if result.deleted_count > 0 else f"❌ Could not find warning `{warning_id}`.")

    @commands.hybrid_command(name="clean", description="Cleans up the bot's own responses.")
    @commands.has_permissions(manage_messages=True)
    async def clean(self, ctx, limit: int = 50):
        if limit <= 0 or limit > 100: return await ctx.send("❌ Limit 1-100.")
        await ctx.defer(ephemeral=True)
        try:
            deleted = await ctx.channel.purge(limit=limit, check=lambda m: m.author == self.bot.user)
            await ctx.send(f"✅ Swept away {len(deleted)} of my own messages.", ephemeral=True, delete_after=5)
        except Exception: await ctx.send("❌ **Error:** Failed to clean.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Moderation(bot))
