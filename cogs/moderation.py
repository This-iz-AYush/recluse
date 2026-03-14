import discord
from discord.ext import commands
import datetime
import re
import asyncio

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
            if settings and settings.get("mod_enabled", True) is False:
                await ctx.send("❌ The **Moderation** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    # --- WICK-STYLE SECURITY HELPERS ---
    async def hierarchy_check(self, ctx, member: discord.Member) -> bool:
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
    @commands.hybrid_command(
        name="ban", 
        description="Permanently removes a member utilizing API-level bans.",
        usage="/ban <member> [reason]",
        help="/ban @Spammer Sending malicious links"
    )
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided."):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        try: await member.send(f"🔨 You have been banned from **{ctx.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden: pass 
            
        try:
            await member.ban(reason=f"Action by {ctx.author} | {reason}")
            await self.log_mod_action(ctx, "Ban", member, reason)
            
            embed = discord.Embed(title="🔨 Target Neutralized", description=f"Successfully banned {member.mention}.", color=discord.Color.red())
            embed.add_field(name="Reason", value=reason)
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ **Error:** Failed to ban member. `{e}`")

    @commands.hybrid_command(
        name="tempban", 
        description="Temporarily removes a member from the server.",
        usage="/tempban <member> <duration> [reason]",
        help="/tempban @User 3d Repeated rule violations"
    )
    @commands.has_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if not await self.hierarchy_check(ctx, member): return
        match = re.match(r"((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?", duration)
        if not match or not any(match.groups()): return await ctx.send("❌ **Syntax Error:** Invalid format (e.g. `1d`, `2h`).")
        
        total_seconds = int(match.group('days') or 0)*86400 + int(match.group('hours') or 0)*3600 + int(match.group('minutes') or 0)*60 + int(match.group('seconds') or 0)
        try:
            await ctx.guild.ban(member, reason=f"Tempban ({duration}): {reason}")
            await self.log_mod_action(ctx, f"Tempban ({duration})", member, reason)
            await ctx.send(f"✅ {member.mention} banned for {duration}.")
        except Exception as e: return await ctx.send(f"❌ **Error:** `{e}`")
            
        await asyncio.sleep(total_seconds)
        try: await ctx.guild.unban(discord.Object(id=member.id), reason="Tempban expired.")
        except Exception: pass

    @commands.hybrid_command(
        name="kick", 
        description="Kicks a member from the server.",
        usage="/kick <member> [reason]",
        help="/kick @User Ignoring staff warnings"
    )
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided."):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        try: await member.send(f"👢 You have been kicked from **{ctx.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden: pass
            
        try:
            await member.kick(reason=f"Action by {ctx.author} | {reason}")
            await self.log_mod_action(ctx, "Kick", member, reason)
            
            embed = discord.Embed(title="👢 Target Expelled", description=f"Successfully kicked {member.mention}.", color=discord.Color.orange())
            embed.add_field(name="Reason", value=reason)
            await ctx.send(embed=embed)
        except Exception as e: await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(
        name="purge", 
        description="Executes a bulk-delete payload.",
        usage="/purge <limit>",
        help="/purge 50"
    )
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, limit: int):
        if limit < 1 or limit > 1000: return await ctx.send("❌ Please specify an amount between 1 and 1000.", ephemeral=True)
        await ctx.defer(ephemeral=True) 
        try:
            deleted = await ctx.channel.purge(limit=limit if ctx.interaction else limit + 1)
            await self.log_mod_action(ctx, "Purge", ctx.author, f"Purged {len(deleted)} messages in #{ctx.channel.name}")
            await ctx.send(f"✅ Successfully sanitized **{max(0, len(deleted) if ctx.interaction else len(deleted)-1)}** messages.", ephemeral=True, delete_after=5)
        except Exception: 
            await ctx.send("❌ **API Error:** Failed to purge (messages might be older than 14 days).", ephemeral=True)

    @commands.hybrid_command(
        name="tempmute", 
        description="Applies a native API-level timeout.",
        usage="/tempmute <member> <duration> [reason]",
        help="/tempmute @User 12h Spamming chat"
    )
    @commands.has_permissions(moderate_members=True)
    async def tempmute(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if not await self.hierarchy_check(ctx, member): return
        match = re.match(r"((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?", duration)
        if not match or not any(match.groups()): return await ctx.send("❌ **Syntax Error:** Invalid format.")
        
        delta = datetime.timedelta(days=int(match.group('days') or 0), hours=int(match.group('hours') or 0), minutes=int(match.group('minutes') or 0), seconds=int(match.group('seconds') or 0))
        if delta.days > 28: return await ctx.send("❌ **API Restriction:** Max timeout is 28 days.")
        
        try:
            await member.timeout(delta, reason=reason)
            await self.log_mod_action(ctx, f"Timeout ({duration})", member, reason)
            await ctx.send(f"✅ {member.mention} silenced for {duration}.")
        except Exception as e: await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(
        name="unmute", 
        description="Removes a timeout from a member.",
        usage="/unmute <member> [reason]",
        help="/unmute @User Appealed in DMs"
    )
    @commands.has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not member.is_timed_out(): return await ctx.send(f"❌ {member.mention} is not muted.")
        try:
            await member.timeout(None, reason=reason)
            await self.log_mod_action(ctx, "Unmute", member, reason)
            await ctx.send(f"✅ Unmuted {member.mention}.")
        except Exception as e: await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(
        name="vckick", 
        description="Forcefully terminates a user's voice connection.",
        usage="/vckick <member> [reason]",
        help="/vckick @User Hot mic"
    )
    @commands.has_permissions(moderate_members=True)
    async def vckick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not (ctx.author == ctx.guild.owner or ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.move_members): return await ctx.send("❌ **Denied:** Missing `Move Members` permission.")
        if member.voice and member.voice.channel:
            try:
                await member.move_to(None, reason=reason)
                await self.log_mod_action(ctx, "VC Kick", member, reason)
                await ctx.send(f"✅ {member.mention} disconnected from voice.")
            except Exception: await ctx.send("❌ **Error:** Failed to disconnect.")
        else: await ctx.send("❌ **Error:** Target not in a voice channel.")

    @commands.hybrid_command(
        name="unban", 
        description="Unbans a user via their User ID.",
        usage="/unban <user_id> [reason]",
        help="/unban 123456789012345678 Apologized"
    )
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: str, *, reason: str = "No reason provided"):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user, reason=reason)
            await self.log_mod_action(ctx, "Unban", user, reason)
            await ctx.send(f"✅ Unbanned **{user.name}**.")
        except Exception: await ctx.send("❌ **Failure:** User not found or not banned.")

    @commands.hybrid_command(
        name="lock", 
        description="Locks the current channel.",
        usage="/lock [channel]",
        help="/lock #general-chat"
    )
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
            await ctx.send(f"🔒 {channel.mention} locked.")
        except Exception: await ctx.send("❌ **Failure:** Permission denied.")

    @commands.hybrid_command(
        name="unlock", 
        description="Unlocks a previously locked channel.",
        usage="/unlock [channel]",
        help="/unlock #general-chat"
    )
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
            await ctx.send(f"🔓 {channel.mention} unlocked.")
        except Exception: await ctx.send("❌ **Failure:** Permission denied.")

    @commands.hybrid_command(
        name="warn", 
        description="Issues a formal warning. Auto-punishes on thresholds.",
        usage="/warn <member> <reason>",
        help="/warn @User Disrespecting staff"
    )
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ **Database Error:** Cannot process warnings right now.")
            
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
        
        total_warns = await self.bot.db.warnings.count_documents({"guild_id": ctx.guild.id, "user_id": member.id})
        
        try: await member.send(f"⚠️ You have been formally warned in **{ctx.guild.name}**.\n**Reason:** {reason}\n*You now have {total_warns} total warnings.*")
        except discord.Forbidden: pass

        embed = discord.Embed(title="⚠️ Warning Issued", description=f"{member.mention} has been warned.", color=discord.Color.yellow())
        embed.add_field(name="Reason", value=reason)
        
        footer_text = f"User now has {total_warns} warnings."
        if total_warns > 0 and total_warns % 3 == 0:
            try:
                await member.timeout(datetime.timedelta(hours=1), reason=f"Auto-Mod: {total_warns} warnings.")
                footer_text += " | 🔇 Auto-Muted for 1 hour."
            except Exception: pass
            
        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="warnings", 
        description="View all warnings for a member.",
        usage="/warnings <member>",
        help="/warnings @User"
    )
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx, member: discord.Member):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        user_warns = await self.bot.db.warnings.find({"guild_id": ctx.guild.id, "user_id": member.id}).sort("timestamp", 1).to_list(length=100)
        
        if not user_warns: return await ctx.send(f"✅ Clean record for {member.display_name}.")
        embed = discord.Embed(title=f"Warnings for {member.display_name}", color=discord.Color.orange())
        for w in user_warns: 
            embed.add_field(name=f"ID: {w.get('warning_id', 'Unknown')}", value=f"**Reason:** {w['reason']}\n<t:{int(w['timestamp'])}:R> | Mod: <@{w.get('moderator_id', 'Unknown')}>", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="delwarn", 
        description="Removes a specific warning.",
        usage="/delwarn <member> <warning_id>",
        help="/delwarn @User 1258900481812"
    )
    @commands.has_permissions(moderate_members=True)
    async def delwarn(self, ctx, member: discord.Member, warning_id: str):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        result = await self.bot.db.warnings.delete_one({"guild_id": ctx.guild.id, "user_id": member.id, "warning_id": warning_id})
        await ctx.send(f"✅ Deleted warning `{warning_id}`." if result.deleted_count > 0 else f"❌ Could not find warning `{warning_id}`.")

    @commands.hybrid_command(
        name="clean", 
        description="Cleans up the bot's own responses.",
        usage="/clean [limit]",
        help="/clean 50"
    )
    @commands.has_permissions(manage_messages=True)
    async def clean(self, ctx, limit: int = 50):
        if limit <= 0 or limit > 100: return await ctx.send("❌ Limit 1-100.")   
        await ctx.defer(ephemeral=True)
        count = 0
        def is_me(m):
            nonlocal count           
            if count >= limit: return False               
            if m.author == self.bot.user:
                count += 1
                return True
            return False            
        try:
            deleted = await ctx.channel.purge(limit=200, check=is_me)
            await ctx.send(f"✅ Swept away {len(deleted)} of my own messages.", ephemeral=True, delete_after=5)
        except Exception: 
            await ctx.send("❌ **Error:** Failed to clean.", ephemeral=True)

    @commands.hybrid_command(
        name="softban", 
        description="Bans and immediately unbans to clear recent messages.",
        usage="/softban <member> [reason]",
        help="/softban @Spammer Raid account"
    )
    @commands.has_permissions(ban_members=True)
    async def softban(self, ctx, member: discord.Member, *, reason: str = "No reason provided."):
        await ctx.defer()
        if not await self.hierarchy_check(ctx, member): return
        
        try: await member.send(f"🔨 You have been softbanned from **{ctx.guild.name}** to clear your messages.\n**Reason:** {reason}")
        except discord.Forbidden: pass
            
        try:
            # Ban with message deletion (7 days is the max standard)
            await member.ban(reason=f"Softban by {ctx.author} | {reason}", delete_message_days=7)
            # Immediately unban
            await ctx.guild.unban(member, reason="Softban release")
            
            await self.log_mod_action(ctx, "Softban", member, reason)
            await ctx.send(f"✅ Successfully softbanned {member.mention}.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** Failed to softban member. `{e}`")

    @commands.hybrid_command(
        name="slowmode", 
        description="Sets the slowmode delay for the current channel.",
        usage="/slowmode <seconds> [channel]",
        help="/slowmode 15 #general"
    )
    @commands.has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: int, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        if seconds < 0 or seconds > 21600: 
            return await ctx.send("❌ **Error:** Slowmode delay must be between 0 and 21600 seconds (6 hours).", ephemeral=True)
        
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Action by {ctx.author}")
            if seconds == 0:
                await ctx.send(f"✅ Disabled slowmode in {channel.mention}.")
            else:
                await ctx.send(f"✅ Set slowmode in {channel.mention} to **{seconds} seconds**.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** Failed to set slowmode. `{e}`")

    @commands.hybrid_command(
        name="role", 
        description="Toggles a role for a member (adds if they don't have it, removes if they do).",
        usage="/role <member> <role>",
        help="/role @User @VIP"
    )
    @commands.has_permissions(manage_roles=True)
    async def role(self, ctx, member: discord.Member, role: discord.Role):
        # Specific hierarchy checks for role assignment
        if ctx.author != ctx.guild.owner and role.position >= ctx.author.top_role.position:
            return await ctx.send("❌ **Hierarchy Error:** You cannot manage a role higher than or equal to your own top role.", ephemeral=True)
        if role.position >= ctx.guild.me.top_role.position:
            return await ctx.send("❌ **Execution Blocked:** That role is higher than or equal to my highest role.", ephemeral=True)

        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"Action by {ctx.author}")
                await ctx.send(f"✅ Removed `{role.name}` from {member.mention}.")
            else:
                await member.add_roles(role, reason=f"Action by {ctx.author}")
                await ctx.send(f"✅ Added `{role.name}` to {member.mention}.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(
        name="clearwarns", 
        description="Clears all database warnings for a user.",
        usage="/clearwarns <member>",
        help="/clearwarns @User"
    )
    @commands.has_permissions(moderate_members=True)
    async def clearwarns(self, ctx, member: discord.Member):
        if not hasattr(self.bot, 'db'): return await ctx.send("❌ Database disconnected.")
        
        try:
            result = await self.bot.db.warnings.delete_many({"guild_id": ctx.guild.id, "user_id": member.id})
            await self.log_mod_action(ctx, "Clear Warnings", member, f"Cleared {result.deleted_count} warnings")
            await ctx.send(f"✅ Successfully cleared **{result.deleted_count}** warnings for {member.mention}.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** Failed to clear warnings. `{e}`")

    @commands.hybrid_command(
        name="nick", 
        description="Changes or resets a member's nickname.",
        usage="/nick <member> [nickname]",
        help="/nick @User CoolGuy99"
    )
    @commands.has_permissions(manage_nicknames=True)
    async def nick(self, ctx, member: discord.Member, *, nickname: str = None):
        if not await self.hierarchy_check(ctx, member): return
        
        try:
            await member.edit(nick=nickname, reason=f"Action by {ctx.author}")
            if nickname:
                await ctx.send(f"✅ Changed {member.mention}'s nickname to **{nickname}**.")
            else:
                await ctx.send(f"✅ Reset {member.mention}'s nickname.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(
        name="vmute", 
        description="Server-mutes a member in voice channels.",
        usage="/vmute <member> [reason]",
        help="/vmute @User Playing loud music"
    )
    @commands.has_permissions(mute_members=True)
    async def vmute(self, ctx, member: discord.Member, *, reason: str = "No reason provided."):
        if not await self.hierarchy_check(ctx, member): return
        
        try:
            await member.edit(mute=True, reason=f"Action by {ctx.author} | {reason}")
            await self.log_mod_action(ctx, "Voice Mute", member, reason)
            await ctx.send(f"✅ Voice muted {member.mention}.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** `{e}`")

    @commands.hybrid_command(
        name="vunmute", 
        description="Removes a server voice mute from a member.",
        usage="/vunmute <member> [reason]",
        help="/vunmute @User"
    )
    @commands.has_permissions(mute_members=True)
    async def vunmute(self, ctx, member: discord.Member, *, reason: str = "No reason provided."):
        if not await self.hierarchy_check(ctx, member): return
        
        try:
            await member.edit(mute=False, reason=f"Action by {ctx.author} | {reason}")
            await self.log_mod_action(ctx, "Voice Unmute", member, reason)
            await ctx.send(f"✅ Voice unmuted {member.mention}.")
        except Exception as e:
            await ctx.send(f"❌ **Error:** `{e}`")
        
async def setup(bot):
    await bot.add_cog(Moderation(bot))
