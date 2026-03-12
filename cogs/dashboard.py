import discord
from discord.ext import commands
from aiohttp import web
import os
import urllib.parse
import aiohttp
import uuid
import datetime
import random
import string

class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.dashboard_maintenance = False # Independent state for the website
        
        # Initialize aiohttp Application with our custom IP Blocker & Verification Middleware
        self.app = web.Application(middlewares=[self.security_middleware])
        
        # Registering all dynamic web routes
        self.app.add_routes([
            web.get('/', self.home),
            web.get('/login', self.login),
            web.get('/callback', self.callback),
            web.get('/logout', self.logout),
            web.get('/manage/{guild_id}', self.manage_server),
            web.get('/logs/{guild_id}', self.server_logs),
            web.get('/automod/{guild_id}', self.auto_mod),
            web.get('/wizard/{guild_id}', self.wizard_setup),
            web.get('/misc/{guild_id}', self.misc_settings),
            web.get('/lockdown/{guild_id}', self.server_lockdown),
            web.get('/owner_panel', self.owner_panel),
            web.post('/api/settings/{guild_id}', self.update_settings),
            web.post('/api/ip_action', self.handle_ip_action),
            web.post('/api/verify_visitor', self.verify_visitor),
            web.post('/api/owner_action', self.handle_owner_action)
        ])
        
        self.runner = None
        self.site = None
        self.bot.loop.create_task(self.start_server())

    def get_bot_avatar(self):
        """Forces Discord to return a PNG/GIF instead of WebP, ensuring the browser can render the Favicon."""
        if self.bot and self.bot.user:
            return str(self.bot.user.display_avatar.url).replace(".webp", ".png")
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    @web.middleware
    async def security_middleware(self, request, handler):
        """Intercepts traffic for IP logging, ban enforcement, Maintenance, and Anti-Bot Verification."""
        raw_ip = request.headers.get('X-Forwarded-For', request.remote)
        ip = raw_ip.split(',')[0].strip() if raw_ip else 'Unknown'
        request['visitor_ip'] = ip

        # 1. IP Ban Check
        if hasattr(self.bot, 'db') and ip != 'Unknown':
            is_banned = await self.bot.db.ip_bans.find_one({"ip": ip})
            if is_banned:
                return web.Response(text="403 Forbidden: Your IP address has been permanently restricted from accessing this network.", status=403)

        # 2. INDEPENDENT DASHBOARD MAINTENANCE CHECK
        if getattr(self.bot, 'dashboard_maintenance', False):
            # Paths the owner must be able to hit to login and lift the maintenance
            allowed_paths = ['/login', '/callback', '/logout', '/owner_panel', '/api/owner_action']
            if request.path not in allowed_paths:
                is_owner = False
                user_session = await self.get_user_session(request)
                
                # Check if the user trying to visit is the Bot Developer
                if user_session:
                    owner_id = getattr(self.bot, 'owner_id', None)
                    if not owner_id:
                        app_info = await self.bot.application_info()
                        self.bot.owner_id = app_info.owner.id
                        owner_id = self.bot.owner_id
                        
                    if int(user_session['discord_id']) == owner_id:
                        is_owner = True
                
                # If they aren't the developer, trap them in the Maintenance Window
                if not is_owner:
                    bot_avatar = self.get_bot_avatar()
                    maintenance_html = f"""
                    <!DOCTYPE html>
                    <html lang="en">
                    <head>
                        <meta charset="UTF-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <title>Recluse.OS | Maintenance</title>
                        <link rel="icon" type="image/png" href="{bot_avatar}">
                        <link rel="shortcut icon" href="{bot_avatar}">
                        <script src="https://cdn.tailwindcss.com"></script>
                        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
                        <style>
                            body {{ background-color: #09090b; color: white; overflow: hidden; }}
                            .glass-panel {{ background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.05); }}
                            .gradient-bg {{ position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(circle at center, rgba(234, 179, 8, 0.15) 0%, transparent 50%); z-index: -1; animation: pulse 10s infinite alternate; }}
                        </style>
                    </head>
                    <body class="min-h-screen flex items-center justify-center relative">
                        <div class="gradient-bg"></div>
                        <div class="glass-panel p-10 rounded-3xl max-w-lg w-full text-center shadow-2xl z-10 mx-4 border border-yellow-500/20">
                            <div class="w-20 h-20 mx-auto rounded-2xl bg-yellow-500/10 flex items-center justify-center mb-6 border border-yellow-500/30 shadow-[0_0_30px_rgba(234,179,8,0.3)]">
                                <i class="fa-solid fa-gear fa-spin text-yellow-500 text-4xl"></i>
                            </div>
                            <h1 class="text-3xl font-bold mb-2 tracking-wide text-white">SYSTEM <span class="text-yellow-500">MAINTENANCE</span></h1>
                            <p class="text-zinc-400 mb-8 leading-relaxed">The Recluse dashboard is currently undergoing scheduled upgrades or maintenance. All bot functions in Discord are still 100% operational.</p>
                            
                            <div class="bg-[#000] rounded-xl p-4 border border-white/5 flex items-center justify-between">
                                <span class="text-sm font-medium text-zinc-300">Web Interface</span>
                                <span class="text-xs font-bold text-yellow-500 bg-yellow-500/10 px-3 py-1 rounded border border-yellow-500/20 flex items-center gap-2">
                                    <i class="fa-solid fa-wrench"></i> UPGRADING
                                </span>
                            </div>
                            
                            <p class="text-[10px] text-zinc-600 mt-8 font-mono uppercase tracking-widest">Recluse.OS Web Infrastructure</p>
                        </div>
                    </body>
                    </html>
                    """
                    return web.Response(text=maintenance_html, content_type='text/html', status=503)

        # 3. Cloudflare-style Anti-Bot Verification Check
        if request.path == '/api/verify_visitor':
            return await handler(request)

        is_verified = request.cookies.get("recluse_verified")
        if not is_verified:
            ray_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
            bot_avatar = self.get_bot_avatar()
            
            verify_html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Recluse.OS | Security Verification</title>
                <link rel="icon" type="image/png" href="{bot_avatar}">
                <link rel="shortcut icon" href="{bot_avatar}">
                <script src="https://cdn.tailwindcss.com"></script>
                <style>
                    body {{ background-color: #000; color: #fff; font-family: system-ui, -apple-system, sans-serif; display: flex; flex-direction: column; min-height: 100vh; }}
                    .spinner {{ border: 3px solid rgba(255,255,255,0.1); width: 24px; height: 24px; border-radius: 50%; border-left-color: #fff; animation: spin 1s linear infinite; }}
                    @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
                </style>
            </head>
            <body class="items-center justify-center p-6 sm:p-12">
                <div class="max-w-4xl w-full mx-auto flex flex-col items-start gap-4 mt-20">
                    <h1 class="text-3xl font-medium tracking-wide mb-1">Recluse.OS</h1>
                    <h2 class="text-xl text-zinc-200">Performing security verification</h2>
                    <p class="text-zinc-400 text-sm mb-6 max-w-2xl">This website uses a security service to protect against malicious bots. This page is displayed while the website verifies you are not a bot.</p>
                    
                    <div class="border border-zinc-800 rounded flex items-center justify-between p-4 w-72 bg-[#0a0a0a]">
                        <div class="flex items-center gap-3">
                            <div class="spinner"></div>
                            <span class="text-sm font-medium">Verifying...</span>
                        </div>
                        <div class="text-[10px] text-zinc-500 text-right leading-tight">
                            <span class="font-bold text-violet-500 text-xs">RECLUSE</span><br>
                            Security
                        </div>
                    </div>
                </div>
                
                <div class="mt-auto pt-8 max-w-4xl w-full mx-auto border-t border-zinc-800 text-center text-xs text-zinc-500">
                    Ray ID: <span class="font-mono">{ray_id}</span><br>
                    Performance and Security by Recluse.OS
                </div>

                <script>
                    setTimeout(async () => {{
                        try {{
                            await fetch('/api/verify_visitor', {{ method: 'POST' }});
                            window.location.reload();
                        }} catch (e) {{
                            console.error("Verification failed", e);
                        }}
                    }}, 3500);
                </script>
            </body>
            </html>
            """
            return web.Response(text=verify_html, content_type='text/html')

        # 4. Visit Telemetry Logging
        if hasattr(self.bot, 'db') and ip != 'Unknown':
            session_id = request.cookies.get("recluse_session")
            discord_username = None
            
            if session_id:
                session = await self.bot.db.sessions.find_one({"session_id": session_id})
                if session and (datetime.datetime.utcnow().timestamp() - session.get('created_at', 0) < 86400):
                    discord_username = session.get("username")

            update_data = {"last_visit": datetime.datetime.utcnow().timestamp()}
            if discord_username:
                update_data["last_user"] = discord_username

            await self.bot.db.visit_logs.update_one(
                {"ip": ip},
                {"$set": update_data, "$inc": {"hits": 1}},
                upsert=True
            )

        return await handler(request)

    async def verify_visitor(self, request):
        response = web.json_response({"success": True})
        response.set_cookie('recluse_verified', 'true', max_age=86400*7, httponly=True)
        return response

    async def get_user_session(self, request):
        session_id = request.cookies.get("recluse_session")
        if not session_id or not hasattr(self.bot, 'db'):
            return None
            
        session = await self.bot.db.sessions.find_one({"session_id": session_id})
        if session and (datetime.datetime.utcnow().timestamp() - session['created_at'] < 86400):
            return session
        return None

    # -------------------------------------------------------------------------------------------------
    # AUTHENTICATION ROUTES
    # -------------------------------------------------------------------------------------------------

    async def login(self, request):
        client_id = os.getenv("DISCORD_CLIENT_ID")
        redirect_uri = os.getenv("REDIRECT_URI")
        if not client_id or not redirect_uri:
            return web.Response(text="Configuration Error: DISCORD_CLIENT_ID or REDIRECT_URI is missing.", status=500)
        oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&response_type=code&scope=identify%20guilds"
        raise web.HTTPFound(oauth_url)

    async def callback(self, request):
        code = request.query.get("code")
        if not code: return web.Response(text="Login failed. No code provided by Discord.", status=400)

        data = {
            "client_id": os.getenv("DISCORD_CLIENT_ID"),
            "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": os.getenv("REDIRECT_URI")
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        async with aiohttp.ClientSession() as session:
            async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
                if resp.status != 200: return web.Response(text=f"Failed to authenticate with Discord.", status=500)
                access_token = (await resp.json()).get("access_token")

            async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                user_data = await resp.json()

        session_id = str(uuid.uuid4())
        
        if hasattr(self.bot, 'db'):
            await self.bot.db.sessions.update_one(
                {"discord_id": user_data["id"]},
                {"$set": {"session_id": session_id, "username": user_data.get("username", "Unknown"), "avatar": user_data.get("avatar", ""), "access_token": access_token, "created_at": datetime.datetime.utcnow().timestamp()}},
                upsert=True
            )

        response = web.HTTPFound('/')
        response.set_cookie('recluse_session', session_id, max_age=86400, httponly=True)
        return response

    async def logout(self, request):
        session_id = request.cookies.get("recluse_session")
        if session_id and hasattr(self.bot, 'db'):
            await self.bot.db.sessions.delete_one({"session_id": session_id})
        response = web.HTTPFound('/')
        response.del_cookie('recluse_session')
        return response

    # -------------------------------------------------------------------------------------------------
    # DASHBOARD UI PAGES
    # -------------------------------------------------------------------------------------------------

    async def home(self, request):
        user_session = await self.get_user_session(request)
        bot_name = self.bot.user.name if self.bot and self.bot.user else "Recluse"
        bot_avatar = self.get_bot_avatar()
        client_id = os.getenv("DISCORD_CLIENT_ID", "")
        invite_link = f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot"

        # --- UNAUTHENTICATED LANDING PAGE (WICK REPLICA) ---
        if not user_session:
            landing_html = """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>__BOT_NAME__ | Next-Gen Security</title>
                <link rel="icon" type="image/png" href="__BOT_AVATAR__">
                <link rel="shortcut icon" href="__BOT_AVATAR__">
                <script src="https://cdn.tailwindcss.com"></script>
                <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
                <style>
                    body { background-color: #0b1121; color: white; overflow-x: hidden; font-family: system-ui, -apple-system, sans-serif; }
                    
                    /* Vibrant Wick-Style Background Gradients */
                    .wick-bg {
                        position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: -2;
                        background: radial-gradient(120% 100% at 80% -10%, #d83b9e 0%, transparent 45%),
                                    radial-gradient(120% 100% at 0% 0%, #0ea5e9 0%, transparent 50%),
                                    radial-gradient(100% 100% at 100% 100%, #1e1b4b 0%, transparent 50%);
                        opacity: 0.6;
                        pointer-events: none;
                    }
                    .shape {
                        position: absolute; width: 150vw; height: 100vh; z-index: -1; transform-origin: top left;
                        background: linear-gradient(135deg, rgba(14, 165, 233, 0.1) 0%, rgba(216, 59, 158, 0.1) 100%);
                        clip-path: polygon(0 0, 100% 0, 100% 40%, 0 80%);
                        pointer-events: none;
                    }

                    /* Mockup CSS for visual flair */
                    .dashboard-mockup { background: #161b22; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7); transform: perspective(1000px) rotateY(-15deg) rotateX(5deg); transition: transform 0.5s ease; }
                    .dashboard-mockup:hover { transform: perspective(1000px) rotateY(-5deg) rotateX(2deg); }
                    .discord-msg-mockup { background: #313338; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); }
                    .pill { background: rgba(14, 165, 233, 0.2); color: #38bdf8; border: 1px solid rgba(14, 165, 233, 0.3); }
                    
                    /* Confetti Simulation */
                    .confetti { position: absolute; width: 8px; height: 8px; background-color: #fce7f3; opacity: 0; animation: fall linear infinite; z-index: 0;}
                    @keyframes fall { 0% { transform: translateY(-100vh) rotate(0deg); opacity: 1; } 100% { transform: translateY(100vh) rotate(720deg); opacity: 0; } }
                </style>
            </head>
            <body class="relative min-h-screen flex flex-col selection:bg-pink-500 selection:text-white">
                <div class="wick-bg"></div>
                <div class="shape"></div>
                
                <div id="confetti-container" class="absolute inset-0 overflow-hidden pointer-events-none opacity-40"></div>

                <nav class="flex items-center justify-between px-8 py-5 relative z-10 max-w-7xl mx-auto w-full">
                    <div class="flex items-center gap-6">
                        <div class="flex items-center gap-3 text-white font-bold text-xl tracking-tight">
                            <img src="__BOT_AVATAR__" class="w-8 h-8 rounded-full border border-white/10" alt="Icon">
                            __BOT_NAME__
                        </div>
                        <div class="hidden md:flex gap-6 text-sm font-semibold text-zinc-300">
                            <a href="/login" class="hover:text-white transition">Dashboard</a>
                            <a href="#" class="hover:text-white transition">Status</a>
                            <a href="#" class="hover:text-white transition">Premium</a>
                        </div>
                    </div>
                    <div class="flex items-center gap-6 text-zinc-400">
                        <a href="#" class="hover:text-white transition"><i class="fa-solid fa-gem"></i></a>
                        <a href="#" class="hover:text-white transition"><i class="fa-solid fa-globe"></i></a>
                        <a href="#" class="hover:text-white transition"><i class="fa-brands fa-discord text-xl"></i></a>
                    </div>
                </nav>

                <main class="flex-1 w-full max-w-7xl mx-auto px-8 flex flex-col lg:flex-row items-center justify-between relative z-10 pb-20 mt-10 lg:mt-20">
                    <div class="w-full lg:w-1/2 mb-16 lg:mb-0">
                        <h1 class="text-5xl lg:text-7xl font-black text-white leading-[1.1] mb-6 tracking-tight">
                            Discord moderation and security that is always one step ahead
                        </h1>
                        <div class="flex flex-col sm:flex-row gap-4 mt-10">
                            <a href="__INVITE_LINK__" class="px-8 py-4 bg-white text-black font-bold rounded-lg text-center hover:bg-zinc-200 transition shadow-[0_0_20px_rgba(255,255,255,0.3)]">
                                + INVITE
                            </a>
                            <a href="/login" class="px-8 py-4 bg-white text-black font-bold rounded-lg text-center hover:bg-zinc-200 transition shadow-[0_0_20px_rgba(255,255,255,0.3)] flex items-center justify-center gap-2">
                                > DASHBOARD
                            </a>
                        </div>
                    </div>
                    
                    <div class="w-full lg:w-[45%] hidden md:block relative">
                        <div class="dashboard-mockup w-full h-[350px] rounded-xl overflow-hidden flex">
                            <div class="w-16 bg-[#0d1117] border-r border-white/5 flex flex-col items-center py-4 gap-4">
                                <div class="w-8 h-8 rounded-full bg-pink-500/20 border border-pink-500/50"></div>
                                <div class="w-8 h-8 rounded bg-blue-500 text-white flex items-center justify-center text-xs shadow-[0_0_10px_rgba(59,130,246,0.5)]"><i class="fa-solid fa-chart-pie"></i></div>
                                <div class="w-8 h-8 text-zinc-500 flex items-center justify-center text-xs"><i class="fa-solid fa-shield"></i></div>
                                <div class="w-8 h-8 text-zinc-500 flex items-center justify-center text-xs"><i class="fa-solid fa-gear"></i></div>
                            </div>
                            <div class="flex-1 bg-[#161b22] p-6 relative">
                                <div class="h-4 w-32 bg-white/10 rounded mb-8 mx-auto mt-2"></div>
                                <div class="h-8 w-48 bg-white/20 rounded mb-8 mx-auto"></div>
                                <div class="w-full h-32 bg-[#12161f] rounded-xl border border-white/5 flex p-4">
                                    <div class="flex-1 flex flex-col gap-3 justify-center">
                                        <div class="h-3 w-20 bg-white/10 rounded"></div>
                                        <div class="h-4 w-32 bg-white/20 rounded"></div>
                                    </div>
                                    <div class="w-24 h-24 rounded-full border-4 border-emerald-500 flex items-center justify-center text-emerald-500 font-bold text-lg">99%</div>
                                </div>
                            </div>
                        </div>
                    </div>
                </main>

                <div class="w-full max-w-7xl mx-auto px-8 flex flex-col lg:flex-row items-center justify-between relative z-10 py-20 mb-20 border-t border-white/5">
                    <div class="w-full lg:w-[45%] mb-16 lg:mb-0">
                        <h2 class="text-5xl font-black text-white mb-6">Auto Mod</h2>
                        <p class="text-zinc-300 text-lg mb-8 leading-relaxed max-w-md">
                            __BOT_NAME__ has a very unique auto mod that is based on a dynamic strike concept. The more filters a user triggers, the quicker they are systematically silenced.
                        </p>
                        <div class="flex flex-col items-start gap-3 font-semibold text-sm">
                            <span class="pill rounded-full px-4 py-1.5 shadow-sm">Advanced Anti Spam</span>
                            <span class="pill rounded-full px-4 py-1.5 shadow-sm">Advanced Anti Raid</span>
                            <span class="pill rounded-full px-4 py-1.5 shadow-sm">Anti Advertisement</span>
                            <span class="pill rounded-full px-4 py-1.5 shadow-sm">Anti NSFW Links</span>
                            <span class="pill rounded-full px-4 py-1.5 shadow-sm">Anti Malicious Links</span>
                            <span class="bg-white/10 text-white rounded-full px-3 py-1 text-xs mt-1">+4 more</span>
                        </div>
                    </div>

                    <div class="w-full lg:w-[45%] relative flex justify-end">
                        <div class="absolute -top-6 right-10 w-12 h-12 bg-emerald-500 rounded-full flex items-center justify-center text-white text-2xl z-20 shadow-[0_0_20px_rgba(16,185,129,0.5)] border-4 border-[#1e293b]">
                            <i class="fa-solid fa-check"></i>
                        </div>
                        <div class="discord-msg-mockup w-[400px] rounded-2xl p-6 relative z-10 border border-white/5">
                            <div class="flex gap-4 mb-6">
                                <div class="w-10 h-10 rounded-full bg-zinc-600 flex-shrink-0 flex items-center justify-center text-white"><i class="fa-solid fa-user-astronaut"></i></div>
                                <div>
                                    <div class="flex items-baseline gap-2 mb-1">
                                        <span class="text-white font-semibold text-[15px]">Salvi0</span>
                                        <span class="text-zinc-400 text-xs">Today at 4:45 PM</span>
                                    </div>
                                    <p class="text-zinc-200 text-[15px] leading-snug">
                                        I'm spamming the same message!<br>
                                        I'm spamming the same message!<br>
                                        I'm spamming the same message!<br>
                                        I'm spamming the same message!
                                    </p>
                                </div>
                            </div>
                            <div class="flex gap-4">
                                <img src="__BOT_AVATAR__" class="w-10 h-10 rounded-full flex-shrink-0 border border-white/10">
                                <div class="w-full">
                                    <div class="flex items-baseline gap-2 mb-2">
                                        <span class="text-pink-400 font-semibold text-[15px]">__BOT_NAME__</span>
                                        <span class="text-white bg-[#5865F2] text-[10px] px-1 rounded flex items-center gap-1"><i class="fa-solid fa-check"></i> BOT</span>
                                        <span class="text-zinc-400 text-xs">Today at 4:45 PM</span>
                                    </div>
                                    <div class="bg-[#2b2d31] border-l-4 border-yellow-500 rounded p-3 w-full shadow-inner">
                                        <p class="text-zinc-200 text-sm mb-1"><b>Salvi0</b> has been silenced for 30m!</p>
                                        <p class="text-zinc-400 text-xs uppercase font-bold tracking-wider"><i class="fa-solid fa-angle-right text-red-500 mr-1"></i> Reason: Similar Message Spam</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <script>
                    const container = document.getElementById('confetti-container');
                    const colors = ['#0ea5e9', '#d83b9e', '#8b5cf6', '#38bdf8', '#fce7f3'];
                    for(let i=0; i<30; i++) {
                        let conf = document.createElement('div');
                        conf.className = 'confetti rounded-sm';
                        conf.style.left = Math.random() * 100 + 'vw';
                        conf.style.backgroundColor = colors[Math.floor(Math.random() * colors.length)];
                        conf.style.animationDuration = (Math.random() * 5 + 5) + 's';
                        conf.style.animationDelay = (Math.random() * 5) + 's';
                        container.appendChild(conf);
                    }
                </script>
            </body>
            </html>
            """
            landing_html = landing_html.replace("__BOT_NAME__", str(bot_name))
            landing_html = landing_html.replace("__BOT_AVATAR__", str(bot_avatar))
            landing_html = landing_html.replace("__INVITE_LINK__", invite_link)
            return web.Response(text=landing_html, content_type='text/html')

        # --- AUTHENTICATED SERVER SELECTION PAGE (WICK REPLICA) ---
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"

        app_info = await self.bot.application_info()
        is_owner = int(user_session['discord_id']) == app_info.owner.id
        owner_button_html = '<a href="/owner_panel" class="text-xs bg-red-500/10 text-red-400 hover:bg-red-500/20 font-medium px-3 py-2 rounded-lg border border-red-500/20 transition"><i class="fa-solid fa-terminal"></i> System</a>' if is_owner else ""

        user_guilds = []
        access_token = user_session.get("access_token")
        if access_token:
            async with aiohttp.ClientSession() as session:
                user_headers = {"Authorization": f"Bearer {access_token}"}
                async with session.get("https://discord.com/api/users/@me/guilds", headers=user_headers) as resp:
                    if resp.status == 200:
                        user_guilds = await resp.json()

        admin_guilds = [
            g for g in user_guilds 
            if (int(g.get('permissions', 0)) & 0x8) == 0x8 or (int(g.get('permissions', 0)) & 0x20) == 0x20
        ]
        bot_guild_ids = [g.id for g in self.bot.guilds]
        
        guild_cards_html = ""
        if not admin_guilds:
            guild_cards_html = """
            <div class="col-span-full p-8 text-center bg-[#161b22] rounded-2xl border border-white/5 max-w-md mx-auto w-full mt-10">
                <i class="fa-solid fa-server text-4xl text-zinc-600 mb-4"></i>
                <h3 class="text-xl font-bold text-white mb-2">No Servers Found</h3>
                <p class="text-zinc-400 text-sm">You do not have Administrator permissions in any Discord servers.</p>
            </div>
            """
        else:
            for g in admin_guilds:
                is_in_server = int(g['id']) in bot_guild_ids
                icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get('icon') else f"https://ui-avatars.com/api/?name={urllib.parse.quote(g['name'])}&background=27272a&color=fff"
                target_url = f"/manage/{g['id']}" if is_in_server else invite_link
                
                bg_style = f"background-image: linear-gradient(rgba(11, 17, 33, 0.7), rgba(11, 17, 33, 0.9)), url('{icon_url}'); background-size: cover; background-position: center;"
                badge = f'<div class="absolute top-3 right-3 text-white/50"><i class="fa-solid fa-thumbtack transform rotate-45"></i></div>' if is_in_server else '<div class="absolute top-3 right-3 bg-black/60 border border-white/10 text-white text-[10px] font-bold px-2 py-1 rounded">INVITE</div>'

                guild_cards_html += f"""
                <a href="{target_url}" data-name="{g['name']}" data-id="{g['id']}" class="server-card relative w-64 h-52 rounded-2xl overflow-hidden group border-2 border-transparent hover:border-blue-500 transition-all shadow-xl block flex-shrink-0 cursor-pointer">
                    <div class="absolute inset-0 transition-transform duration-700 group-hover:scale-110" style="{bg_style}"></div>
                    <div class="absolute inset-0 flex flex-col items-center justify-center p-4">
                        <img src="{icon_url}" class="w-20 h-20 rounded-full shadow-2xl mb-4 border-2 border-white/10 group-hover:border-white/30 transition-all object-cover">
                        <div class="bg-black/60 backdrop-blur px-4 py-2 rounded-xl border border-white/5 w-11/12 text-center">
                            <h3 class="text-zinc-200 font-bold text-sm truncate">{g['name']}</h3>
                        </div>
                    </div>
                    {badge}
                </a>
                """

        dashboard_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__BOT_NAME__ | Select Server</title>
            <link rel="icon" type="image/png" href="__BOT_AVATAR__">
            <link rel="shortcut icon" href="__BOT_AVATAR__">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background-color: #0b1121; color: white; font-family: system-ui, -apple-system, sans-serif; }
                .wick-bg {
                    position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: -2;
                    background: radial-gradient(100% 100% at 80% 0%, rgba(14, 165, 233, 0.4) 0%, transparent 40%),
                                radial-gradient(100% 100% at 20% 100%, rgba(216, 59, 158, 0.2) 0%, transparent 40%);
                    opacity: 0.8; pointer-events: none;
                }
            </style>
        </head>
        <body class="relative min-h-screen flex flex-col selection:bg-blue-500 selection:text-white">
            <div class="wick-bg"></div>

            <nav class="flex items-center justify-between px-8 py-5 relative z-10 w-full">
                <div class="flex items-center gap-3 text-white font-bold text-xl tracking-tight">
                    <img src="__BOT_AVATAR__" class="w-8 h-8 rounded-full border border-white/10" alt="Icon">
                </div>
                <div class="flex items-center gap-6 text-zinc-400">
                    __OWNER_BUTTON__
                    <a href="#" class="hover:text-white transition"><i class="fa-regular fa-bell"></i></a>
                    <a href="#" class="hover:text-white transition"><i class="fa-solid fa-sun"></i></a>
                    <div class="flex items-center gap-2 bg-white/5 py-1.5 px-1.5 pr-4 rounded-full border border-white/5 cursor-pointer hover:bg-white/10 transition">
                        <img src="__USER_AVATAR__" alt="User" class="w-8 h-8 rounded-full">
                        <span class="text-xs font-semibold text-white">__USER_NAME__</span>
                    </div>
                    <a href="/logout" class="text-xs text-zinc-500 hover:text-white font-medium transition ml-2"><i class="fa-solid fa-right-from-bracket"></i></a>
                </div>
            </nav>

            <main class="flex-1 w-full max-w-6xl mx-auto px-6 py-12 relative z-10 text-center flex flex-col items-center justify-center -mt-10">
                
                <p class="text-zinc-300 text-xs font-bold tracking-[0.2em] uppercase mb-3">Servers</p>
                <h1 class="text-4xl md:text-5xl font-extrabold text-white mb-8 tracking-tight">Select the server you want to manage</h1>

                <div class="flex flex-col sm:flex-row items-center gap-4 text-sm font-medium text-zinc-300 w-full max-w-lg mx-auto">
                    <span>Filter:</span>
                    <div class="relative flex-1">
                        <input type="text" id="serverSearch" placeholder="Type name or ID" class="w-full bg-[#161b22] border border-white/10 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-blue-500 transition shadow-inner">
                    </div>
                    <button class="text-zinc-500 hover:text-white transition"><i class="fa-solid fa-rotate-right"></i></button>
                    <a href="#" class="text-zinc-500 hover:text-zinc-300 transition text-xs flex items-center gap-1 ml-2"><i class="fa-solid fa-key text-[10px]"></i> Lost control over __BOT_NAME__ in a server?</a>
                </div>

                <div class="flex flex-wrap justify-center gap-6 mt-16 w-full pb-20" id="serverGrid">
                    __GUILD_CARDS__
                </div>

            </main>

            <script>
                // Live Server Filtering Logic
                document.getElementById('serverSearch').addEventListener('input', function(e) {
                    const query = e.target.value.toLowerCase();
                    const cards = document.querySelectorAll('.server-card');
                    cards.forEach(card => {
                        const name = card.getAttribute('data-name').toLowerCase();
                        const id = card.getAttribute('data-id').toLowerCase();
                        if (name.includes(query) || id.includes(query)) {
                            card.style.display = 'block';
                        } else {
                            card.style.display = 'none';
                        }
                    });
                });
            </script>
        </body>
        </html>
        """
        
        dashboard_html = dashboard_html.replace("__BOT_NAME__", str(bot_name))
        dashboard_html = dashboard_html.replace("__BOT_AVATAR__", str(bot_avatar))
        dashboard_html = dashboard_html.replace("__USER_NAME__", str(user_name))
        dashboard_html = dashboard_html.replace("__USER_AVATAR__", str(user_avatar))
        dashboard_html = dashboard_html.replace("__OWNER_BUTTON__", owner_button_html)
        dashboard_html = dashboard_html.replace("__GUILD_CARDS__", guild_cards_html)
        
        return web.Response(text=dashboard_html, content_type='text/html')

    async def owner_panel(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.HTTPFound('/login')

        app_info = await self.bot.application_info()
        if int(user_session['discord_id']) != app_info.owner.id:
            return web.Response(text="Access Denied: You do not possess Developer clearance.", status=403)

        bot_name = self.bot.user.name if self.bot and self.bot.user else "Recluse"
        bot_avatar = self.get_bot_avatar()
        server_count = len(self.bot.guilds)
        member_count = sum(g.member_count for g in self.bot.guilds if g.member_count)
        
        is_locked = getattr(self.bot, 'global_lockdown', False)
        is_maintenance = getattr(self.bot, 'dashboard_maintenance', False)

        # Build Hot-Reload Cog List
        cogs_html = ""
        for ext_name in self.bot.extensions.keys():
            clean_name = ext_name.replace('cogs.', '')
            cogs_html += f"""
            <div class="flex justify-between items-center p-2.5 hover:bg-white/5 rounded-lg transition group border border-transparent hover:border-white/10">
                <span class="text-sm font-mono text-zinc-300"><i class="fa-solid fa-microchip text-zinc-600 mr-2"></i> {ext_name}</span>
                <button onclick="reloadCog('{clean_name}')" class="text-xs bg-blue-500/10 text-blue-400 hover:bg-blue-500 hover:text-white px-3 py-1.5 rounded-lg opacity-0 group-hover:opacity-100 transition-all transform translate-x-2 group-hover:translate-x-0 font-bold border border-blue-500/20"><i class="fa-solid fa-arrows-rotate mr-1"></i> Reload</button>
            </div>
            """

        visitor_html = ""
        banned_html = ""
        
        if hasattr(self.bot, 'db'):
            visits_cursor = self.bot.db.visit_logs.find().sort("last_visit", -1)
            async for v in visits_cursor:
                time_str = datetime.datetime.fromtimestamp(v['last_visit']).strftime('%Y-%m-%d %H:%M')
                last_user = v.get('last_user', 'Guest')
                user_badge_color = "bg-violet-500/20 text-violet-300 border-violet-500/30" if last_user != 'Guest' else "bg-zinc-500/20 text-zinc-400 border-zinc-500/30"
                
                visitor_html += f"""
                <div class="flex justify-between items-center p-3 border-b border-white/5 hover:bg-white/5 transition">
                    <div>
                        <div class="flex items-center gap-2 mb-1">
                            <span class="font-mono text-white text-sm">{v['ip']}</span>
                            <span class="text-[10px] px-2 py-0.5 rounded border {user_badge_color}">{last_user}</span>
                        </div>
                        <p class="text-xs text-zinc-500">Hits: {v.get('hits', 1)}</p>
                    </div>
                    <div class="flex items-center gap-3">
                        <span class="text-xs text-zinc-400">{time_str}</span>
                        <button onclick="submitIPAction('ban', '{v['ip']}')" class="text-xs bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white px-2 py-1 rounded border border-red-500/20 transition">Ban</button>
                    </div>
                </div>"""
                
            if not visitor_html: visitor_html = "<p class='text-zinc-500 text-sm p-3'>No visitors logged yet.</p>"

            bans_cursor = self.bot.db.ip_bans.find().limit(50)
            async for b in bans_cursor:
                banned_html += f"""
                <div class="flex justify-between items-center p-3 border-b border-red-500/10 hover:bg-red-500/5 transition">
                    <span class="font-mono text-red-400 text-sm">{b['ip']}</span>
                    <button onclick="submitIPAction('unban', '{b['ip']}')" class="text-xs bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500 hover:text-white px-2 py-1 rounded border border-emerald-500/20 transition">Unban</button>
                </div>"""
                
            if not banned_html: banned_html = "<p class='text-zinc-500 text-sm p-3'>No IPs are currently banned.</p>"

        # Dynamic UI States
        lock_btn_color = "bg-emerald-500 hover:bg-emerald-600 shadow-emerald-500/20" if is_locked else "bg-red-600 hover:bg-red-700 shadow-red-500/20"
        lock_btn_text = "LIFT LOCKDOWN" if is_locked else "ENGAGE DEFCON LOCKDOWN"
        lock_title_color = "text-red-500" if is_locked else "text-zinc-400"
        lock_icon_anim = "animate-pulse" if is_locked else ""
        
        maint_btn_color = "bg-emerald-500 hover:bg-emerald-600 shadow-emerald-500/20" if is_maintenance else "bg-yellow-600 hover:bg-yellow-700 shadow-yellow-500/20"
        maint_btn_text = "LIFT MAINTENANCE" if is_maintenance else "ENABLE MAINTENANCE"
        maint_title_color = "text-yellow-500" if is_maintenance else "text-zinc-400"
        maint_icon_anim = "animate-pulse" if is_maintenance else ""

        owner_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{bot_name} | Developer Override</title>
            <link rel="icon" type="image/png" href="{bot_avatar}">
            <link rel="shortcut icon" href="{bot_avatar}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                .glass-panel {{ background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); }}
                ::-webkit-scrollbar {{ width: 8px; }}
                ::-webkit-scrollbar-track {{ background: rgba(24, 24, 27, 0.6); }}
                ::-webkit-scrollbar-thumb {{ background: rgba(255, 255, 255, 0.1); border-radius: 4px; }}
                ::-webkit-scrollbar-thumb:hover {{ background: rgba(255, 255, 255, 0.2); }}
            </style>
        </head>
        <body class="bg-[#09090b] text-zinc-300 font-sans min-h-screen flex flex-col selection:bg-red-500 selection:text-white">

            <nav class="glass-panel sticky top-0 z-50 px-6 py-4 flex justify-between items-center border-b border-red-500/20">
                <div class="flex items-center gap-4">
                    <a href="/" class="text-zinc-400 hover:text-white transition"><i class="fa-solid fa-arrow-left"></i></a>
                    <div class="h-6 w-px bg-white/10"></div>
                    <span class="text-lg font-bold text-red-500 tracking-wide"><i class="fa-solid fa-terminal"></i> System Override</span>
                </div>
            </nav>

            <main class="flex-1 max-w-7xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8 pb-20">
                <div>
                    <h1 class="text-3xl font-extrabold text-white mb-2">Owner Control Panel</h1>
                    <p class="text-zinc-400">Global telemetry and administrative actions for {bot_name}.</p>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
                    <div class="glass-panel p-6 rounded-2xl border border-white/5">
                        <div class="w-10 h-10 rounded-lg bg-blue-500/10 flex items-center justify-center border border-blue-500/20 mb-4">
                            <i class="fa-solid fa-server text-blue-400"></i>
                        </div>
                        <h3 class="text-zinc-400 text-sm font-medium">Connected Servers</h3>
                        <p class="text-3xl font-bold text-white mt-1">{server_count}</p>
                    </div>
                    <div class="glass-panel p-6 rounded-2xl border border-white/5">
                        <div class="w-10 h-10 rounded-lg bg-emerald-500/10 flex items-center justify-center border border-emerald-500/20 mb-4">
                            <i class="fa-solid fa-users text-emerald-400"></i>
                        </div>
                        <h3 class="text-zinc-400 text-sm font-medium">Total Global Users</h3>
                        <p class="text-3xl font-bold text-white mt-1">{member_count}</p>
                    </div>
                    <div class="glass-panel p-6 rounded-2xl border border-white/5">
                        <div class="w-10 h-10 rounded-lg bg-purple-500/10 flex items-center justify-center border border-purple-500/20 mb-4">
                            <i class="fa-solid fa-puzzle-piece text-purple-400"></i>
                        </div>
                        <h3 class="text-zinc-400 text-sm font-medium">Active Modules</h3>
                        <p class="text-3xl font-bold text-white mt-1">{len(self.bot.cogs)}</p>
                    </div>
                </div>

                <h2 class="text-xl font-bold text-white mt-4 flex items-center gap-2 border-b border-white/5 pb-4">
                    <i class="fa-solid fa-bolt text-yellow-500"></i> Global System Overrides
                </h2>

                <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-4 gap-6">
                    
                    <div class="glass-panel p-6 rounded-2xl border border-red-500/30 flex flex-col relative overflow-hidden">
                        <div class="absolute -right-6 -top-6 text-red-500/5 text-9xl {lock_icon_anim}"><i class="fa-solid fa-power-off"></i></div>
                        <h3 class="{lock_title_color} font-bold text-lg mb-2 relative z-10"><i class="fa-solid fa-skull"></i> Master Lockdown</h3>
                        <p class="text-xs text-zinc-400 mb-6 relative z-10 leading-relaxed">Instantly paralyze network command processing globally. Use only during active severe API outages or zero-day exploits.</p>
                        <div class="mt-auto relative z-10">
                            <button onclick="toggleGlobalLockdown({str(not is_locked).lower()})" class="w-full py-3.5 rounded-xl {lock_btn_color} font-bold text-white transition shadow-lg tracking-wider" id="globalLockBtn">
                                {lock_btn_text}
                            </button>
                        </div>
                    </div>
                    
                    <div class="glass-panel p-6 rounded-2xl border border-yellow-500/30 flex flex-col relative overflow-hidden">
                        <div class="absolute -right-6 -top-6 text-yellow-500/5 text-9xl {maint_icon_anim}"><i class="fa-solid fa-gear"></i></div>
                        <h3 class="{maint_title_color} font-bold text-lg mb-2 relative z-10"><i class="fa-solid fa-wrench"></i> Web Maintenance</h3>
                        <p class="text-xs text-zinc-400 mb-6 relative z-10 leading-relaxed">Lock public access to the dashboard site. Bot commands remain fully functional in Discord.</p>
                        <div class="mt-auto relative z-10">
                            <button onclick="toggleDashboardMaintenance({str(not is_maintenance).lower()})" class="w-full py-3.5 rounded-xl {maint_btn_color} font-bold text-white transition shadow-lg tracking-wider" id="maintBtn">
                                {maint_btn_text}
                            </button>
                        </div>
                    </div>

                    <div class="glass-panel p-6 rounded-2xl border border-white/5 flex flex-col relative overflow-hidden">
                        <div class="absolute -right-6 -bottom-6 text-white/5 text-8xl"><i class="fa-solid fa-ban"></i></div>
                        <h3 class="text-white font-bold text-lg mb-2 relative z-10"><i class="fa-solid fa-user-shield text-zinc-400 mr-1"></i> Entity Blacklist</h3>
                        <p class="text-xs text-zinc-400 mb-4 relative z-10">Permanently sever a specific user ID or Guild ID from the entire network.</p>
                        <div class="space-y-3 mt-auto relative z-10">
                            <div class="flex gap-2">
                                <select id="bl_type" class="w-1/3 bg-[#18181b] border border-white/10 rounded-lg p-2 text-sm text-white focus:outline-none focus:border-purple-500 transition">
                                    <option value="user">User</option>
                                    <option value="guild">Guild</option>
                                </select>
                                <input type="text" id="bl_id" placeholder="Target ID..." class="w-2/3 bg-[#18181b] border border-white/10 rounded-lg p-2 text-sm font-mono text-white focus:outline-none focus:border-purple-500 transition">
                            </div>
                            <input type="text" id="bl_reason" placeholder="Reason for execution..." class="w-full bg-[#18181b] border border-white/10 rounded-lg p-2 text-sm text-white focus:outline-none focus:border-purple-500 transition">
                            <button onclick="submitBlacklist()" class="w-full py-2.5 rounded-lg bg-purple-500 hover:bg-purple-600 shadow-lg shadow-purple-500/20 text-white font-bold text-sm transition">
                                Execute Override
                            </button>
                        </div>
                    </div>

                    <div class="glass-panel p-6 rounded-2xl border border-white/5 flex flex-col relative">
                        <div class="flex justify-between items-start mb-2">
                            <h3 class="text-white font-bold text-lg"><i class="fa-solid fa-rotate-right text-zinc-400 mr-1"></i> Module Injection</h3>
                        </div>
                        <p class="text-xs text-zinc-400 mb-4">Recompile and hot-swap Python code in active memory.</p>
                        <div class="bg-[#090b10] rounded-xl border border-white/5 p-2 overflow-y-auto h-[170px] custom-scrollbar space-y-1 shadow-inner">
                            {cogs_html}
                        </div>
                    </div>
                </div>

                <h2 class="text-xl font-bold text-white mt-4 flex items-center gap-2 border-b border-white/5 pb-4">
                    <i class="fa-solid fa-shield-halved text-blue-500"></i> Network Firewall (IP Access)
                </h2>
                
                <div class="glass-panel p-6 rounded-2xl border border-white/5">
                    <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                        <div>
                            <h3 class="text-zinc-300 font-medium mb-3">All Logged Visitors</h3>
                            <div class="bg-[#18181b] rounded-xl border border-white/10 overflow-y-auto max-h-[300px]">
                                {visitor_html}
                            </div>
                        </div>
                        
                        <div>
                            <h3 class="text-red-400 font-medium mb-3">Restricted IP Addresses</h3>
                            <div class="bg-red-500/5 rounded-xl border border-red-500/20 overflow-y-auto max-h-[220px] mb-4">
                                {banned_html}
                            </div>
                            
                            <div class="flex gap-2">
                                <input type="text" id="manual_ip" placeholder="Enter IP address manually..." class="flex-1 bg-[#18181b] border border-white/10 rounded-xl p-2.5 text-white text-sm focus:outline-none focus:border-red-500 transition">
                                <button onclick="submitIPAction('ban', document.getElementById('manual_ip').value)" class="px-4 py-2.5 bg-red-500/20 text-red-400 font-semibold hover:bg-red-500 hover:text-white rounded-xl transition border border-red-500/30">Ban</button>
                                <button onclick="submitIPAction('unban', document.getElementById('manual_ip').value)" class="px-4 py-2.5 bg-emerald-500/20 text-emerald-400 font-semibold hover:bg-emerald-500 hover:text-white rounded-xl transition border border-emerald-500/30">Unban</button>
                            </div>
                        </div>
                    </div>
                </div>
            </main>
            
            <script>
                // --- IP FIREWALL ---
                async function submitIPAction(action, ip) {{
                    if(!ip) return alert("Please provide an IP address.");
                    if(action === 'ban' && !confirm(`Are you sure you want to permanently IP ban ${{ip}}?`)) return;
                    if(action === 'unban' && !confirm(`Are you sure you want to unban the IP: ${{ip}}?`)) return;
                    
                    try {{
                        const res = await fetch('/api/ip_action', {{
                            method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ action: action, ip: ip.trim() }})
                        }});
                        if(res.ok) window.location.reload();
                        else alert("Failed to execute action.");
                    }} catch (e) {{ alert("Network error."); }}
                }}

                // --- SYSTEM OVERRIDES ---
                async function toggleGlobalLockdown(targetState) {{
                    if (targetState && !confirm("CRITICAL WARNING: This will immediately paralyze the entire bot network. Proceed?")) return;
                    if (!targetState && !confirm("Lift global lockdown and resume normal operations?")) return;
                    
                    const btn = document.getElementById('globalLockBtn');
                    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Processing...';
                    
                    try {{
                        const res = await fetch('/api/owner_action', {{
                            method: 'POST', headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{action: 'global_lockdown', state: targetState}})
                        }});
                        if (res.ok) window.location.reload();
                        else throw new Error("Failed");
                    }} catch (e) {{ alert("Network error."); window.location.reload(); }}
                }}
                
                async function toggleDashboardMaintenance(targetState) {{
                    if (targetState && !confirm("Enable Web Maintenance? Users will not be able to access the dashboard. Bot commands will continue to work.")) return;
                    
                    const btn = document.getElementById('maintBtn');
                    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Processing...';
                    
                    try {{
                        const res = await fetch('/api/owner_action', {{
                            method: 'POST', headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{action: 'dashboard_maintenance', state: targetState}})
                        }});
                        if (res.ok) window.location.reload();
                        else throw new Error("Failed");
                    }} catch (e) {{ alert("Network error."); window.location.reload(); }}
                }}

                async function submitBlacklist() {{
                    const target_id = document.getElementById('bl_id').value;
                    const type = document.getElementById('bl_type').value;
                    const reason = document.getElementById('bl_reason').value;

                    if(!target_id || isNaN(target_id)) return alert("Valid ID required.");
                    if(!confirm(`Permanently blacklist ${{type.toUpperCase()}} ID ${{target_id}} globally? This cannot be undone from the dashboard.`)) return;

                    try {{
                        const res = await fetch('/api/owner_action', {{
                            method: 'POST', headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{action: 'blacklist', target_id: target_id, type: type, reason: reason}})
                        }});
                        if (res.ok) {{
                            alert("Target successfully neutralized.");
                            document.getElementById('bl_id').value = '';
                        }} else alert("Failed to execute blacklist.");
                    }} catch (e) {{ alert("Network error."); }}
                }}

                async function reloadCog(cogName) {{
                    try {{
                        const res = await fetch('/api/owner_action', {{
                            method: 'POST', headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{action: 'reload_cog', cog: cogName}})
                        }});
                        if (res.ok) alert(`${{cogName}} injected successfully.`);
                        else alert(`Injection Failed for ${{cogName}}. Check Python console for syntax tracebacks.`);
                    }} catch (e) {{ alert("Network error."); }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=owner_html, content_type='text/html')

    async def handle_owner_action(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.json_response({"error": "Unauthorized"}, status=401)
        
        app_info = await self.bot.application_info()
        if int(user_session['discord_id']) != app_info.owner.id:
            return web.json_response({"error": "Forbidden"}, status=403)
            
        try:
            data = await request.json()
            action = data.get('action')
            
            if action == 'global_lockdown':
                state = data.get('state', False)
                self.bot.global_lockdown = state
                return web.json_response({"success": True})
                
            elif action == 'dashboard_maintenance':
                state = data.get('state', False)
                self.bot.dashboard_maintenance = state
                return web.json_response({"success": True})
                
            elif action == 'blacklist':
                target_id = int(data.get('target_id'))
                b_type = data.get('type') 
                reason = data.get('reason') or "Dashboard System Override"
                
                if hasattr(self.bot, 'db'):
                    await self.bot.db.global_blacklist.update_one(
                        {"target_id": target_id, "type": b_type},
                        {"$set": {"reason": reason, "timestamp": datetime.datetime.utcnow().timestamp()}},
                        upsert=True
                    )
                    if b_type == 'guild':
                        guild = self.bot.get_guild(target_id)
                        if guild: await guild.leave()
                return web.json_response({"success": True})
                
            elif action == 'reload_cog':
                cog_name = data.get('cog')
                if not cog_name.startswith('cogs.'):
                    cog_name = f"cogs.{cog_name}"
                await self.bot.reload_extension(cog_name)
                return web.json_response({"success": True})
                
            return web.json_response({"error": "Invalid action"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_ip_action(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.json_response({"error": "Unauthorized"}, status=401)
        
        app_info = await self.bot.application_info()
        if int(user_session['discord_id']) != app_info.owner.id:
            return web.json_response({"error": "Forbidden"}, status=403)
            
        data = await request.json()
        action = data.get('action')
        target_ip = data.get('ip')
        
        if not target_ip or not hasattr(self.bot, 'db'): 
            return web.json_response({"error": "Invalid request"}, status=400)
            
        try:
            if action == 'ban':
                await self.bot.db.ip_bans.update_one(
                    {"ip": target_ip}, 
                    {"$set": {"banned_at": datetime.datetime.utcnow().timestamp()}}, 
                    upsert=True
                )
            elif action == 'unban':
                await self.bot.db.ip_bans.delete_one({"ip": target_ip})
                
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def manage_server(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        
        try:
            guild_id_int = int(guild_id)
        except ValueError:
            return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild:
            return web.Response(text="Recluse is not in this server. Please invite the bot first.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied: You do not have permission to manage this server.", status=403)

        bot_name = self.bot.user.name if self.bot and self.bot.user else "Recluse"
        bot_avatar = self.get_bot_avatar()
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"

        # --- FETCH SAVED SETTINGS FROM DATABASE ---
        ai_enabled = True
        automod_enabled = True
        anime_enabled = True
        sports_enabled = True
        misc_enabled = True
        default_ai_model = "nexusify"
        banned_words = []
        
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings:
                ai_enabled = settings.get("ai_enabled", True)
                automod_enabled = settings.get("automod_enabled", True)
                anime_enabled = settings.get("anime_enabled", True)
                sports_enabled = settings.get("sports_enabled", True)
                misc_enabled = settings.get("misc_enabled", True)
                default_ai_model = settings.get("default_ai_model", "nexusify")
                banned_words = settings.get("banned_words", ["unauthorized_term_1", "prohibited_phrase", "blacklisted_word"])
                
        ai_checked = "checked" if ai_enabled else ""
        mod_checked = "checked" if automod_enabled else ""
        anime_checked = "checked" if anime_enabled else ""
        sports_checked = "checked" if sports_enabled else ""
        misc_checked = "checked" if misc_enabled else ""
        
        nexusify_checked = "checked" if default_ai_model == "nexusify" else ""
        gemini_checked = "checked" if default_ai_model == "gemini" else ""
        sarvam_checked = "checked" if default_ai_model == "sarvam" else ""
        banned_words_str = ", ".join(banned_words)

        manage_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__GUILD_NAME__ | __BOT_NAME__ Dashboard</title>
            <link rel="icon" type="image/png" href="__BOT_AVATAR__">
            <link rel="shortcut icon" href="__BOT_AVATAR__">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }
                .glass-panel { background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }
                .sidebar-link { transition: all 0.2s; }
                .sidebar-link.active { background-color: #3b82f6; color: white; border-radius: 0.5rem; }
                .sidebar-link:hover:not(.active) { background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }
                
                /* Circular Progress Bar CSS */
                .circular-chart { display: block; margin: 0 auto; max-width: 80%; max-height: 250px; }
                .circle-bg { fill: none; stroke: rgba(255, 255, 255, 0.1); stroke-width: 3.8; }
                .circle { fill: none; stroke-width: 2.8; stroke-linecap: round; animation: progress 1s ease-out forwards; }
                @keyframes progress { 0% { stroke-dasharray: 0 100; } }
                .percentage { fill: #fff; font-family: sans-serif; font-size: 0.5em; text-anchor: middle; font-weight: bold; }
                
                .toggle-checkbox:checked { right: 0; border-color: #3b82f6; }
                .toggle-checkbox:checked + .toggle-label { background-color: #3b82f6; box-shadow: 0 0 10px rgba(59, 130, 246, 0.5); }
            </style>
        </head>
        <body class="text-zinc-300 font-sans h-screen flex overflow-hidden selection:bg-blue-500 selection:text-white">

            <aside class="w-64 bg-[#0d1117] border-r border-white/5 flex flex-col hidden md:flex flex-shrink-0 z-20 shadow-2xl">
                <div class="p-4 border-b border-white/5 relative group cursor-pointer hover:bg-white/5 transition">
                    <div class="flex items-center gap-3">
                        <img src="__GUILD_ICON__" alt="Server" class="w-10 h-10 rounded-full shadow-lg">
                        <div class="overflow-hidden">
                            <h2 class="text-white font-bold truncate text-sm">__GUILD_NAME__</h2>
                            <p class="text-[10px] text-zinc-500 font-mono">__GUILD_ID__</p>
                        </div>
                    </div>
                </div>

                <nav class="flex-1 overflow-y-auto p-3 space-y-1 mt-2 custom-scrollbar">
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-4">Main Menu</p>
                    <a href="/manage/__GUILD_ID__" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-white">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="/wizard/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="/misc/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="/lockdown/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-lock w-5 text-center"></i> Lockdown
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
                
                <div class="p-4 border-t border-white/5">
                    <a href="/" class="flex items-center gap-3 text-sm text-zinc-400 hover:text-white transition">
                        <i class="fa-solid fa-arrow-left"></i> Back to Servers
                    </a>
                </div>
            </aside>

            <main class="flex-1 flex flex-col h-screen overflow-hidden relative">
                
                <header class="h-16 border-b border-white/5 bg-[#090b10]/80 backdrop-blur flex items-center justify-between px-6 z-10 shrink-0">
                    <div class="flex items-center gap-3 md:hidden">
                        <img src="__GUILD_ICON__" class="w-8 h-8 rounded-full">
                        <span class="font-bold text-white text-sm">__GUILD_NAME__</span>
                    </div>
                    <div class="hidden md:block text-sm font-bold text-zinc-400 tracking-widest uppercase">Overview</div>
                    <div class="flex items-center gap-4">
                        <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 p-1.5 rounded-lg transition">
                            <span class="text-xs font-medium text-white">__USER_NAME__</span>
                            <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        </div>
                    </div>
                </header>

                <div class="flex-1 overflow-y-auto p-6 lg:p-10 pb-20">
                    
                    <div class="text-center mb-10 mt-4">
                        <p class="text-blue-500 font-bold tracking-widest text-xs mb-2">OVERVIEW</p>
                        <h1 class="text-4xl font-extrabold text-white">__GUILD_NAME__</h1>
                    </div>

                    <div class="max-w-5xl mx-auto glass-panel rounded-xl shadow-2xl flex flex-col md:flex-row overflow-hidden mb-12 border border-white/5">
                        <div class="flex-1 p-8">
                            <h3 class="text-white font-bold text-lg mb-6 border-b border-white/5 pb-2">DETAILS</h3>
                            <div class="grid grid-cols-2 gap-y-8 gap-x-4">
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Server Name <i class="fa-regular fa-copy cursor-pointer hover:text-white" onclick="navigator.clipboard.writeText('__GUILD_NAME__')"></i></p>
                                    <p class="text-white text-sm font-medium">__GUILD_NAME__</p>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Server ID <i class="fa-regular fa-copy cursor-pointer hover:text-white" onclick="navigator.clipboard.writeText('__GUILD_ID__')"></i></p>
                                    <p class="text-white text-sm font-medium font-mono">__GUILD_ID__</p>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Shard ID</p>
                                    <p class="text-white text-sm font-medium">0</p>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Type</p>
                                    <span class="bg-blue-500 text-white text-[10px] font-bold px-2 py-0.5 rounded shadow-lg shadow-blue-500/20">STANDARD</span>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Members</p>
                                    <p class="text-white text-sm font-medium">__MEMBER_COUNT__</p>
                                </div>
                            </div>
                        </div>

                        <div class="md:w-72 bg-[#12161f] p-8 flex flex-col border-l border-white/5 relative">
                            <h3 class="text-white font-bold text-lg mb-4 text-center">SECURITY</h3>
                            <div class="flex-1 flex items-center justify-center">
                                <svg viewBox="0 0 36 36" class="circular-chart" style="stroke: #10b981;">
                                    <path class="circle-bg" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                                    <path class="circle" stroke-dasharray="100, 100" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                                    <text x="18" y="20.35" class="percentage">100%</text>
                                </svg>
                            </div>
                            <div class="mt-4 pt-4 border-t border-white/5 flex items-center justify-between text-xs text-zinc-400">
                                <span><i class="fa-solid fa-circle-info mr-1"></i> Notes</span>
                                <div class="flex gap-1">
                                    <div class="w-4 h-4 rounded-full bg-emerald-500/20 text-emerald-500 flex items-center justify-center text-[8px] border border-emerald-500"><i class="fa-solid fa-check"></i></div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="max-w-5xl mx-auto">
                        <h4 class="text-center text-sm font-bold text-zinc-400 tracking-widest mb-6">QUICK SYSTEMS OVERVIEW</h4>
                        
                        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                            
                            <div class="glass-panel p-5 rounded-xl border border-white/5 relative overflow-hidden group transition-all" id="card-toggleAI">
                                <div class="flex justify-between items-center mb-3">
                                    <h3 class="text-white font-bold flex items-center gap-2"><i class="fa-solid fa-microchip text-blue-400"></i> AI Core</h3>
                                    <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in">
                                        <input type="checkbox" id="toggleAI" __AI_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                        <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                    </div>
                                </div>
                                <p class="text-xs text-zinc-400 mb-4 h-8">Generative chat and image processing engines.</p>
                                <button onclick="openModal('aiModal')" class="w-full text-xs bg-white/5 hover:bg-white/10 text-white font-medium py-2 rounded-lg transition border border-white/5"><i class="fa-solid fa-gear"></i> Configure Models</button>
                            </div>

                            <div class="glass-panel p-5 rounded-xl border border-white/5 relative overflow-hidden group transition-all" id="card-toggleMod">
                                <div class="flex justify-between items-center mb-3">
                                    <h3 class="text-white font-bold flex items-center gap-2"><i class="fa-solid fa-hammer text-red-400"></i> Auto Mod</h3>
                                    <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in">
                                        <input type="checkbox" id="toggleMod" __MOD_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                        <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                    </div>
                                </div>
                                <p class="text-xs text-zinc-400 mb-4 h-8">Enables warn, ban, mute, and dynamic chat filters.</p>
                                <a href="/automod/__GUILD_ID__" class="w-full block text-center text-xs bg-white/5 hover:bg-white/10 text-white font-medium py-2 rounded-lg transition border border-white/5"><i class="fa-solid fa-filter"></i> Edit Filters</a>
                            </div>

                            <div class="glass-panel p-5 rounded-xl border border-white/5 relative overflow-hidden group transition-all" id="card-toggleAnime">
                                <div class="flex justify-between items-center mb-3">
                                    <h3 class="text-white font-bold flex items-center gap-2"><i class="fa-solid fa-tv text-pink-400"></i> Anime API</h3>
                                    <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in">
                                        <input type="checkbox" id="toggleAnime" __ANIME_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                        <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                    </div>
                                </div>
                                <p class="text-xs text-zinc-400 h-8">Allows users to query MyAnimeList database.</p>
                            </div>

                            <div class="glass-panel p-5 rounded-xl border border-white/5 relative overflow-hidden group transition-all" id="card-toggleSports">
                                <div class="flex justify-between items-center mb-3">
                                    <h3 class="text-white font-bold flex items-center gap-2"><i class="fa-solid fa-baseball-bat-ball text-orange-400"></i> Live Sports</h3>
                                    <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in">
                                        <input type="checkbox" id="toggleSports" __SPORTS_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                        <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                    </div>
                                </div>
                                <p class="text-xs text-zinc-400 h-8">Cricket score tracking and real-time updates.</p>
                            </div>

                            <div class="glass-panel p-5 rounded-xl border border-white/5 relative overflow-hidden group transition-all" id="card-toggleMisc">
                                <div class="flex justify-between items-center mb-3">
                                    <h3 class="text-white font-bold flex items-center gap-2"><i class="fa-solid fa-box-open text-teal-400"></i> Miscellaneous</h3>
                                    <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in">
                                        <input type="checkbox" id="toggleMisc" __MISC_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                        <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                    </div>
                                </div>
                                <p class="text-xs text-zinc-400 h-8">AFK statuses, server info, avatars, and telemetry.</p>
                            </div>

                        </div>
                    </div>
                </div>
            </main>

            <div id="aiModal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/80 backdrop-blur-sm px-4">
                <div class="glass-panel w-full max-w-lg rounded-2xl p-6 border border-white/10 shadow-2xl">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-xl font-bold text-white"><i class="fa-solid fa-microchip text-blue-500 mr-2"></i> Configure AI Engine</h3>
                        <button onclick="closeModal('aiModal')" class="text-zinc-400 hover:text-white transition"><i class="fa-solid fa-times"></i></button>
                    </div>
                    <p class="text-zinc-400 text-sm mb-4">Select the default generative AI model for your server.</p>
                    
                    <div class="space-y-3 mb-6">
                        <label class="flex items-center gap-3 p-3 rounded-xl border border-white/5 bg-white/5 cursor-pointer hover:bg-white/10 transition">
                            <input type="radio" name="ai_model" value="nexusify" class="w-4 h-4 text-blue-500 bg-zinc-800 border-zinc-700" __NEXUSIFY_CHECKED__>
                            <span class="text-white font-medium">Nexusify (Kimi-k2.5)</span>
                        </label>
                        <label class="flex items-center gap-3 p-3 rounded-xl border border-white/5 bg-white/5 cursor-pointer hover:bg-white/10 transition">
                            <input type="radio" name="ai_model" value="gemini" class="w-4 h-4 text-blue-500 bg-zinc-800 border-zinc-700" __GEMINI_CHECKED__>
                            <span class="text-white font-medium">Google Gemini (Flash 2.5)</span>
                        </label>
                        <label class="flex items-center gap-3 p-3 rounded-xl border border-white/5 bg-white/5 cursor-pointer hover:bg-white/10 transition">
                            <input type="radio" name="ai_model" value="sarvam" class="w-4 h-4 text-blue-500 bg-zinc-800 border-zinc-700" __SARVAM_CHECKED__>
                            <span class="text-white font-medium">Sarvam AI (Text Only)</span>
                        </label>
                    </div>
                    <div class="flex justify-end gap-3">
                        <button onclick="closeModal('aiModal')" class="px-4 py-2 rounded-xl text-zinc-400 hover:text-white font-medium transition">Cancel</button>
                        <button id="saveAIBtn" onclick="saveAIModel()" class="px-5 py-2 rounded-xl bg-blue-500 hover:bg-blue-600 shadow-lg text-white font-semibold transition">Save Changes</button>
                    </div>
                </div>
            </div>

            <script>
                function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
                function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

                async function saveAIModel() {
                    const selectedModel = document.querySelector('input[name="ai_model"]:checked').value;
                    const btn = document.getElementById('saveAIBtn'); btn.innerText = 'Saving...';
                    await fetch(`/api/settings/__GUILD_ID__`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'update_ai_model', model: selectedModel }) });
                    closeModal('aiModal'); btn.innerText = 'Save Changes';
                }

                document.querySelectorAll('.toggle-checkbox').forEach(toggle => {
                    const updateVisuals = (element) => {
                        const card = document.getElementById('card-' + element.id);
                        const label = element.nextElementSibling;
                        if(element.checked) {
                            element.style.left = 'auto'; element.style.right = '0';
                            element.style.borderColor = '#3b82f6'; label.style.backgroundColor = '#3b82f6';
                            label.style.boxShadow = '0 0 10px rgba(59, 130, 246, 0.5)'; card.style.opacity = '1';
                        } else {
                            element.style.right = 'auto'; element.style.left = '0';
                            element.style.borderColor = '#52525b'; label.style.backgroundColor = '#52525b';
                            label.style.boxShadow = 'none'; card.style.opacity = '0.6';
                        }
                    };

                    updateVisuals(toggle);

                    toggle.addEventListener('change', async function() {
                        updateVisuals(this);
                        try {
                            const response = await fetch(`/api/settings/__GUILD_ID__`, {
                                method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ action: 'toggle', module: this.id, enabled: this.checked })
                            });
                            if (!response.ok) { this.checked = !this.checked; updateVisuals(this); }
                        } catch (error) { this.checked = !this.checked; updateVisuals(this); }
                    });
                });
            </script>
        </body>
        </html>
        """
        
        manage_html = manage_html.replace("__GUILD_NAME__", str(guild.name))
        manage_html = manage_html.replace("__GUILD_ICON__", str(guild_icon))
        manage_html = manage_html.replace("__BOT_NAME__", str(bot_name))
        manage_html = manage_html.replace("__BOT_AVATAR__", str(bot_avatar))
        manage_html = manage_html.replace("__USER_NAME__", str(user_name))
        manage_html = manage_html.replace("__USER_AVATAR__", str(user_avatar))
        manage_html = manage_html.replace("__GUILD_ID__", str(guild_id_int))
        manage_html = manage_html.replace("__MEMBER_COUNT__", str(guild.member_count))
        manage_html = manage_html.replace("__AI_CHECKED__", ai_checked)
        manage_html = manage_html.replace("__MOD_CHECKED__", mod_checked)
        manage_html = manage_html.replace("__ANIME_CHECKED__", anime_checked)
        manage_html = manage_html.replace("__SPORTS_CHECKED__", sports_checked)
        manage_html = manage_html.replace("__MISC_CHECKED__", misc_checked)
        manage_html = manage_html.replace("__NEXUSIFY_CHECKED__", nexusify_checked)
        manage_html = manage_html.replace("__GEMINI_CHECKED__", gemini_checked)
        manage_html = manage_html.replace("__SARVAM_CHECKED__", sarvam_checked)
        
        return web.Response(text=manage_html, content_type='text/html')

    async def update_settings(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.json_response({"error": "Unauthorized"}, status=401)
            
        guild_id = request.match_info.get('guild_id')
        try:
            guild_id_int = int(guild_id)
        except ValueError:
            return web.json_response({"error": "Invalid Server ID"}, status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild:
            return web.json_response({"error": "Recluse is not in this server"}, status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.json_response({"error": "Forbidden: Missing Permissions"}, status=403)
            
        try:
            data = await request.json()
            action = data.get('action')
            
            if action == 'update_ai_model':
                model = data.get('model')
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": {"default_ai_model": model}},
                        upsert=True
                    )
                return web.json_response({"success": True})
                
            elif action == 'update_automod':
                words_string = data.get('words', '')
                words_list = [w.strip().lower() for w in words_string.split(',') if w.strip()]
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": {"banned_words": words_list}},
                        upsert=True
                    )
                return web.json_response({"success": True})
                
            elif action == 'toggle':
                module = data.get('module')
                enabled = data.get('enabled')
                
                db_mapping = {
                    'toggleAI': 'ai_enabled',
                    'toggleMod': 'automod_enabled',
                    'toggleAnime': 'anime_enabled',
                    'toggleSports': 'sports_enabled',
                    'toggleMisc': 'misc_enabled'
                }
                
                if module not in db_mapping:
                    return web.json_response({"error": "Invalid module name"}, status=400)
                    
                db_field = db_mapping[module]
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": {db_field: enabled}},
                        upsert=True
                    )
                return web.json_response({"success": True})
                
            elif action == 'bulk_update':
                new_settings = data.get('settings', {})
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": new_settings},
                        upsert=True
                    )
                return web.json_response({"success": True})

            elif action == 'toggle_lockdown':
                state = data.get('state', True)
                try:
                    if hasattr(self.bot, 'db'):
                        await self.bot.db.guild_settings.update_one(
                            {"guild_id": guild_id_int},
                            {"$set": {"lockdown_active": state}},
                            upsert=True
                        )
                    default_role = guild.default_role
                    await default_role.edit(send_messages=not state, reason="Dashboard: Emergency Lockdown Toggled")
                    return web.json_response({"success": True})
                except discord.Forbidden:
                    return web.json_response({"error": "Recluse lacks the 'Manage Roles' permission needed to lock the server."}, status=403)
            
            return web.json_response({"error": "Invalid action"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def server_logs(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        try: guild_id_int = int(guild_id)
        except ValueError: return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        mod_logs_html = ""
        security_logs_html = ""
        
        if hasattr(self.bot, 'db'):
            mod_cursor = self.bot.db.mod_logs.find({"guild_id": guild_id_int}).sort("timestamp", -1).limit(50)
            async for log in mod_cursor:
                action = log.get('action', 'Unknown')
                if action == 'Ban': badge = '<span class="px-2 py-1 bg-red-500/10 text-red-500 border border-red-500/20 rounded text-[10px] font-bold uppercase tracking-wider">Ban</span>'
                elif action == 'Kick': badge = '<span class="px-2 py-1 bg-orange-500/10 text-orange-500 border border-orange-500/20 rounded text-[10px] font-bold uppercase tracking-wider">Kick</span>'
                elif action == 'Warn': badge = '<span class="px-2 py-1 bg-yellow-500/10 text-yellow-500 border border-yellow-500/20 rounded text-[10px] font-bold uppercase tracking-wider">Warn</span>'
                else: badge = f'<span class="px-2 py-1 bg-blue-500/10 text-blue-500 border border-blue-500/20 rounded text-[10px] font-bold uppercase tracking-wider">{action}</span>'

                time_str = datetime.datetime.utcfromtimestamp(log['timestamp']).strftime('%Y-%m-%d %H:%M:%S UTC')
                mod_logs_html += f"""
                <tr class="border-b border-white/5 hover:bg-white/5 transition text-sm text-zinc-300">
                    <td class="py-3 px-4">{badge}</td>
                    <td class="py-3 px-4 font-medium text-white">{log.get('target_name')} <span class="text-xs text-zinc-500 block">{log.get('target_id')}</span></td>
                    <td class="py-3 px-4">{log.get('moderator_name')}</td>
                    <td class="py-3 px-4 max-w-xs truncate" title="{log.get('reason')}">{log.get('reason')}</td>
                    <td class="py-3 px-4 text-xs font-mono text-zinc-500">{time_str}</td>
                </tr>
                """
                
            if not mod_logs_html:
                mod_logs_html = '<tr><td colspan="5" class="py-8 text-center text-zinc-500">No manual moderation logs found for this server.</td></tr>'

            sec_cursor = self.bot.db.security_logs.find({"guild_id": guild_id_int}).sort("timestamp", -1).limit(50)
            async for log in sec_cursor:
                time_str = datetime.datetime.utcfromtimestamp(log['timestamp']).strftime('%Y-%m-%d %H:%M:%S UTC')
                security_logs_html += f"""
                <tr class="border-b border-white/5 hover:bg-white/5 transition text-sm text-zinc-300">
                    <td class="py-3 px-4"><span class="px-2 py-1 bg-emerald-500/10 text-emerald-500 border border-emerald-500/20 rounded text-[10px] font-bold uppercase tracking-wider"><i class="fa-solid fa-robot mr-1"></i> Auto Mod</span></td>
                    <td class="py-3 px-4 font-medium text-white">{log.get('user_name')} <span class="text-xs text-zinc-500 block">{log.get('user_id')}</span></td>
                    <td class="py-3 px-4 text-red-400 font-mono text-xs max-w-xs truncate" title="{log.get('content')}">{log.get('content')}</td>
                    <td class="py-3 px-4 text-xs font-mono text-zinc-500">{time_str}</td>
                </tr>
                """
                
            if not security_logs_html:
                security_logs_html = '<tr><td colspan="4" class="py-8 text-center text-zinc-500">No automated security infractions logged yet.</td></tr>'

        bot_avatar = self.get_bot_avatar()
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"

        logs_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__GUILD_NAME__ | Audit Logs</title>
            <link rel="icon" type="image/png" href="__BOT_AVATAR__">
            <link rel="shortcut icon" href="__BOT_AVATAR__">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }
                .glass-panel { background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }
                .sidebar-link { transition: all 0.2s; }
                .sidebar-link.active { background-color: #3b82f6; color: white; border-radius: 0.5rem; }
                .sidebar-link:hover:not(.active) { background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }
                .table-container::-webkit-scrollbar { height: 8px; width: 8px; }
                .table-container::-webkit-scrollbar-track { background: #0d1117; }
                .table-container::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
            </style>
        </head>
        <body class="text-zinc-300 font-sans h-screen flex overflow-hidden selection:bg-blue-500 selection:text-white">

            <aside class="w-64 bg-[#0d1117] border-r border-white/5 flex flex-col hidden md:flex flex-shrink-0 z-20 shadow-2xl">
                <div class="p-4 border-b border-white/5 relative group cursor-pointer hover:bg-white/5 transition">
                    <div class="flex items-center gap-3">
                        <img src="__GUILD_ICON__" alt="Server" class="w-10 h-10 rounded-full shadow-lg">
                        <div class="overflow-hidden">
                            <h2 class="text-white font-bold truncate text-sm">__GUILD_NAME__</h2>
                            <p class="text-[10px] text-zinc-500 font-mono">__GUILD_ID__</p>
                        </div>
                    </div>
                </div>

                <nav class="flex-1 overflow-y-auto p-3 space-y-1 mt-2 custom-scrollbar">
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-4">Main Menu</p>
                    <a href="/manage/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="/wizard/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="/misc/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="/lockdown/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-lock w-5 text-center"></i> Lockdown
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/__GUILD_ID__" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-white">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
                
                <div class="p-4 border-t border-white/5">
                    <a href="/" class="flex items-center gap-3 text-sm text-zinc-400 hover:text-white transition">
                        <i class="fa-solid fa-arrow-left"></i> Back to Servers
                    </a>
                </div>
            </aside>

            <main class="flex-1 flex flex-col h-screen overflow-hidden relative">
                <header class="h-16 border-b border-white/5 bg-[#090b10]/80 backdrop-blur flex items-center justify-between px-6 z-10 shrink-0">
                    <div class="flex items-center gap-3 md:hidden">
                        <img src="__GUILD_ICON__" class="w-8 h-8 rounded-full">
                        <span class="font-bold text-white text-sm">__GUILD_NAME__</span>
                    </div>
                    <div class="hidden md:block text-sm font-bold text-zinc-400 tracking-widest uppercase">Security Audits</div> 
                    <div class="flex items-center gap-4">
                        <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 p-1.5 rounded-lg transition">
                            <span class="text-xs font-medium text-white">__USER_NAME__</span>
                            <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        </div>
                    </div>
                </header>

                <div class="flex-1 overflow-y-auto p-6 lg:p-10 pb-20">
                    <div class="mb-8">
                        <h1 class="text-3xl font-extrabold text-white mb-2"><i class="fa-solid fa-server text-blue-500 mr-2"></i> Server Audit Logs</h1>
                        <p class="text-zinc-400 text-sm">Review manual moderation actions and automated security interventions in real-time.</p>
                    </div>

                    <div class="glass-panel rounded-xl shadow-2xl overflow-hidden mb-10 border border-emerald-500/20">
                        <div class="bg-emerald-500/10 border-b border-emerald-500/20 px-6 py-4 flex justify-between items-center">
                            <h3 class="text-emerald-400 font-bold tracking-wide flex items-center gap-2"><i class="fa-solid fa-shield-halved"></i> Automated Security Interventions</h3>
                            <span class="text-xs text-emerald-500/70 font-mono">Last 50 Events</span>
                        </div>
                        <div class="overflow-x-auto table-container">
                            <table class="w-full text-left whitespace-nowrap">
                                <thead class="bg-[#0d1117] text-zinc-500 text-xs uppercase tracking-wider">
                                    <tr>
                                        <th class="py-3 px-4 font-medium">Trigger</th>
                                        <th class="py-3 px-4 font-medium">Offender</th>
                                        <th class="py-3 px-4 font-medium">Intercepted Content</th>
                                        <th class="py-3 px-4 font-medium">Timestamp</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    __SECURITY_LOGS__
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div class="glass-panel rounded-xl shadow-2xl overflow-hidden border border-white/5">
                        <div class="bg-[#12161f] border-b border-white/5 px-6 py-4 flex justify-between items-center">
                            <h3 class="text-white font-bold tracking-wide flex items-center gap-2"><i class="fa-solid fa-gavel text-zinc-400"></i> Manual Moderation History</h3>
                            <span class="text-xs text-zinc-500 font-mono">Last 50 Events</span>
                        </div>
                        <div class="overflow-x-auto table-container">
                            <table class="w-full text-left whitespace-nowrap">
                                <thead class="bg-[#0d1117] text-zinc-500 text-xs uppercase tracking-wider">
                                    <tr>
                                        <th class="py-3 px-4 font-medium">Action</th>
                                        <th class="py-3 px-4 font-medium">Target</th>
                                        <th class="py-3 px-4 font-medium">Moderator</th>
                                        <th class="py-3 px-4 font-medium">Provided Reason</th>
                                        <th class="py-3 px-4 font-medium">Timestamp</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    __MOD_LOGS__
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </main>
        </body>
        </html>
        """
        logs_html = logs_html.replace("__GUILD_NAME__", str(guild.name))
        logs_html = logs_html.replace("__GUILD_ICON__", str(guild_icon))
        logs_html = logs_html.replace("__BOT_AVATAR__", str(bot_avatar))
        logs_html = logs_html.replace("__GUILD_ID__", str(guild_id_int))
        logs_html = logs_html.replace("__USER_NAME__", str(user_name))
        logs_html = logs_html.replace("__USER_AVATAR__", str(user_avatar))
        logs_html = logs_html.replace("__MOD_LOGS__", mod_logs_html)
        logs_html = logs_html.replace("__SECURITY_LOGS__", security_logs_html)
        return web.Response(text=logs_html, content_type='text/html')

    async def auto_mod(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        try: guild_id_int = int(guild_id)
        except ValueError: return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        banned_words = ["unauthorized_term_1", "prohibited_phrase", "blacklisted_word"]
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings and "banned_words" in settings:
                banned_words = settings["banned_words"]

        banned_words_raw = ", ".join(banned_words)
        pills_html = ""
        if not banned_words or (len(banned_words) == 1 and banned_words[0] == ""):
            pills_html = "<p class='text-zinc-500 text-sm'>No words currently blacklisted. The AI will not filter any specific terms.</p>"
        else:
            for word in banned_words:
                if word.strip():
                    pills_html += f'<span class="px-3 py-1.5 bg-red-500/10 border border-red-500/20 text-red-400 text-xs font-mono rounded-md shadow-sm">{word}</span>\n'

        bot_avatar = self.get_bot_avatar()
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"

        automod_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__GUILD_NAME__ | Auto Mod</title>
            <link rel="icon" type="image/png" href="__BOT_AVATAR__">
            <link rel="shortcut icon" href="__BOT_AVATAR__">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }
                .glass-panel { background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }
                .sidebar-link { transition: all 0.2s; }
                .sidebar-link.active { background-color: #3b82f6; color: white; border-radius: 0.5rem; }
                .sidebar-link:hover:not(.active) { background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }
            </style>
        </head>
        <body class="text-zinc-300 font-sans h-screen flex overflow-hidden selection:bg-blue-500 selection:text-white">

            <aside class="w-64 bg-[#0d1117] border-r border-white/5 flex flex-col hidden md:flex flex-shrink-0 z-20 shadow-2xl">
                <div class="p-4 border-b border-white/5 relative group cursor-pointer hover:bg-white/5 transition">
                    <div class="flex items-center gap-3">
                        <img src="__GUILD_ICON__" alt="Server" class="w-10 h-10 rounded-full shadow-lg">
                        <div class="overflow-hidden">
                            <h2 class="text-white font-bold truncate text-sm">__GUILD_NAME__</h2>
                            <p class="text-[10px] text-zinc-500 font-mono">__GUILD_ID__</p>
                        </div>
                    </div>
                </div>

                <nav class="flex-1 overflow-y-auto p-3 space-y-1 mt-2 custom-scrollbar">
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-4">Main Menu</p>
                    <a href="/manage/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="/wizard/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="/misc/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-white">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="/lockdown/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-lock w-5 text-center"></i> Lockdown
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
                
                <div class="p-4 border-t border-white/5">
                    <a href="/" class="flex items-center gap-3 text-sm text-zinc-400 hover:text-white transition">
                        <i class="fa-solid fa-arrow-left"></i> Back to Servers
                    </a>
                </div>
            </aside>

            <main class="flex-1 flex flex-col h-screen overflow-hidden relative">
                <header class="h-16 border-b border-white/5 bg-[#090b10]/80 backdrop-blur flex items-center justify-between px-6 z-10 shrink-0">
                    <div class="flex items-center gap-3 md:hidden">
                        <img src="__GUILD_ICON__" class="w-8 h-8 rounded-full">
                        <span class="font-bold text-white text-sm">__GUILD_NAME__</span>
                    </div>
                    <div class="hidden md:block text-sm font-bold text-zinc-400 tracking-widest uppercase">Content Filtration</div> 
                    <div class="flex items-center gap-4">
                        <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 p-1.5 rounded-lg transition">
                            <span class="text-xs font-medium text-white">__USER_NAME__</span>
                            <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        </div>
                    </div>
                </header>

                <div class="flex-1 overflow-y-auto p-6 lg:p-10 pb-20 custom-scrollbar">
                    <div class="mb-8 max-w-4xl mx-auto">
                        <h1 class="text-3xl font-extrabold text-white mb-2"><i class="fa-solid fa-shield-halved text-red-500 mr-2"></i> Auto Mod Lexicon</h1>
                        <p class="text-zinc-400 text-sm">Configure the exact terminology and phrasing that will trigger the AI's automated deletion and strike protocols.</p>
                    </div>

                    <div class="max-w-4xl mx-auto grid grid-cols-1 lg:grid-cols-5 gap-6">
                        <div class="lg:col-span-3 glass-panel rounded-xl shadow-2xl overflow-hidden border border-white/5">
                            <div class="bg-[#12161f] border-b border-white/5 px-6 py-4">
                                <h3 class="text-white font-bold tracking-wide flex items-center gap-2"><i class="fa-solid fa-pen-to-square text-zinc-400"></i> Edit Rule Set</h3>
                            </div>
                            <div class="p-6">
                                <label class="block text-xs font-bold text-zinc-500 uppercase tracking-wider mb-2">Blacklisted Terminology</label>
                                <p class="text-xs text-zinc-400 mb-4">Enter words or exact phrases you wish to ban. Separate each entry with a comma.</p>
                                <textarea id="banned_words_input" rows="6" class="w-full bg-[#0d1117] border border-white/10 rounded-lg p-4 text-white text-sm font-mono focus:outline-none focus:border-blue-500 transition resize-none placeholder-zinc-700 shadow-inner">__BANNED_WORDS_RAW__</textarea>
                                <div class="mt-6 flex justify-end">
                                    <button id="saveModBtn" onclick="saveAutomod()" class="px-6 py-2.5 rounded-lg bg-blue-500 hover:bg-blue-600 shadow-lg shadow-blue-500/20 text-white font-bold text-sm transition flex items-center gap-2">
                                        <i class="fa-solid fa-cloud-arrow-up"></i> Deploy Rules
                                    </button>
                                </div>
                            </div>
                        </div>

                        <div class="lg:col-span-2 glass-panel rounded-xl shadow-2xl overflow-hidden border border-red-500/20 flex flex-col">
                            <div class="bg-red-500/5 border-b border-red-500/20 px-6 py-4">
                                <h3 class="text-red-400 font-bold tracking-wide flex items-center gap-2"><i class="fa-solid fa-ban"></i> Active Filters</h3>
                            </div>
                            <div class="p-6 flex-1 bg-red-500/5">
                                <p class="text-xs text-zinc-400 mb-4">The following terms are currently loaded into the active memory bank.</p>
                                <div class="flex flex-wrap gap-2">
                                    __BANNED_WORDS_PILLS__
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="max-w-4xl mx-auto mt-6 p-4 rounded-xl bg-blue-500/10 border border-blue-500/20 flex gap-4 items-start">
                        <i class="fa-solid fa-circle-info text-blue-400 mt-1"></i>
                        <div>
                            <h4 class="text-blue-400 font-bold text-sm mb-1">How it works</h4>
                            <p class="text-xs text-blue-300/80 leading-relaxed">
                                When a user triggers one of these filters, Recluse will immediately intercept and delete the message. A strike will be recorded against the user's ID, and the infraction will be logged in your <strong>Server Audit Logs</strong> page. 
                            </p>
                        </div>
                    </div>
                </div>
            </main>

            <script>
                async function saveAutomod() {
                    const words = document.getElementById('banned_words_input').value;
                    const btn = document.getElementById('saveModBtn'); 
                    const originalText = btn.innerHTML;
                    
                    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Deploying...';
                    btn.classList.add('opacity-75');
                    
                    try {
                        const response = await fetch(`/api/settings/__GUILD_ID__`, { 
                            method: 'POST', 
                            headers: { 'Content-Type': 'application/json' }, 
                            body: JSON.stringify({ action: 'update_automod', words: words }) 
                        });
                        
                        if (response.ok) {
                            btn.innerHTML = '<i class="fa-solid fa-check"></i> Deployed';
                            btn.classList.remove('bg-blue-500', 'hover:bg-blue-600');
                            btn.classList.add('bg-emerald-500', 'hover:bg-emerald-600', 'shadow-emerald-500/20');
                            setTimeout(() => { window.location.reload(); }, 1000);
                        } else {
                            throw new Error("Failed to save");
                        }
                    } catch (e) {
                        btn.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Error';
                        btn.classList.remove('bg-blue-500', 'hover:bg-blue-600');
                        btn.classList.add('bg-red-500', 'hover:bg-red-600', 'shadow-red-500/20');
                        setTimeout(() => { 
                            btn.innerHTML = originalText;
                            btn.className = 'px-6 py-2.5 rounded-lg bg-blue-500 hover:bg-blue-600 shadow-lg shadow-blue-500/20 text-white font-bold text-sm transition flex items-center gap-2';
                        }, 2000);
                    }
                }
            </script>
        </body>
        </html>
        """
        automod_html = automod_html.replace("__GUILD_NAME__", str(guild.name))
        automod_html = automod_html.replace("__GUILD_ICON__", str(guild_icon))
        automod_html = automod_html.replace("__BOT_AVATAR__", str(bot_avatar))
        automod_html = automod_html.replace("__GUILD_ID__", str(guild_id_int))
        automod_html = automod_html.replace("__USER_NAME__", str(user_name))
        automod_html = automod_html.replace("__USER_AVATAR__", str(user_avatar))
        automod_html = automod_html.replace("__BANNED_WORDS_RAW__", banned_words_raw)
        automod_html = automod_html.replace("__BANNED_WORDS_PILLS__", pills_html)
        return web.Response(text=automod_html, content_type='text/html')

    async def wizard_setup(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        try: guild_id_int = int(guild_id)
        except ValueError: return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        default_ai_model = "nexusify"
        automod_enabled = True
        anime_enabled = True
        sports_enabled = True
        misc_enabled = True
        
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings:
                default_ai_model = settings.get("default_ai_model", "nexusify")
                automod_enabled = settings.get("automod_enabled", True)
                anime_enabled = settings.get("anime_enabled", True)
                sports_enabled = settings.get("sports_enabled", True)
                misc_enabled = settings.get("misc_enabled", True)

        nexusify_checked = "checked" if default_ai_model == "nexusify" else ""
        gemini_checked = "checked" if default_ai_model == "gemini" else ""
        sarvam_checked = "checked" if default_ai_model == "sarvam" else ""
        mod_checked = "checked" if automod_enabled else ""
        anime_checked = "checked" if anime_enabled else ""
        sports_checked = "checked" if sports_enabled else ""
        misc_checked = "checked" if misc_enabled else ""

        bot_avatar = self.get_bot_avatar()
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"

        wizard_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__GUILD_NAME__ | Setup Wizard</title>
            <link rel="icon" type="image/png" href="__BOT_AVATAR__">
            <link rel="shortcut icon" href="__BOT_AVATAR__">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }
                .glass-panel { background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }
                .sidebar-link { transition: all 0.2s; }
                .sidebar-link.active { background-color: #3b82f6; color: white; border-radius: 0.5rem; }
                .sidebar-link:hover:not(.active) { background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }
                .step-content { display: none; animation: fadeIn 0.4s ease-in-out; }
                .step-content.active { display: block; }
                @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
                .toggle-checkbox:checked { right: 0; border-color: #3b82f6; }
                .toggle-checkbox:checked + .toggle-label { background-color: #3b82f6; box-shadow: 0 0 10px rgba(59, 130, 246, 0.5); }
            </style>
        </head>
        <body class="text-zinc-300 font-sans h-screen flex overflow-hidden selection:bg-blue-500 selection:text-white">

            <aside class="w-64 bg-[#0d1117] border-r border-white/5 flex flex-col hidden md:flex flex-shrink-0 z-20 shadow-2xl">
                <div class="p-4 border-b border-white/5 relative group cursor-pointer hover:bg-white/5 transition">
                    <div class="flex items-center gap-3">
                        <img src="__GUILD_ICON__" alt="Server" class="w-10 h-10 rounded-full shadow-lg">
                        <div class="overflow-hidden">
                            <h2 class="text-white font-bold truncate text-sm">__GUILD_NAME__</h2>
                            <p class="text-[10px] text-zinc-500 font-mono">__GUILD_ID__</p>
                        </div>
                    </div>
                </div>

                <nav class="flex-1 overflow-y-auto p-3 space-y-1 mt-2 custom-scrollbar">
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-4">Main Menu</p>
                    <a href="/manage/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="/wizard/__GUILD_ID__" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-white">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="/misc/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="/lockdown/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-lock w-5 text-center"></i> Lockdown
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
                
                <div class="p-4 border-t border-white/5">
                    <a href="/" class="flex items-center gap-3 text-sm text-zinc-400 hover:text-white transition">
                        <i class="fa-solid fa-arrow-left"></i> Back to Servers
                    </a>
                </div>
            </aside>

            <main class="flex-1 flex flex-col h-screen overflow-hidden relative">
                <header class="h-16 border-b border-white/5 bg-[#090b10]/80 backdrop-blur flex items-center justify-between px-6 z-10 shrink-0">
                    <div class="flex items-center gap-3 md:hidden">
                        <img src="__GUILD_ICON__" class="w-8 h-8 rounded-full">
                        <span class="font-bold text-white text-sm">__GUILD_NAME__</span>
                    </div>
                    <div class="hidden md:block text-sm font-bold text-zinc-400 tracking-widest uppercase">Configuration Wizard</div> 
                    <div class="flex items-center gap-4">
                        <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 p-1.5 rounded-lg transition">
                            <span class="text-xs font-medium text-white">__USER_NAME__</span>
                            <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        </div>
                    </div>
                </header>

                <div class="flex-1 overflow-y-auto p-6 lg:p-10 pb-20 flex flex-col items-center justify-center">
                    <div class="w-full max-w-3xl glass-panel rounded-2xl shadow-2xl overflow-hidden border border-white/5">
                        <div class="bg-[#12161f] border-b border-white/5 p-6 flex items-center justify-between relative overflow-hidden">
                            <div class="absolute bottom-0 left-0 h-1 bg-blue-500 transition-all duration-500 ease-in-out" id="progressBar" style="width: 25%;"></div>
                            <div class="flex flex-col">
                                <span class="text-blue-500 font-bold text-xs tracking-widest mb-1" id="stepIndicatorText">STEP 1 OF 4</span>
                                <h2 class="text-white font-bold text-xl" id="stepTitle">Select AI Core</h2>
                            </div>
                            <i class="fa-solid fa-wand-magic-sparkles text-3xl text-zinc-700"></i>
                        </div>

                        <div class="p-8 min-h-[350px]">
                            <div id="step1" class="step-content active">
                                <p class="text-zinc-400 text-sm mb-6">Select the primary generative intelligence engine Recluse will use.</p>
                                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                                    <label class="flex flex-col p-4 rounded-xl border border-white/10 bg-[#0d1117] cursor-pointer hover:border-blue-500 transition group relative overflow-hidden">
                                        <input type="radio" name="wiz_ai_model" value="nexusify" class="absolute right-4 top-4 text-blue-500 bg-zinc-800 border-zinc-700" __NEXUSIFY_CHECKED__>
                                        <i class="fa-solid fa-network-wired text-2xl text-blue-400 mb-3 group-hover:scale-110 transition"></i>
                                        <span class="text-white font-bold text-sm">Nexusify</span>
                                        <span class="text-zinc-500 text-xs mt-1">Grok-3 architecture. Deep reasoning enabled.</span>
                                    </label>
                                    <label class="flex flex-col p-4 rounded-xl border border-white/10 bg-[#0d1117] cursor-pointer hover:border-blue-500 transition group relative overflow-hidden">
                                        <input type="radio" name="wiz_ai_model" value="gemini" class="absolute right-4 top-4 text-blue-500 bg-zinc-800 border-zinc-700" __GEMINI_CHECKED__>
                                        <i class="fa-brands fa-google text-2xl text-emerald-400 mb-3 group-hover:scale-110 transition"></i>
                                        <span class="text-white font-bold text-sm">Google Gemini</span>
                                        <span class="text-zinc-500 text-xs mt-1">Flash 2.5 model. Excellent vision analysis.</span>
                                    </label>
                                    <label class="flex flex-col p-4 rounded-xl border border-white/10 bg-[#0d1117] cursor-pointer hover:border-blue-500 transition group relative overflow-hidden">
                                        <input type="radio" name="wiz_ai_model" value="sarvam" class="absolute right-4 top-4 text-blue-500 bg-zinc-800 border-zinc-700" __SARVAM_CHECKED__>
                                        <i class="fa-solid fa-language text-2xl text-orange-400 mb-3 group-hover:scale-110 transition"></i>
                                        <span class="text-white font-bold text-sm">Sarvam AI</span>
                                        <span class="text-zinc-500 text-xs mt-1">Text-only multilingual processing.</span>
                                    </label>
                                </div>
                            </div>

                            <div id="step2" class="step-content">
                                <div class="flex items-center gap-4 mb-6">
                                    <div class="w-12 h-12 rounded-xl bg-red-500/10 flex items-center justify-center border border-red-500/20">
                                        <i class="fa-solid fa-hammer text-red-500 text-xl"></i>
                                    </div>
                                    <div>
                                        <h3 class="text-white font-bold text-lg">Automated Moderation</h3>
                                        <p class="text-zinc-400 text-sm">Protect your server from harmful content.</p>
                                    </div>
                                </div>
                                <div class="bg-[#0d1117] p-5 rounded-xl border border-white/5 flex justify-between items-center mb-4">
                                    <div>
                                        <span class="text-white font-bold block">Enable AI Auto Mod</span>
                                        <span class="text-xs text-zinc-500">Allows Recluse to delete messages containing blacklisted terms.</span>
                                    </div>
                                    <div class="relative inline-block w-12 align-middle select-none transition duration-200 ease-in ml-4">
                                        <input type="checkbox" id="wiz_mod" __MOD_CHECKED__ class="toggle-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                        <label class="toggle-label block overflow-hidden h-6 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                    </div>
                                </div>
                            </div>

                            <div id="step3" class="step-content">
                                <p class="text-zinc-400 text-sm mb-6">Toggle the utility modules you want active.</p>
                                <div class="space-y-3">
                                    <div class="bg-[#0d1117] p-4 rounded-xl border border-white/5 flex justify-between items-center">
                                        <div>
                                            <span class="text-white font-bold text-sm flex items-center gap-2"><i class="fa-solid fa-tv text-pink-400"></i> Anime API</span>
                                        </div>
                                        <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in ml-4">
                                            <input type="checkbox" id="wiz_anime" __ANIME_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                            <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                        </div>
                                    </div>
                                    <div class="bg-[#0d1117] p-4 rounded-xl border border-white/5 flex justify-between items-center">
                                        <div>
                                            <span class="text-white font-bold text-sm flex items-center gap-2"><i class="fa-solid fa-baseball-bat-ball text-orange-400"></i> Live Sports</span>
                                        </div>
                                        <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in ml-4">
                                            <input type="checkbox" id="wiz_sports" __SPORTS_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                            <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                        </div>
                                    </div>
                                    <div class="bg-[#0d1117] p-4 rounded-xl border border-white/5 flex justify-between items-center">
                                        <div>
                                            <span class="text-white font-bold text-sm flex items-center gap-2"><i class="fa-solid fa-box-open text-teal-400"></i> Miscellaneous</span>
                                        </div>
                                        <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in ml-4">
                                            <input type="checkbox" id="wiz_misc" __MISC_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                            <label class="toggle-label block overflow-hidden h-5 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <div id="step4" class="step-content text-center py-8">
                                <div class="w-20 h-20 mx-auto bg-emerald-500/10 border border-emerald-500/20 rounded-full flex items-center justify-center mb-4">
                                    <i class="fa-solid fa-check text-4xl text-emerald-400"></i>
                                </div>
                                <h2 class="text-2xl font-bold text-white mb-2">Ready for Deployment</h2>
                                <p class="text-zinc-400 max-w-sm mx-auto">Your configuration is ready. Click the deploy button below to push these settings.</p>
                            </div>
                        </div>

                        <div class="bg-[#12161f] border-t border-white/5 p-4 flex justify-between items-center">
                            <button id="prevBtn" class="px-5 py-2 rounded-lg text-zinc-400 hover:text-white font-medium transition invisible" onclick="changeStep(-1)">Back</button>
                            <button id="nextBtn" class="px-6 py-2 rounded-lg bg-blue-500 hover:bg-blue-600 shadow-lg text-white font-bold transition" onclick="changeStep(1)">Next Step</button>
                        </div>
                    </div>
                </div>
            </main>

            <script>
                let currentStep = 1;
                const totalSteps = 4;
                const titles = ["Select AI Core", "Network Security", "Utility Modules", "Finalize Configuration"];

                function updateUI() {
                    document.querySelectorAll('.step-content').forEach((el, index) => {
                        if (index + 1 === currentStep) el.classList.add('active');
                        else el.classList.remove('active');
                    });
                    document.getElementById('stepIndicatorText').innerText = `STEP ${currentStep} OF ${totalSteps}`;
                    document.getElementById('stepTitle').innerText = titles[currentStep - 1];
                    document.getElementById('progressBar').style.width = `${(currentStep / totalSteps) * 100}%`;

                    const prevBtn = document.getElementById('prevBtn');
                    const nextBtn = document.getElementById('nextBtn');
                    if (currentStep === 1) prevBtn.classList.add('invisible');
                    else prevBtn.classList.remove('invisible');

                    if (currentStep === totalSteps) {
                        nextBtn.innerHTML = '<i class="fa-solid fa-rocket mr-2"></i> Deploy Settings';
                        nextBtn.classList.remove('bg-blue-500', 'hover:bg-blue-600');
                        nextBtn.classList.add('bg-emerald-500', 'hover:bg-emerald-600', 'shadow-emerald-500/20');
                    } else {
                        nextBtn.innerText = 'Next Step';
                        nextBtn.classList.add('bg-blue-500', 'hover:bg-blue-600');
                        nextBtn.classList.remove('bg-emerald-500', 'hover:bg-emerald-600', 'shadow-emerald-500/20');
                    }
                }

                async function changeStep(direction) {
                    if (direction === 1 && currentStep === totalSteps) { await finalizeSetup(); return; }
                    currentStep += direction;
                    if (currentStep < 1) currentStep = 1;
                    if (currentStep > totalSteps) currentStep = totalSteps;
                    updateUI();
                }

                async function finalizeSetup() {
                    const nextBtn = document.getElementById('nextBtn');
                    nextBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Deploying...';
                    nextBtn.disabled = true;

                    const payload = {
                        action: 'bulk_update',
                        settings: {
                            default_ai_model: document.querySelector('input[name="wiz_ai_model"]:checked').value,
                            automod_enabled: document.getElementById('wiz_mod').checked,
                            anime_enabled: document.getElementById('wiz_anime').checked,
                            sports_enabled: document.getElementById('wiz_sports').checked,
                            misc_enabled: document.getElementById('wiz_misc').checked
                        }
                    };

                    try {
                        const response = await fetch(`/api/settings/__GUILD_ID__`, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
                        });
                        if (response.ok) window.location.href = `/manage/__GUILD_ID__`;
                        else throw new Error('Failed to save');
                    } catch (e) {
                        alert("Error saving configuration.");
                        nextBtn.innerHTML = '<i class="fa-solid fa-rocket mr-2"></i> Deploy Settings';
                        nextBtn.disabled = false;
                    }
                }
                
                document.querySelectorAll('.toggle-checkbox').forEach(toggle => {
                    const updateVisuals = (element) => {
                        const label = element.nextElementSibling;
                        if(element.checked) {
                            element.style.left = 'auto'; element.style.right = '0';
                            element.style.borderColor = '#3b82f6'; label.style.backgroundColor = '#3b82f6';
                            label.style.boxShadow = '0 0 10px rgba(59, 130, 246, 0.5)';
                        } else {
                            element.style.right = 'auto'; element.style.left = '0';
                            element.style.borderColor = '#52525b'; label.style.backgroundColor = '#52525b';
                            label.style.boxShadow = 'none';
                        }
                    };
                    updateVisuals(toggle);
                    toggle.addEventListener('change', function() { updateVisuals(this); });
                });
            </script>
        </body>
        </html>
        """
        wizard_html = wizard_html.replace("__GUILD_NAME__", str(guild.name))
        wizard_html = wizard_html.replace("__GUILD_ICON__", str(guild_icon))
        wizard_html = wizard_html.replace("__BOT_AVATAR__", str(bot_avatar))
        wizard_html = wizard_html.replace("__GUILD_ID__", str(guild_id_int))
        wizard_html = wizard_html.replace("__USER_NAME__", str(user_name))
        wizard_html = wizard_html.replace("__USER_AVATAR__", str(user_avatar))
        wizard_html = wizard_html.replace("__NEXUSIFY_CHECKED__", nexusify_checked)
        wizard_html = wizard_html.replace("__GEMINI_CHECKED__", gemini_checked)
        wizard_html = wizard_html.replace("__SARVAM_CHECKED__", sarvam_checked)
        wizard_html = wizard_html.replace("__MOD_CHECKED__", mod_checked)
        wizard_html = wizard_html.replace("__ANIME_CHECKED__", anime_checked)
        wizard_html = wizard_html.replace("__SPORTS_CHECKED__", sports_checked)
        wizard_html = wizard_html.replace("__MISC_CHECKED__", misc_checked)
        return web.Response(text=wizard_html, content_type='text/html')

    async def server_lockdown(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        try: guild_id_int = int(guild_id)
        except ValueError: return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        lockdown_active = False
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings: lockdown_active = settings.get("lockdown_active", False)

        bot_avatar = self.get_bot_avatar()
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"

        status_color = "text-red-500" if lockdown_active else "text-emerald-500"
        status_text = "LOCKED DOWN" if lockdown_active else "SECURE"
        status_bg = "bg-red-500/10 border-red-500/20" if lockdown_active else "bg-emerald-500/10 border-emerald-500/20"
        status_icon = "fa-lock" if lockdown_active else "fa-shield-check"
        btn_text = "Lift Lockdown" if lockdown_active else "Engage Emergency Lockdown"
        btn_class = "bg-emerald-500 hover:bg-emerald-600 shadow-emerald-500/20" if lockdown_active else "bg-red-600 hover:bg-red-700 shadow-red-500/20"

        lockdown_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{guild.name} | Lockdown</title>
            <link rel="icon" type="image/png" href="{bot_avatar}">
            <link rel="shortcut icon" href="{bot_avatar}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body {{ background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }}
                .glass-panel {{ background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }}
                .sidebar-link {{ transition: all 0.2s; }}
                .sidebar-link.active {{ background-color: #3b82f6; color: white; border-radius: 0.5rem; }}
                .sidebar-link:hover:not(.active) {{ background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }}
            </style>
        </head>
        <body class="text-zinc-300 font-sans h-screen flex overflow-hidden selection:bg-blue-500 selection:text-white">

            <aside class="w-64 bg-[#0d1117] border-r border-white/5 flex flex-col hidden md:flex flex-shrink-0 z-20 shadow-2xl">
                <div class="p-4 border-b border-white/5 relative group cursor-pointer hover:bg-white/5 transition">
                    <div class="flex items-center gap-3">
                        <img src="{guild_icon}" alt="Server" class="w-10 h-10 rounded-full shadow-lg">
                        <div class="overflow-hidden">
                            <h2 class="text-white font-bold truncate text-sm">{guild.name}</h2>
                            <p class="text-[10px] text-zinc-500 font-mono">{guild_id_int}</p>
                        </div>
                    </div>
                </div>

                <nav class="flex-1 overflow-y-auto p-3 space-y-1 mt-2 custom-scrollbar">
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-4">Main Menu</p>
                    <a href="/manage/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="/wizard/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="/misc/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="/lockdown/{guild_id_int}" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-white">
                        <i class="fa-solid fa-lock w-5 text-center"></i> Lockdown
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
                
                <div class="p-4 border-t border-white/5">
                    <a href="/" class="flex items-center gap-3 text-sm text-zinc-400 hover:text-white transition">
                        <i class="fa-solid fa-arrow-left"></i> Back to Servers
                    </a>
                </div>
            </aside>

            <main class="flex-1 flex flex-col h-screen overflow-hidden relative">
                <header class="h-16 border-b border-white/5 bg-[#090b10]/80 backdrop-blur flex items-center justify-between px-6 z-10 shrink-0">
                    <div class="hidden md:block text-sm font-bold text-zinc-400 tracking-widest uppercase">Emergency Protocols</div> 
                    <div class="flex items-center gap-4">
                        <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 p-1.5 rounded-lg transition">
                            <span class="text-xs font-medium text-white">{user_name}</span>
                            <img src="{user_avatar}" alt="User" class="w-7 h-7 rounded-full">
                        </div>
                    </div>
                </header>

                <div class="flex-1 overflow-y-auto p-6 lg:p-10 pb-20 flex items-center justify-center">
                    <div class="max-w-2xl w-full text-center">
                        <div class="mb-8">
                            <h1 class="text-4xl font-extrabold text-white mb-2"><i class="fa-solid fa-triangle-exclamation text-red-500 mr-2"></i> Server Lockdown</h1>
                            <p class="text-zinc-400">Instantly halt raids by severing messaging capabilities across the network.</p>
                        </div>

                        <div class="glass-panel p-10 rounded-2xl shadow-2xl border border-white/10 relative overflow-hidden">
                            <div class="absolute inset-0 bg-[url('https://www.transparenttextures.com/patterns/carbon-fibre.png')] opacity-10"></div>
                            
                            <div class="relative z-10">
                                <div class="w-24 h-24 mx-auto rounded-full border-4 {status_bg} {status_color} flex items-center justify-center mb-6 shadow-lg shadow-black/50">
                                    <i class="fa-solid {status_icon} text-4xl"></i>
                                </div>
                                
                                <h2 class="text-zinc-500 text-sm font-bold tracking-widest uppercase mb-1">Current Status</h2>
                                <p class="text-3xl font-black {status_color} tracking-wider mb-8">{status_text}</p>

                                <div class="bg-[#0d1117] border border-white/5 rounded-xl p-5 mb-8 text-left">
                                    <h3 class="text-white font-bold text-sm mb-2"><i class="fa-solid fa-circle-info text-blue-400 mr-1"></i> Protocol Details</h3>
                                    <p class="text-xs text-zinc-400 leading-relaxed">
                                        Engaging a lockdown will physically edit your server's <code>@everyone</code> role, instantly revoking the <code>Send Messages</code> permission. This halts all unprivileged chat activity while your moderation team handles the ongoing threat. Ensure Recluse has the <b>Manage Roles</b> permission.
                                    </p>
                                </div>

                                <button id="lockdownBtn" onclick="toggleLockdown({str(not lockdown_active).lower()})" class="w-full py-4 rounded-xl {btn_class} shadow-lg text-white font-bold text-lg transition flex items-center justify-center gap-3">
                                    <i class="fa-solid fa-power-off"></i> {btn_text}
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </main>

            <script>
                async function toggleLockdown(targetState) {{
                    if (targetState && !confirm("WARNING: This will revoke Send Messages from @everyone. Are you sure you want to engage lockdown?")) return;
                    
                    const btn = document.getElementById('lockdownBtn');
                    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Processing...';
                    btn.disabled = true;

                    try {{
                        const res = await fetch(`/api/settings/{guild_id_int}`, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ action: 'toggle_lockdown', state: targetState }})
                        }});
                        
                        const data = await res.json();
                        if (res.ok && data.success) {{
                            window.location.reload();
                        }} else {{
                            throw new Error(data.error || "Failed to execute");
                        }}
                    }} catch (e) {{
                        alert("Error: " + e.message);
                        window.location.reload();
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=lockdown_html, content_type='text/html')

    async def misc_settings(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        try: guild_id_int = int(guild_id)
        except ValueError: return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        misc_enabled = True
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings: misc_enabled = settings.get("misc_enabled", True)

        misc_checked = "checked" if misc_enabled else ""
        bot_avatar = self.get_bot_avatar()
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"
        
        misc_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{guild.name} | Miscellaneous</title>
            <link rel="icon" type="image/png" href="{bot_avatar}">
            <link rel="shortcut icon" href="{bot_avatar}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body {{ background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }}
                .glass-panel {{ background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }}
                .sidebar-link {{ transition: all 0.2s; }}
                .sidebar-link.active {{ background-color: #3b82f6; color: white; border-radius: 0.5rem; }}
                .sidebar-link:hover:not(.active) {{ background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }}
                .toggle-checkbox:checked {{ right: 0; border-color: #3b82f6; }}
                .toggle-checkbox:checked + .toggle-label {{ background-color: #3b82f6; box-shadow: 0 0 10px rgba(59, 130, 246, 0.5); }}
            </style>
        </head>
        <body class="text-zinc-300 font-sans h-screen flex overflow-hidden selection:bg-blue-500 selection:text-white">

            <aside class="w-64 bg-[#0d1117] border-r border-white/5 flex flex-col hidden md:flex flex-shrink-0 z-20 shadow-2xl">
                <div class="p-4 border-b border-white/5 relative group cursor-pointer hover:bg-white/5 transition">
                    <div class="flex items-center gap-3">
                        <img src="{guild_icon}" alt="Server" class="w-10 h-10 rounded-full shadow-lg">
                        <div class="overflow-hidden">
                            <h2 class="text-white font-bold truncate text-sm">{guild.name}</h2>
                            <p class="text-[10px] text-zinc-500 font-mono">{guild_id_int}</p>
                        </div>
                    </div>
                </div>

                <nav class="flex-1 overflow-y-auto p-3 space-y-1 mt-2 custom-scrollbar">
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-4">Main Menu</p>
                    <a href="/manage/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="/wizard/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="/misc/{guild_id_int}" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-white">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="/lockdown/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-lock w-5 text-center"></i> Lockdown
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/{guild_id_int}" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
                
                <div class="p-4 border-t border-white/5">
                    <a href="/" class="flex items-center gap-3 text-sm text-zinc-400 hover:text-white transition">
                        <i class="fa-solid fa-arrow-left"></i> Back to Servers
                    </a>
                </div>
            </aside>

            <main class="flex-1 flex flex-col h-screen overflow-hidden relative">
                <header class="h-16 border-b border-white/5 bg-[#090b10]/80 backdrop-blur flex items-center justify-between px-6 z-10 shrink-0">
                    <div class="hidden md:block text-sm font-bold text-zinc-400 tracking-widest uppercase">Utility Configuration</div> 
                    <div class="flex items-center gap-4">
                        <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 p-1.5 rounded-lg transition">
                            <span class="text-xs font-medium text-white">{user_name}</span>
                            <img src="{user_avatar}" alt="User" class="w-7 h-7 rounded-full">
                        </div>
                    </div>
                </header>

                <div class="flex-1 overflow-y-auto p-6 lg:p-10 pb-20">
                    <div class="mb-8 max-w-4xl mx-auto">
                        <h1 class="text-3xl font-extrabold text-white mb-2"><i class="fa-solid fa-box-open text-teal-400 mr-2"></i> Miscellaneous Utilities</h1>
                        <p class="text-zinc-400 text-sm">Manage quality-of-life commands and server information tools.</p>
                    </div>

                    <div class="max-w-4xl mx-auto glass-panel p-8 rounded-2xl border border-white/5">
                        <div class="flex justify-between items-start mb-8 border-b border-white/5 pb-6">
                            <div>
                                <h3 class="text-white font-bold text-lg flex items-center gap-2">Global Module Status</h3>
                                <p class="text-xs text-zinc-400 mt-1">Disabling this completely turns off commands like /ping, /afk, /serverinfo, and /whois.</p>
                            </div>
                            <div class="relative inline-block w-12 align-middle select-none transition duration-200 ease-in ml-4">
                                <input type="checkbox" id="toggleMisc" {misc_checked} class="toggle-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-blue-500"/>
                                <label class="toggle-label block overflow-hidden h-6 rounded-full bg-blue-500 cursor-pointer transition-colors duration-300"></label>
                            </div>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div class="bg-[#0d1117] p-5 rounded-xl border border-white/5">
                                <i class="fa-solid fa-bed text-xl text-blue-400 mb-3"></i>
                                <h4 class="text-white font-bold text-sm mb-1">Dynamic AFK System</h4>
                                <p class="text-xs text-zinc-500">Allows users to use <code>/afk</code>. Recluse will automatically notify users who mention them and welcome them back upon their return.</p>
                            </div>
                            <div class="bg-[#0d1117] p-5 rounded-xl border border-white/5">
                                <i class="fa-solid fa-magnifying-glass-chart text-xl text-emerald-400 mb-3"></i>
                                <h4 class="text-white font-bold text-sm mb-1">Security Dossiers</h4>
                                <p class="text-xs text-zinc-500">Enables the highly detailed <code>/whois</code> and <code>/serverinfo</code> commands for user auditing and investigation.</p>
                            </div>
                        </div>
                    </div>
                </div>
            </main>

            <script>
                document.getElementById('toggleMisc').addEventListener('change', async function() {{
                    const label = this.nextElementSibling;
                    if(this.checked) {{
                        this.style.left = 'auto'; this.style.right = '0';
                        this.style.borderColor = '#3b82f6'; label.style.backgroundColor = '#3b82f6';
                        label.style.boxShadow = '0 0 10px rgba(59, 130, 246, 0.5)';
                    }} else {{
                        this.style.right = 'auto'; this.style.left = '0';
                        this.style.borderColor = '#52525b'; label.style.backgroundColor = '#52525b';
                        label.style.boxShadow = 'none';
                    }}
                    
                    try {{
                        const response = await fetch(`/api/settings/{guild_id_int}`, {{
                            method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ action: 'toggle', module: 'toggleMisc', enabled: this.checked }})
                        }});
                    }} catch (error) {{ console.error(error); }}
                }});
            </script>
        </body>
        </html>
        """
        return web.Response(text=misc_html, content_type='text/html')

    async def start_server(self):
        port = int(os.getenv("PORT", 8080))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        self.site = web.TCPSite(self.runner, '0.0.0.0', port)
        try:
            await self.site.start()
            print(f"🌐 Web dashboard successfully started on port {port}!")
        except Exception as e:
            print(f"❌ Failed to start web server: {e}")
            
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())

async def setup(bot):
    await bot.add_cog(Dashboard(bot))
