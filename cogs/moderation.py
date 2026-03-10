import discord
from discord.ext import commands
import datetime
import re
import asyncio

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="ban", description="Permanently removes a member utilizing API-level bans.")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No administrative reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ **Execution denied:** The target possesses an equal or superior hierarchy role.")
        try:
            await ctx.guild.ban(member, reason=reason)
            await ctx.send(f"✅ {member.mention} has been permanently excised from the server. Reason: {reason}")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I do not have permission to ban this member (role hierarchy or missing permissions).")
        except discord.HTTPException as e:
            await ctx.send(f"❌ **API Error:** Failed to ban member. `{e.text}`")

    @commands.hybrid_command(name="tempban", description="Temporarily removes a member from the server.")
    @commands.has_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ **Execution denied:** Role hierarchy validation failed.")
        
        time_regex = re.compile(r"((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?")
        match = time_regex.match(duration)
        
        if not match or not any(match.groups()):
            return await ctx.send("❌ **Syntax Error:** Invalid temporal format. Acceptable format example: `1d`, `2h`, `30m`.")
        
        days = int(match.group('days') or 0)
        hours = int(match.group('hours') or 0)
        minutes = int(match.group('minutes') or 0)
        seconds = int(match.group('seconds') or 0)
        
        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        
        try:
            await ctx.guild.ban(member, reason=f"Tempban ({duration}): {reason}")
            await ctx.send(f"✅ {member.mention} has been temporarily banned for {duration}. Reason: {reason}\n*(Note: If the bot restarts during this period, the unban will need to be done manually.)*")
        except discord.Forbidden:
            return await ctx.send("❌ **Failure:** I lack adequate role hierarchy permissions to ban this user.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ **API Error:** `{e.text}`")
            
        await asyncio.sleep(total_seconds)
        
        try:
            user = discord.Object(id=member.id)
            await ctx.guild.unban(user, reason=f"Tempban expired after {duration}.")
        except discord.NotFound:
            pass 
        except Exception:
            pass

    @commands.hybrid_command(name="purge", description="Executes a bulk-delete API payload to eradicate messages.")
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, limit: int):
        if limit <= 0 or limit > 1000:
            return await ctx.send("❌ Limit must be between 1 and 1000.")
            
        await ctx.defer(ephemeral=True) 
        
        search_limit = limit if ctx.interaction else limit + 1
        try:
            deleted = await ctx.channel.purge(limit=search_limit)
            deleted_count = len(deleted) if ctx.interaction else len(deleted) - 1
            await ctx.send(f"✅ Successfully eradicated {max(0, deleted_count)} messages.", ephemeral=True, delete_after=5)
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I lack the `Manage Messages` permission in this channel.", ephemeral=True)
        except discord.HTTPException:
            await ctx.send(f"❌ **API Error:** Failed to purge messages. Note: Messages older than 14 days cannot be bulk-deleted.", ephemeral=True)

    @commands.hybrid_command(name="tempmute", description="Applies a native API-level timeout.")
    @commands.has_permissions(moderate_members=True)
    async def tempmute(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ **Execution denied:** Role hierarchy validation failed.")
        
        time_regex = re.compile(r"((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?")
        match = time_regex.match(duration)
        
        if not match or not any(match.groups()):
            return await ctx.send("❌ **Syntax Error:** Invalid temporal format. Acceptable format example: `1d`, `2h`, `30m`.")
        
        days = int(match.group('days') or 0)
        hours = int(match.group('hours') or 0)
        minutes = int(match.group('minutes') or 0)
        seconds = int(match.group('seconds') or 0)
        
        delta = datetime.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        
        if delta.days > 28:
            return await ctx.send("❌ **API Restriction:** Temporal bounds exceeded. Maximum duration is 28 days.")
        
        try:
            await member.timeout(delta, reason=reason)
            await ctx.send(f"✅ {member.mention} has been successfully silenced for {duration}. Reason: {reason}")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I lack adequate role hierarchy permissions to timeout this user.")
        except discord.HTTPException as e:
            await ctx.send(f"❌ **API Error:** `{e.text}`")

    @commands.hybrid_command(name="unmute", description="Removes a timeout from a member.")
    @commands.has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not member.is_timed_out():
            return await ctx.send(f"❌ {member.mention} is not currently muted.")
        try:
            await member.timeout(None, reason=reason)
            await ctx.send(f"✅ Successfully unmuted {member.mention}. Reason: {reason}")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I lack adequate role hierarchy permissions to unmute this user.")
        except discord.HTTPException as e:
            await ctx.send(f"❌ **API Error:** `{e.text}`")

    @commands.hybrid_command(name="vckick", description="Forcefully terminates a user's voice connection.")
    async def vckick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not (ctx.author == ctx.guild.owner or ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.move_members):
            return await ctx.send("❌ **Execution Denied:** You lack the `Move Members` permission.")
        
        if member.voice and member.voice.channel:
            try:
                await member.move_to(None, reason=reason)
                await ctx.send(f"✅ {member.mention} has been forcefully disconnected from voice communications.")
            except discord.Forbidden:
                 await ctx.send("❌ **Failure:** I lack permissions to disconnect this user.")
            except discord.HTTPException:
                 await ctx.send("❌ **API Error:** Failed to disconnect the user.")
        else:
            await ctx.send("❌ **Execution Error:** Target is not currently connected to an active voice channel.")

    @commands.hybrid_command(name="kick", description="Kicks a member from the server.")
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ **Execution denied:** The target possesses an equal or superior hierarchy role.")
        try:
            await ctx.guild.kick(member, reason=reason)
            await ctx.send(f"✅ {member.mention} has been removed from the server. Reason: {reason}")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I do not have permission to kick this member (role hierarchy or missing permissions).")
        except discord.HTTPException as e:
            await ctx.send(f"❌ **API Error:** Failed to kick member. `{e.text}`")

    @commands.hybrid_command(name="unban", description="Unbans a user via their User ID.")
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: str, *, reason: str = "No reason provided"):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user, reason=reason)
            await ctx.send(f"✅ Successfully unbanned **{user.name}**. Reason: {reason}")
        except ValueError:
            await ctx.send("❌ **Invalid ID:** Please provide a valid numeric User ID.")
        except discord.NotFound:
            await ctx.send("❌ **Failure:** User not found or not currently banned.")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I lack permissions to unban users.")

    @commands.hybrid_command(name="lock", description="Locks the current channel, preventing members from sending messages.")
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {ctx.author}")
            await ctx.send(f"🔒 {channel.mention} has been locked. Members can no longer send messages here.")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I do not have permission to manage this channel.")

    @commands.hybrid_command(name="unlock", description="Unlocks a previously locked channel.")
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {ctx.author}")
            await ctx.send(f"🔓 {channel.mention} has been unlocked. Normal messaging permissions restored.")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I do not have permission to manage this channel.")

    @commands.hybrid_command(name="slowmode", description="Sets the slowmode delay for the current channel.")
    @commands.has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: int):
        if seconds < 0 or seconds > 21600:
            return await ctx.send("❌ Slowmode must be between 0 and 21600 seconds (6 hours).")
        try:
            await ctx.channel.edit(slowmode_delay=seconds)
            if seconds == 0:
                await ctx.send("✅ Slowmode has been **disabled** in this channel.")
            else:
                await ctx.send(f"✅ Slowmode set to **{seconds} seconds**.")
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I lack permissions to edit this channel.")

    @commands.hybrid_command(name="warn", description="Warns a member. Auto-mutes after 3 warnings.")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ **Execution denied:** Role hierarchy validation failed.")
        
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ The database is currently disconnected.")
            
        guild_id = ctx.guild.id
        user_id = member.id
        
        # Create a unique warning ID using the current timestamp integer
        warn_id = int(datetime.datetime.utcnow().timestamp())
        
        warn_doc = {
            'guild_id': guild_id,
            'user_id': user_id,
            'warn_id': warn_id,
            'reason': reason,
            'timestamp': datetime.datetime.utcnow().timestamp(),
            'moderator': ctx.author.id
        }
        
        # Insert into MongoDB
        await self.bot.db.warnings.insert_one(warn_doc)
        
        # Count how many warnings they have in total
        total_warns = await self.bot.db.warnings.count_documents({"guild_id": guild_id, "user_id": user_id})
        
        msg = f"⚠️ {member.mention} has been warned for: **{reason}** (Warning #{total_warns})"
        
        # Auto-mute logic
        if total_warns > 0 and total_warns % 3 == 0:
            delta = datetime.timedelta(hours=1)
            try:
                await member.timeout(delta, reason=f"Automatic Mod: Reached {total_warns} warnings.")
                msg += f"\n🔇 **Auto-Mod:** User reached {total_warns} warnings and was automatically muted for 1 hour."
            except Exception:
                msg += f"\n❌ **Auto-Mod:** Tried to mute user for reaching {total_warns} warnings, but I lack permissions."
                
        await ctx.send(msg)

    @commands.hybrid_command(name="warnings", description="View all warnings for a member.")
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx, member: discord.Member):
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ The database is currently disconnected.")
            
        guild_id = ctx.guild.id
        user_id = member.id
        
        # Fetch all warnings for this user, sorted by oldest first
        cursor = self.bot.db.warnings.find({"guild_id": guild_id, "user_id": user_id}).sort("timestamp", 1)
        user_warns = await cursor.to_list(length=100)
        
        if not user_warns:
            return await ctx.send(f"✅ {member.display_name} has a clean record with 0 active warnings.")
            
        embed = discord.Embed(title=f"Warnings for {member.display_name}", color=discord.Color.orange())
        for w in user_warns:
            timestamp = f"<t:{int(w['timestamp'])}:R>"
            embed.add_field(
                name=f"Warning ID: {w['warn_id']}", 
                value=f"**Reason:** {w['reason']}\n**Given:** {timestamp}\n**Mod:** <@{w['moderator']}>", 
                inline=False
            )
            
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="delwarn", description="Removes a specific warning from a user by ID.")
    @commands.has_permissions(moderate_members=True)
    async def delwarn(self, ctx, member: discord.Member, warning_id: int):
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ The database is currently disconnected.")
            
        guild_id = ctx.guild.id
        user_id = member.id
        
        # Delete exactly one warning that matches the ID
        result = await self.bot.db.warnings.delete_one({"guild_id": guild_id, "user_id": user_id, "warn_id": warning_id})
        
        if result.deleted_count > 0:
            await ctx.send(f"✅ Successfully deleted warning `{warning_id}` for {member.mention}.")
        else:
            await ctx.send(f"❌ Could not find warning `{warning_id}` for that user.")

    @commands.hybrid_command(name="moderations", description="Lists active timed moderations (mutes) in the server.")
    @commands.has_permissions(moderate_members=True)
    async def active_moderations(self, ctx):
        await ctx.defer()
        muted_members = [m for m in ctx.guild.members if m.is_timed_out()]
        
        if not muted_members:
            return await ctx.send("✅ There are currently no active timed moderations (mutes) in this server.")
            
        embed = discord.Embed(title="Active Timed Moderations", color=discord.Color.red())
        
        for m in muted_members[:25]:
            timeout_end = m.timed_out_until
            timestamp = f"<t:{int(timeout_end.timestamp())}:R>"
            embed.add_field(name=str(m), value=f"Unmuted {timestamp}", inline=False)
            
        if len(muted_members) > 25:
            embed.set_footer(text=f"And {len(muted_members) - 25} more...")
            
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="members", description="Lists members in a specific role.")
    @commands.has_permissions(manage_roles=True)
    async def list_members(self, ctx, role: discord.Role):
        members = role.members
        if not members:
            return await ctx.send(f"ℹ️ Nobody currently has the {role.mention} role.")
            
        member_mentions = [m.mention for m in members]
        chunks = []
        current_chunk = ""
        for mention in member_mentions:
            if len(current_chunk) + len(mention) + 2 > 1900:
                chunks.append(current_chunk)
                current_chunk = mention + ", "
            else:
                current_chunk += mention + ", "
        
        if current_chunk:
            chunks.append(current_chunk.rstrip(", "))
            
        for i, chunk in enumerate(chunks):
            if i == 0:
                await ctx.send(f"👥 **Members with the {role.name} role ({len(members)} total):**\n{chunk}")
            else:
                await ctx.send(chunk)

    @commands.hybrid_command(name="clean", description="Cleans up the bot's own responses in the channel.")
    @commands.has_permissions(manage_messages=True)
    async def clean(self, ctx, limit: int = 50):
        if limit <= 0 or limit > 100:
            return await ctx.send("❌ Limit must be between 1 and 100.")
            
        await ctx.defer(ephemeral=True)
        
        def is_me(m):
            return m.author == self.bot.user
            
        try:
            deleted = await ctx.channel.purge(limit=limit, check=is_me)
            await ctx.send(f"✅ Swept away {len(deleted)} of my own messages.", ephemeral=True, delete_after=5)
        except discord.Forbidden:
            await ctx.send("❌ **Failure:** I lack the `Manage Messages` permission here.", ephemeral=True)
        except discord.HTTPException:
            await ctx.send("❌ **API Error:** Failed to clean messages.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Moderation(bot))