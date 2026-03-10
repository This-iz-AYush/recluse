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
        # Security check just in case someone else somehow clicks the button
        if interaction.user != self.author:
            return await interaction.response.send_message("This isn't your button!", ephemeral=True)
            
        await interaction.response.edit_message(content="🛑 Initiating graceful shutdown sequence. Goodbye!", view=None)
        print(f"Shutdown confirmed and initiated by owner ({interaction.user}).")
        await self.bot.close()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author:
            return await interaction.response.send_message("This isn't your button!", ephemeral=True)
            
        await interaction.response.edit_message(content="✅ Shutdown aborted. Keeping systems online.", view=None)

class Owner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Using a variable for discord code blocks so the UI parser doesn't break!
        self.cb = "```" 

    # --- 1. EXTENSION MANAGEMENT ---
    @commands.hybrid_command(name="reload", description="[Dev] Hot-reloads a specific cog/extension.")
    @commands.is_owner()
    async def reload_cog(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            await self.bot.reload_extension(f"cogs.{extension}")
            await ctx.send(f"✅ Successfully reloaded `cogs/{extension}.py`!", ephemeral=True)
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(f"cogs.{extension}")
                await ctx.send(f"✅ Successfully loaded `cogs/{extension}.py` for the first time!", ephemeral=True)
            except Exception as e:
                await ctx.send(f"❌ Failed to load `{extension}`.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{extension}`.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="load", description="[Dev] Loads a brand new cog/extension.")
    @commands.is_owner()
    async def load_cog(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            await self.bot.load_extension(f"cogs.{extension}")
            await ctx.send(f"✅ Successfully loaded `cogs/{extension}.py`!", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to load `{extension}`.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="unload", description="[Dev] Unloads an active cog/extension.")
    @commands.is_owner()
    async def unload_cog(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            await self.bot.unload_extension(f"cogs.{extension}")
            await ctx.send(f"✅ Successfully unloaded `cogs/{extension}.py`! The commands are now offline.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to unload `{extension}`.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    # --- 2. BOT LIFECYCLE ---
    @commands.hybrid_command(name="sync", description="[Dev] Manually synchronizes slash commands to Discord.")
    @commands.is_owner()
    async def sync_tree(self, ctx):
        await ctx.defer(ephemeral=True)
        try:
            synced = await self.bot.tree.sync()
            await ctx.send(f"✅ Successfully synced {len(synced)} command(s) globally.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to sync commands.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="shutdown", description="[Dev] Gracefully disconnects the bot and terminates the process.")
    @commands.is_owner()
    async def shutdown(self, ctx):
        view = ShutdownConfirm(self.bot, ctx.author)
        await ctx.send("⚠️ **WARNING:** Are you sure you want to completely shut down the bot?", view=view, ephemeral=True)

    # --- 3. SERVER MANAGEMENT ---
    @commands.hybrid_command(name="guilds", description="[Dev] Lists all servers the bot is currently in.")
    @commands.is_owner()
    async def list_guilds(self, ctx):
        await ctx.defer(ephemeral=True)
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count, reverse=True)
        
        embed = discord.Embed(title=f"Connected Servers ({len(guilds)})", color=discord.Color.purple())
        
        # Display up to 20 guilds to avoid hitting embed limits
        for guild in guilds[:20]:
            embed.add_field(
                name=f"{guild.name}", 
                value=f"ID: `{guild.id}`\nMembers: {guild.member_count}", 
                inline=True
            )
            
        if len(guilds) > 20:
            embed.set_footer(text=f"And {len(guilds) - 20} more servers...")
            
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="leave_server", description="[Dev] Forces the bot to leave a specific server.")
    @commands.is_owner()
    async def leave_server(self, ctx, guild_id: str):
        await ctx.defer(ephemeral=True)
        try:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                return await ctx.send(f"❌ I am not currently in a server with the ID `{guild_id}`.", ephemeral=True)
                
            await guild.leave()
            await ctx.send(f"✅ Successfully left the server **{guild.name}**.", ephemeral=True)
        except ValueError:
            await ctx.send("❌ Please provide a valid numeric Server ID.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to leave the server.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    # --- 4. COMMUNICATION ---
    @commands.hybrid_command(name="echo", description="[Dev] Forces the bot to send a message in a specific channel.")
    @commands.is_owner()
    async def echo_message(self, ctx, channel: discord.TextChannel, *, message: str):
        try:
            await channel.send(message)
            await ctx.send(f"✅ Message sent to {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send("❌ I do not have permission to send messages in that channel.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to send message.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    # --- 5. EVALUATION ---
    @commands.command(name="eval", hidden=True)
    @commands.is_owner()
    async def eval_code(self, ctx, *, code: str):
        """[Dev] Evaluates raw python code. (Prefix only for formatting ease)"""
        if code.startswith(self.cb) and code.endswith(self.cb):
            code = '\n'.join(code.split('\n')[1:-1])
        else:
            code = code.strip('` \n')

        env = {
            'bot': self.bot,
            'ctx': ctx,
            'channel': ctx.channel,
            'author': ctx.author,
            'guild': ctx.guild,
            'message': ctx.message
        }
        env.update(globals())

        indented_code = textwrap.indent(code, '    ')
        exec_wrapper = f"async def func():\n{indented_code}"

        try:
            exec(exec_wrapper, env)
            func = env['func']
            
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                ret = await func()

            output = stdout.getvalue()
            
            response = ""
            if output:
                response += f"**Output:**\n{self.cb}py\n{output}\n{self.cb}\n"
            if ret is not None:
                response += f"**Returned:**\n{self.cb}py\n{ret}\n{self.cb}"
            if not response:
                response = "✅ Execution completed with no output."

            if len(response) > 2000:
                await ctx.send("⚠️ Output is too long to send in Discord. Check your console.")
                print(f"Eval Output:\n{output}\nReturned: {ret}")
            else:
                await ctx.send(response)

        except Exception as e:
            traceback_string = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            await ctx.send(f"❌ **Evaluation Failed:**\n{self.cb}py\n{traceback_string[:1900]}\n{self.cb}")

    # --- 6. OWNER FLEX COMMANDS ---
    @commands.hybrid_command(name="dm_user", description="[Dev] Send a direct message to a specific user as the bot.")
    @commands.is_owner()
    async def dm_user(self, ctx, user: discord.User, *, message: str):
        await ctx.defer(ephemeral=True)
        try:
            await user.send(message)
            await ctx.send(f"✅ Successfully slipped into {user.mention}'s DMs.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send(f"❌ Failed. {user.mention} has DMs disabled or has blocked me.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ An error occurred.\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)

    @commands.hybrid_command(name="global_echo", description="[Dev] Broadcasts a message to EVERY server the bot is in.")
    @commands.is_owner()
    async def global_echo(self, ctx, *, message: str):
        await ctx.defer(ephemeral=True)
        success_count = 0
        
        for guild in self.bot.guilds:
            # Try to find the default system channel first
            target_channel = guild.system_channel
            
            # If no system channel, find the first channel we have permission to type in
            if not target_channel:
                for channel in guild.text_channels:
                    if channel.permissions_for(guild.me).send_messages:
                        target_channel = channel
                        break
                        
            if target_channel:
                try:
                    await target_channel.send(message)
                    success_count += 1
                except discord.Forbidden:
                    pass # Silently skip if we get a weird permission block
                    
        await ctx.send(f"📢 **Global Broadcast Complete!**\nMessage delivered to `{success_count}/{len(self.bot.guilds)}` servers.", ephemeral=True)

    @commands.hybrid_command(name="rename", description="[Dev] Change the bot's nickname in the current server.")
    @commands.is_owner()
    async def rename(self, ctx, *, nickname: str = None):
        if not ctx.guild:
            return await ctx.send("❌ This command can only be used inside a server, not in DMs.", ephemeral=True)
            
        try:
            await ctx.guild.me.edit(nick=nickname)
            if nickname:
                await ctx.send(f"✅ Nickname updated to **{nickname}**.", ephemeral=True)
            else:
                await ctx.send("✅ Nickname reset to default.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send("❌ I do not have the 'Change Nickname' permission in this server.", ephemeral=True)

    @commands.command(name="set_avatar", hidden=True)
    @commands.is_owner()
    async def set_avatar(self, ctx, url: str):
        """[Dev] Change the bot's profile picture using an image URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        await self.bot.user.edit(avatar=image_data)
                        await ctx.send("✅ Profile picture updated successfully!", ephemeral=True)
                    else:
                        await ctx.send(f"❌ Failed to download image. Status Code: `{response.status}`", ephemeral=True)
        except discord.HTTPException as e:
            await ctx.send(f"❌ Discord API rejected the image (might be too large or hitting a rate limit).\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Error setting avatar:\n{self.cb}py\n{e}\n{self.cb}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Owner(bot))