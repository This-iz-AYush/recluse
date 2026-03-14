import discord
from discord.ext import commands
import traceback
import textwrap
import io
import contextlib
import datetime

class ShutdownConfirm(discord.ui.View):
    def __init__(self, bot, author):
        super().__init__(timeout=60)
        self.bot = bot
        self.author = author

    @discord.ui.button(label="Shut Down", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return await interaction.response.send_message("This isn't your button!", ephemeral=True)
        await interaction.response.edit_message(content="🛑 Initiating graceful shutdown sequence. Goodbye!", view=None)
        await self.bot.close()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return await interaction.response.send_message("This isn't your button!", ephemeral=True)
        await interaction.response.edit_message(content="✅ Shutdown aborted. Keeping systems online.", view=None)

class Owner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cb = "```" 

    # --- WICK-STYLE ENTERPRISE NETWORK MANAGEMENT ---

    @commands.hybrid_group(name="network", description="[Dev] Enterprise global network management and security.")
    @commands.is_owner()
    async def network(self, ctx):
        """Base command for network security."""
        if ctx.invoked_subcommand is None:
            await ctx.send("❌ Use a valid subcommand: `/network blacklist_user`, `/network blacklist_guild`, or `/network lockdown`.", ephemeral=True)

    @network.command(name="blacklist_user", description="[Dev] Globally restrict a user from interacting with the bot.")
    async def blacklist_user(self, ctx, user_id: str, *, reason: str = "TOS Violation / Abuse"):
        await ctx.defer(ephemeral=True)
        try:
            uid = int(user_id)
            if hasattr(self.bot, 'db'):
                await self.bot.db.global_blacklist.update_one(
                    {"target_id": uid, "type": "user"},
                    {"$set": {"reason": reason, "timestamp": datetime.datetime.utcnow().timestamp()}},
                    upsert=True
                )
            await ctx.send(f"⛔ **Network Security:** User `{uid}` has been globally blacklisted.\n**Reason:** {reason}", ephemeral=True)
        except ValueError:
            await ctx.send("❌ Invalid ID format. Must be an integer.", ephemeral=True)

    @network.command(name="unblacklist_user", description="[Dev] Remove a user from the global blacklist.")
    async def unblacklist_user(self, ctx, user_id: str):
        await ctx.defer(ephemeral=True)
        try:
            uid = int(user_id)
            if hasattr(self.bot, 'db'):
                await self.bot.db.global_blacklist.delete_one({"target_id": uid, "type": "user"})
            await ctx.send(f"✅ **Network Security:** User `{uid}` has been removed from the blacklist.", ephemeral=True)
        except ValueError:
            await ctx.send("❌ Invalid ID format.", ephemeral=True)

    @network.command(name="blacklist_guild", description="[Dev] Globally blacklist a server and force the bot to leave it.")
    async def blacklist_guild(self, ctx, guild_id: str, *, reason: str = "TOS Violation / Network Abuse"):
        await ctx.defer(ephemeral=True)
        try:
            gid = int(guild_id)
            if hasattr(self.bot, 'db'):
                await self.bot.db.global_blacklist.update_one(
                    {"target_id": gid, "type": "guild"},
                    {"$set": {"reason": reason, "timestamp": datetime.datetime.utcnow().timestamp()}},
                    upsert=True
                )
                
            # Attempt to instantly force-leave the server
            guild = self.bot.get_guild(gid)
            if guild:
                await guild.leave()
                await ctx.send(f"⛔ **Network Security:** Guild `{guild.name}` ({gid}) has been blacklisted and abandoned.\n**Reason:** {reason}", ephemeral=True)
            else:
                await ctx.send(f"⛔ **Network Security:** Guild `{gid}` has been blacklisted. (Bot is not currently in this server).", ephemeral=True)
        except ValueError:
             await ctx.send("❌ Invalid ID format.", ephemeral=True)

    @network.command(name="lockdown", description="[Dev] Engage global network lockdown (disables all command processing).")
    async def global_lockdown(self, ctx, state: bool):
        """Toggle to True to disable the bot globally in case of an exploit."""
        self.bot.global_lockdown = state
        
        if state:
            embed = discord.Embed(title="🔴 GLOBAL LOCKDOWN ENGAGED", description="All command processing has been suspended across the network.", color=discord.Color.red())
        else:
            embed = discord.Embed(title="🟢 GLOBAL LOCKDOWN LIFTED", description="Normal network operations have resumed.", color=discord.Color.brand_green())
            
        await ctx.send(embed=embed)

    @network.command(name="force_leave", description="[Dev] Force the bot to leave a specific server without blacklisting it.")
    async def force_leave(self, ctx, guild_id: str):
        await ctx.defer(ephemeral=True)
        try:
            gid = int(guild_id)
            guild = self.bot.get_guild(gid)
            if guild:
                await guild.leave()
                await ctx.send(f"👋 Successfully left the server: `{guild.name}` ({gid}).", ephemeral=True)
            else:
                await ctx.send(f"❌ I am not in a server with ID `{gid}`.", ephemeral=True)
        except ValueError:
            await ctx.send("❌ Invalid ID format.", ephemeral=True)


    # --- EXISTING UTILITY COMMANDS ---

    @commands.hybrid_command(name="reload", description="[Dev] Hot-reloads a specific cog/extension.")
    @commands.is_owner()
    async def reload_cog(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            await self.bot.reload_extension(f"cogs.{extension}")
            await ctx.send(f"✅ Successfully reloaded `{extension}`!", ephemeral=True)
        except Exception as e: await ctx.send(f"❌ Failed to reload `{extension}`.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="sync", description="[Dev] Manually synchronizes slash commands to Discord.")
    @commands.is_owner()
    async def sync_tree(self, ctx):
        await ctx.defer(ephemeral=True)
        try:
            synced = await self.bot.tree.sync()
            await ctx.send(f"✅ Synced {len(synced)} command(s).", ephemeral=True)
        except Exception as e: await ctx.send(f"❌ Failed to sync.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="shutdown", description="[Dev] Gracefully disconnects the bot.")
    @commands.is_owner()
    async def shutdown(self, ctx):
        await ctx.send("⚠️ **WARNING:** Are you sure you want to shut down?", view=ShutdownConfirm(self.bot, ctx.author), ephemeral=True)

    @commands.hybrid_command(name="guilds", description="[Dev] Lists all servers the bot is currently in.")
    @commands.is_owner()
    async def list_guilds(self, ctx):
        await ctx.defer(ephemeral=True)
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count, reverse=True)
        embed = discord.Embed(title=f"Connected Servers ({len(guilds)})", color=discord.Color.purple())
        for guild in guilds[:20]: embed.add_field(name=f"{guild.name}", value=f"ID: `{guild.id}`\nMembers: {guild.member_count}", inline=True)
        if len(guilds) > 20: embed.set_footer(text=f"Showing top 20 of {len(guilds)} servers.")
        await ctx.send(embed=embed, ephemeral=True)

    @commands.command(name="eval", hidden=True)
    @commands.is_owner()
    async def eval_code(self, ctx, *, code: str):
        if code.startswith(self.cb) and code.endswith(self.cb): code = '\n'.join(code.split('\n')[1:-1])
        else: code = code.strip('` \n')

        env = {'bot': self.bot, 'ctx': ctx, 'channel': ctx.channel, 'author': ctx.author, 'guild': ctx.guild, 'message': ctx.message}
        env.update(globals())
        exec_wrapper = f"async def func():\n{textwrap.indent(code, '    ')}"

        try:
            exec(exec_wrapper, env)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout): ret = await env['func']()
            output = stdout.getvalue()
            
            res = ""
            if output: res += f"**Output:**\n{self.cb}py\n{output}\n{self.cb}\n"
            if ret is not None: res += f"**Returned:**\n{self.cb}py\n{ret}\n{self.cb}"
            await ctx.send(res if res else "✅ Executed with no output.")
        except Exception as e:
            await ctx.send(f"❌ **Failed:**\n{self.cb}py\n{''.join(traceback.format_exception(type(e), e, e.__traceback__))[:1900]}\n{self.cb}")

    @commands.hybrid_command(name="global_echo", description="[Dev] Broadcasts a message to EVERY server the bot is in.")
    @commands.is_owner()
    async def global_echo(self, ctx, *, message: str):
        await ctx.defer(ephemeral=True)
        success_count = 0
        for guild in self.bot.guilds:
            target = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            if target:
                try: await target.send(message); success_count += 1
                except discord.Forbidden: pass
        await ctx.send(f"📢 Broadcast delivered to `{success_count}/{len(self.bot.guilds)}` servers.", ephemeral=True)
   
    @commands.hybrid_command(name="echo", description="[Dev] Forces the bot to echo a message in the current or specified channel.")
    @commands.is_owner()
    async def echo(self, ctx, channel: discord.TextChannel = None, *, message: str):
        target_channel = channel or ctx.channel       
        try:
            await target_channel.send(message)
            await ctx.send(f"✅ Message silently sent to {target_channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send("❌ I do not have permission to send messages in that channel.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to send message.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="unblacklist", description="Developer Override: Removes an ID from the global blacklist.")
    @commands.is_owner()
    async def unblacklist(self, ctx, target_id: str): 
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ **Database Error:** Disconnected.", ephemeral=True)
            
        # Safely convert the string back into a Python integer
        try:
            target_id_int = int(target_id.strip())
        except ValueError:
            return await ctx.send("❌ **Error:** Please provide a valid numeric ID.", ephemeral=True)
            
        result = await self.bot.db.global_blacklist.delete_one({"target_id": target_id_int})
        if result.deleted_count > 0:
            await ctx.send(f"✅ **Override Successful:** ID `{target_id_int}` has been wiped from the global blacklist.", ephemeral=True)
        else:
            await ctx.send(f"❌ **Not Found:** ID `{target_id_int}` is not currently blacklisted.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Owner(bot))
