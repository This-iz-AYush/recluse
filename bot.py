import discord
from discord.ext import commands
import os
import datetime
import traceback
import asyncio
from dotenv import load_dotenv

ERROR_LOG_CHANNEL_ID = 1096869180621463662 
CONSOLE_CHANNEL_ID = 1188082818656510032

class Recluse(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(command_prefix=',', intents=intents)
        self.launch_time = datetime.datetime.utcnow()
        self.remove_command("help") 
        self.global_lockdown = False # Wick-style kill switch state

    async def setup_hook(self):
        self.tree.on_error = self.on_app_command_error
        
        # Bind the global security check to all slash commands
        self.tree.interaction_check = self.global_interaction_check
        
        # Bind the global security check to all prefix commands
        self.add_check(self.global_prefix_check)

        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and not filename.startswith('_'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f"Loaded extension: {filename}")
                except Exception as e:
                    print(f"Failed to load extension {filename}: {e}")
                    
        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self):
        print(f'System Online: Authenticated to Gateway as {self.user}')

    # --- WICK-STYLE GLOBAL SECURITY CHECKS ---
    
    async def _core_security_check(self, user_id: int, guild_id: int = None, is_owner: bool = False) -> tuple[bool, str]:
        """The core engine that verifies if a user or guild is allowed to use the bot."""
        if self.global_lockdown and not is_owner:
            return False, "🛑 **Network Lockdown:** Recluse is currently under global maintenance or security lockdown. Commands are temporarily disabled."
            
        if hasattr(self, 'db'):
            # Check User Blacklist
            user_banned = await self.db.global_blacklist.find_one({"target_id": user_id, "type": "user"})
            if user_banned:
                return False, f"⛔ **Network Security:** You have been globally blacklisted from Recluse.\n**Reason:** {user_banned.get('reason', 'TOS Violation')}"
                
            # Check Guild Blacklist
            if guild_id:
                guild_banned = await self.db.global_blacklist.find_one({"target_id": guild_id, "type": "guild"})
                if guild_banned:
                    return False, "⛔ **Network Security:** This server is globally blacklisted. Initiating auto-leave."
                    
        return True, ""

    async def global_interaction_check(self, interaction: discord.Interaction) -> bool:
        """Intercepts all slash commands."""
        is_owner = await self.is_owner(interaction.user)
        guild_id = interaction.guild_id if interaction.guild else None
        
        passed, error_msg = await self._core_security_check(interaction.user.id, guild_id, is_owner)
        
        if not passed:
            if guild_id and "auto-leave" in error_msg:
                try: await interaction.response.send_message(error_msg, ephemeral=True)
                except: pass
                await interaction.guild.leave()
            else:
                await interaction.response.send_message(error_msg, ephemeral=True)
            return False
        return True

    async def global_prefix_check(self, ctx: commands.Context) -> bool:
        """Intercepts all prefix commands."""
        is_owner = await self.is_owner(ctx.author)
        guild_id = ctx.guild.id if ctx.guild else None
        
        passed, error_msg = await self._core_security_check(ctx.author.id, guild_id, is_owner)
        
        if not passed:
            if guild_id and "auto-leave" in error_msg:
                try: await ctx.send(error_msg)
                except: pass
                await ctx.guild.leave()
            else:
                await ctx.send(error_msg)
            return False
        return True

    async def on_guild_join(self, guild: discord.Guild):
        """Wick-style auto-leave if added to a blacklisted server."""
        if hasattr(self, 'db'):
            is_blacklisted = await self.db.global_blacklist.find_one({"target_id": guild.id, "type": "guild"})
            if is_blacklisted:
                try:
                    target_channel = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
                    if target_channel:
                        await target_channel.send(f"⛔ **Network Security:** This server is globally blacklisted from the Recluse network. \n**Reason:** {is_blacklisted.get('reason', 'TOS Violation')}\nLeaving immediately.")
                except discord.Forbidden:
                    pass
                await guild.leave()

    # --- GLOBAL ERROR LOGGING ---
    async def log_system_error(self, ctx_or_msg, error, is_command=True):
        channel = self.get_channel(ERROR_LOG_CHANNEL_ID)
        if not channel: return
        
        embed = discord.Embed(title="⚠️ System Exception Caught", color=discord.Color.red())
        
        if is_command:
            embed.add_field(name="Command Route", value=f"`{ctx_or_msg.command.name if getattr(ctx_or_msg, 'command', None) else 'Unknown'}`", inline=True)
            embed.add_field(name="Invoker", value=f"{ctx_or_msg.author} (`{ctx_or_msg.author.id}`)", inline=True)
        else:
            embed.add_field(name="Event Source", value="`on_message` Automaton", inline=True)
            embed.add_field(name="Trigger User", value=f"{ctx_or_msg.author} (`{ctx_or_msg.author.id}`)", inline=True)
            
        embed.add_field(name="Guild", value=f"{ctx_or_msg.guild.name if ctx_or_msg.guild else 'DMs'}", inline=True)
        
        code_block = "```"
        traceback_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        embed.add_field(name="Traceback Details", value=f"{code_block}py\n{traceback_str[:1000]}\n{code_block}", inline=False)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            print(f"CRITICAL: Failed to log error to channel. \n{traceback_str}")

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.NotOwner):
            return await ctx.send("❌ **Access Denied:** You must be the bot developer to execute this command.", ephemeral=True)
        if isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(f"⏳ **Chill out!** You can use this command again in `{error.retry_after:.1f}s`.", ephemeral=True)
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"❌ **Missing Argument:** You forgot to include `{error.param.name}`. Use `/help` for syntax.", ephemeral=True)
        if isinstance(error, commands.BadArgument):
            return await ctx.send("❌ **Invalid Input:** One or more arguments were invalid. Check your spelling and formatting.", ephemeral=True)
        if isinstance(error, commands.MissingPermissions):
            return await ctx.send("❌ **Access Denied:** You lack the necessary permissions to execute this command.", ephemeral=True)
        if isinstance(error, commands.BotMissingPermissions):
            return await ctx.send("❌ **Execution Blocked:** I don't have the necessary Discord permissions.", ephemeral=True)
        
        if isinstance(error, commands.CommandNotFound):
            return
            
        if isinstance(error, commands.CheckFailure):
            return

        await self.log_system_error(ctx, error)
        try:
            await ctx.send("❌ An unexpected system error occurred. A telemetry report has been dispatched.", ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        if isinstance(error, discord.app_commands.errors.CommandInvokeError):
            error = error.original

        if isinstance(error, discord.app_commands.errors.CommandOnCooldown):
            return await interaction.response.send_message(f"⏳ **Chill out!** You can use this command again in `{error.retry_after:.1f}s`.", ephemeral=True)
        if isinstance(error, discord.app_commands.errors.MissingPermissions):
            return await interaction.response.send_message("❌ **Access Denied:** You lack the necessary permissions.", ephemeral=True)
        if isinstance(error, discord.app_commands.errors.BotMissingPermissions):
             return await interaction.response.send_message("❌ **Execution Blocked:** I don't have the necessary permissions.", ephemeral=True)
             
        if isinstance(error, discord.app_commands.errors.CheckFailure):
            return
        
        class MockCtx:
            def __init__(self, interaction):
                self.command = interaction.command
                self.author = interaction.user
                self.guild = interaction.guild
                
        await self.log_system_error(MockCtx(interaction), error)
        
        msg = "❌ An unexpected routing error occurred. A report has been filed."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

if __name__ == "__main__":
    load_dotenv()
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if TOKEN:
        bot = Recluse()
        bot.run(TOKEN)
    else:
        print("Initialization Failure: Environmental variable DISCORD_BOT_TOKEN is undefined.")
