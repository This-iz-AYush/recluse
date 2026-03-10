import discord
from discord.ext import commands
from aiohttp import web
import os

class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.app.add_routes([web.get('/', self.home)])
        self.runner = None
        self.site = None
        
        # Start the web server safely in the background
        self.bot.loop.create_task(self.start_server())

    async def home(self, request):
        # A simple webpage that reads live data directly from your Discord Bot!
        server_count = len(self.bot.guilds)
        bot_name = self.bot.user.name if self.bot.user else "Loading..."
        
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <title>{bot_name} Dashboard</title>
            <style>
                body {{ font-family: Arial, sans-serif; background-color: #1a1a1a; color: white; text-align: center; padding-top: 100px; }}
                .card {{ background-color: #2d2d2d; padding: 40px; border-radius: 10px; display: inline-block; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }}
                h1 {{ color: #f43f5e; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>✅ The Web Server is Online!</h1>
                <p><strong>Bot Name:</strong> {bot_name}</p>
                <p><strong>Connected Servers:</strong> {server_count}</p>
                <p>This is the beginning of your Dyno-style dashboard.</p>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type='text/html')

    async def start_server(self):
        # Wait until the bot is fully ready before starting the web server
        await self.bot.wait_until_ready()
        
        # Back on Daki, we MUST use the specific port they assigned us!
        port = 4145
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        # Bind to 0.0.0.0 so the outside world can connect
        self.site = web.TCPSite(self.runner, '0.0.0.0', port)
        
        try:
            await self.site.start()
            print(f"🌐 Web dashboard successfully started on port {port}!")
        except Exception as e:
            print(f"❌ Failed to start web server: {e}")

    async def cog_unload(self):
        # Clean up the web server when the cog is reloaded/unloaded
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())

async def setup(bot):
    await bot.add_cog(Dashboard(bot))