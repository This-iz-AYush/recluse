import discord
from discord.ext import commands
from aiohttp import web
import os
import urllib.parse
import aiohttp
import uuid
import datetime

class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        
        # Registering our new dynamic web routes!
        self.app.add_routes([
            web.get('/', self.home),
            web.get('/login', self.login),
            web.get('/callback', self.callback),
            web.get('/logout', self.logout)
        ])
        
        self.runner = None
        self.site = None
        self.bot.loop.create_task(self.start_server())

    async def get_user_session(self, request):
        """Helper function to securely fetch a logged-in user from the database using their cookie."""
        session_id = request.cookies.get("recluse_session")
        if not session_id or not hasattr(self.bot, 'db'):
            return None
            
        session = await self.bot.db.sessions.find_one({"session_id": session_id})
        # Check if session exists and isn't expired (24 hours)
        if session and (datetime.datetime.utcnow().timestamp() - session['created_at'] < 86400):
            return session
        return None

    async def home(self, request):
        user_session = await self.get_user_session(request)
        
        # --- SCENARIO 1: USER IS NOT LOGGED IN (Show Landing Page) ---
        if not user_session:
            landing_html = """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Recluse.OS | Login</title>
                <script src="https://cdn.tailwindcss.com"></script>
                <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
                <style>
                    body { background-color: #09090b; color: white; overflow: hidden; }
                    .glass-panel { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.05); }
                    .gradient-bg { position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(circle at center, rgba(139, 92, 246, 0.15) 0%, transparent 50%); z-index: -1; animation: pulse 10s infinite alternate; }
                </style>
            </head>
            <body class="min-h-screen flex items-center justify-center relative">
                <div class="gradient-bg"></div>
                <div class="glass-panel p-10 rounded-3xl max-w-md w-full text-center shadow-2xl z-10 mx-4">
                    <div class="w-20 h-20 mx-auto rounded-2xl bg-gradient-to-br from-violet-500 to-pink-500 flex items-center justify-center mb-6 shadow-lg shadow-violet-500/30">
                        <i class="fa-solid fa-spider text-white text-4xl"></i>
                    </div>
                    <h1 class="text-3xl font-bold mb-2 tracking-wide">RECLUSE<span class="text-zinc-500 font-light">.OS</span></h1>
                    <p class="text-zinc-400 mb-8">Advanced intelligence & moderation for modern Discord communities.</p>
                    
                    <a href="/login" class="block w-full py-3 px-4 bg-[#5865F2] hover:bg-[#4752C4] transition-colors rounded-xl font-semibold text-white flex items-center justify-center gap-3">
                        <i class="fa-brands fa-discord text-xl"></i> Login with Discord
                    </a>
                </div>
            </body>
            </html>
            """
            return web.Response(text=landing_html, content_type='text/html')

        # --- SCENARIO 2: USER IS LOGGED IN (Show Dashboard) ---
        server_count = len(self.bot.guilds)
        bot_name = self.bot.user.name if self.bot.user else "Loading..."
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"

        dashboard_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__BOT_NAME__ | Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                .glass-panel { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.05); }
                .gradient-text { background: linear-gradient(to right, #8b5cf6, #ec4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
                .toggle-checkbox:checked { right: 0; border-color: #8b5cf6; }
                .toggle-checkbox:checked + .toggle-label { background-color: #8b5cf6; box-shadow: 0 0 10px rgba(139, 92, 246, 0.5); }
            </style>
        </head>
        <body class="bg-[#09090b] text-zinc-300 font-sans min-h-screen flex flex-col selection:bg-violet-500 selection:text-white">

            <!-- Top Navigation -->
            <nav class="glass-panel sticky top-0 z-50 px-6 py-4 flex justify-between items-center border-b border-white/5">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500 to-pink-500 flex items-center justify-center shadow-lg shadow-violet-500/20">
                        <i class="fa-solid fa-spider text-white text-lg"></i>
                    </div>
                    <span class="text-xl font-bold text-white tracking-wide">__BOT_NAME__<span class="font-light text-zinc-500">.OS</span></span>
                </div>
                
                <div class="flex items-center gap-4">
                    <div class="flex items-center gap-3 bg-white/5 py-1.5 px-3 rounded-full border border-white/5">
                        <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        <span class="text-sm font-medium text-white">__USER_NAME__</span>
                    </div>
                    <a href="/logout" class="text-xs text-red-400 hover:text-red-300 font-medium px-3 py-2 rounded-lg hover:bg-red-400/10 transition">Logout</a>
                </div>
            </nav>

            <main class="flex-1 max-w-7xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8">
                
                <div class="flex flex-col md:flex-row justify-between items-start md:items-end gap-4">
                    <div>
                        <h1 class="text-3xl md:text-4xl font-extrabold text-white mb-2">Welcome back, <span class="gradient-text">__USER_NAME__</span></h1>
                        <p class="text-zinc-400">Manage your __BOT_NAME__ instance and monitor active modules.</p>
                    </div>
                    <div class="flex gap-3">
                        <button class="px-4 py-2 rounded-lg bg-surface border border-white/10 hover:border-white/20 transition text-sm font-medium text-white flex items-center gap-2">
                            <i class="fa-solid fa-rotate"></i> Sync Data
                        </button>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="text-zinc-400 text-sm font-medium mb-4">Connected Servers</div>
                        <div class="text-3xl font-bold text-white mb-1">__SERVER_COUNT__</div>
                    </div>
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="text-zinc-400 text-sm font-medium mb-4">Dashboard Status</div>
                        <div class="text-3xl font-bold text-emerald-400 mb-1">Online</div>
                    </div>
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="text-zinc-400 text-sm font-medium mb-4">Active Database</div>
                        <div class="text-3xl font-bold text-white mb-1">MongoDB</div>
                    </div>
                </div>
                
                <div class="mt-4 p-6 glass-panel rounded-2xl border border-violet-500/20 text-center">
                    <h3 class="text-xl font-bold text-white mb-2">🚧 Interactive Modules Coming Soon!</h3>
                    <p class="text-zinc-400">You have successfully authenticated via Discord OAuth2. The ability to toggle specific features and edit server configs from this panel will be unlocked in Phase 3.</p>
                </div>

            </main>
        </body>
        </html>
        """
        
        dashboard_html = dashboard_html.replace("__BOT_NAME__", str(bot_name))
        dashboard_html = dashboard_html.replace("__SERVER_COUNT__", str(server_count))
        dashboard_html = dashboard_html.replace("__USER_NAME__", str(user_name))
        dashboard_html = dashboard_html.replace("__USER_AVATAR__", str(user_avatar))
        
        return web.Response(text=dashboard_html, content_type='text/html')

    async def login(self, request):
        """Redirects the user to the official Discord authorization page."""
        client_id = os.getenv("DISCORD_CLIENT_ID")
        redirect_uri = os.getenv("REDIRECT_URI")
        
        if not client_id or not redirect_uri:
            return web.Response(text="Configuration Error: DISCORD_CLIENT_ID or REDIRECT_URI is missing in Render.", status=500)

        # Discord OAuth2 URL
        oauth_url = (
            f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
            f"&response_type=code&scope=identify%20guilds"
        )
        raise web.HTTPFound(oauth_url)

    async def callback(self, request):
        """Discord sends the user back here with a secret 'code'. We exchange it for their info."""
        code = request.query.get("code")
        if not code:
            return web.Response(text="Login failed. No code provided by Discord.", status=400)

        client_id = os.getenv("DISCORD_CLIENT_ID")
        client_secret = os.getenv("DISCORD_CLIENT_SECRET")
        redirect_uri = os.getenv("REDIRECT_URI")

        # 1. Exchange the code for an Access Token
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        async with aiohttp.ClientSession() as session:
            async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
                if resp.status != 200:
                    return web.Response(text=f"Failed to authenticate with Discord: {await resp.text()}", status=500)
                
                token_data = await resp.json()
                access_token = token_data.get("access_token")

            # 2. Use the Access Token to get the user's Discord profile
            user_headers = {"Authorization": f"Bearer {access_token}"}
            async with session.get("https://discord.com/api/users/@me", headers=user_headers) as resp:
                user_data = await resp.json()

        # 3. Create a secure session in MongoDB
        session_id = str(uuid.uuid4())
        
        if hasattr(self.bot, 'db'):
            await self.bot.db.sessions.update_one(
                {"discord_id": user_data["id"]},
                {
                    "$set": {
                        "session_id": session_id,
                        "username": user_data.get("username", "Unknown"),
                        "avatar": user_data.get("avatar", ""),
                        "created_at": datetime.datetime.utcnow().timestamp()
                    }
                },
                upsert=True
            )

        # 4. Give the user a cookie and redirect them to the home page!
        response = web.HTTPFound('/')
        response.set_cookie('recluse_session', session_id, max_age=86400, httponly=True)
        return response

    async def logout(self, request):
        """Logs the user out by destroying their session cookie."""
        session_id = request.cookies.get("recluse_session")
        if session_id and hasattr(self.bot, 'db'):
            await self.bot.db.sessions.delete_one({"session_id": session_id})
            
        response = web.HTTPFound('/')
        response.del_cookie('recluse_session')
        return response

    async def start_server(self):
        await self.bot.wait_until_ready()
        port = int(os.getenv("PORT", 8080))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        self.site = web.TCPSite(self.runner, '0.0.0.0', port)
        try:
            await self.site.start()
            print(f"🌐 Web dashboard successfully started on port {port}!")
        except Exception as e:
            print(f"❌ Failed to start web server: {e}")

    async def cog_unload(self):
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())

async def setup(bot):
    await bot.add_cog(Dashboard(bot))
