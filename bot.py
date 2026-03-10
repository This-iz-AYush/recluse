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
        self.remove_command("help") # Removing default help so our UI cog can take over

    async def setup_hook(self):
        # Bind the global slash command error handler
        self.tree.on_error = self.on_app_command_error
        
        # Start the console reader task
        #self.loop.create_task(self.console_reader())

        # Dynamically load all cogs
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
        # 1. Handle explicit permission and state errors first
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
        
        # 2. Quietly ignore commands that don't exist
        if isinstance(error, commands.CommandNotFound):
            return
            
        # 3. Quietly ignore generic CheckFailures. 
        # (Our dashboard cog_checks already send the user a warning message, so we just stop execution here without logging an error)
        if isinstance(error, commands.CheckFailure):
            return

        # 4. If it's none of the above, it's a real bug. Log it and notify the user.
        await self.log_system_error(ctx, error)
        try:
            await ctx.send("❌ An unexpected system error occurred. A telemetry report has been dispatched.", ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        # Extract the original error if it's wrapped
        if isinstance(error, discord.app_commands.errors.CommandInvokeError):
            error = error.original

        # 1. Handle Slash Command specific errors
        if isinstance(error, discord.app_commands.errors.CommandOnCooldown):
            return await interaction.response.send_message(f"⏳ **Chill out!** You can use this command again in `{error.retry_after:.1f}s`.", ephemeral=True)
        if isinstance(error, discord.app_commands.errors.MissingPermissions):
            return await interaction.response.send_message("❌ **Access Denied:** You lack the necessary permissions.", ephemeral=True)
        if isinstance(error, discord.app_commands.errors.BotMissingPermissions):
             return await interaction.response.send_message("❌ **Execution Blocked:** I don't have the necessary permissions.", ephemeral=True)
             
        # 2. Catch the dashboard toggle CheckFailures 
        if isinstance(error, discord.app_commands.errors.CheckFailure):
            return
        
        # 3. Log actual application bugs
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
