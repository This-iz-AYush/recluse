import discord
from discord.ext import commands
import traceback
import textwrap
import io
import contextlib
import aiohttp

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
        """
        Usage: 
        /echo <message> (Sends in current channel)
        /echo <#channel> <message> (Sends in specific channel)
        """
        target_channel = channel or ctx.channel       
        try:
            await target_channel.send(message)
            await ctx.send(f"✅ Message silently sent to {target_channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send("❌ I do not have permission to send messages in that channel.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to send message.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Owner(bot))
