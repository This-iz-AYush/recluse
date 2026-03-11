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
        
        # Initialize aiohttp Application with our custom IP Blocker & Verification Middleware
        self.app = web.Application(middlewares=[self.security_middleware])
        
        # Registering our dynamic web routes
        self.app.add_routes([
            web.get('/', self.home),
            web.get('/login', self.login),
            web.get('/callback', self.callback),
            web.get('/logout', self.logout),
            web.get('/manage/{guild_id}', self.manage_server),
            web.get('/logs/{guild_id}', self.server_logs),
            web.get('/automod/{guild_id}', self.auto_mod), # <-- ADD THIS LINE
            web.get('/owner_panel', self.owner_panel),
            web.post('/api/settings/{guild_id}', self.update_settings),
            web.post('/api/ip_action', self.handle_ip_action),
            web.post('/api/verify_visitor', self.verify_visitor)
        ])
        
        self.runner = None
        self.site = None
        self.bot.loop.create_task(self.start_server())

    @web.middleware
    async def security_middleware(self, request, handler):
        """Intercepts traffic for IP logging, ban enforcement, and Anti-Bot Verification."""
        raw_ip = request.headers.get('X-Forwarded-For', request.remote)
        ip = raw_ip.split(',')[0].strip() if raw_ip else 'Unknown'
        request['visitor_ip'] = ip

        if hasattr(self.bot, 'db') and ip != 'Unknown':
            is_banned = await self.bot.db.ip_bans.find_one({"ip": ip})
            if is_banned:
                return web.Response(text="403 Forbidden: Your IP address has been permanently restricted from accessing this network.", status=403)

        if request.path == '/api/verify_visitor':
            return await handler(request)

        is_verified = request.cookies.get("recluse_verified")
        if not is_verified:
            ray_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
            
            verify_html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Recluse.OS | Security Verification</title>
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

    async def home(self, request):
        user_session = await self.get_user_session(request)
        
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

        bot_name = self.bot.user.name if self.bot.user else "Recluse"
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"

        app_info = await self.bot.application_info()
        is_owner = int(user_session['discord_id']) == app_info.owner.id
        
        owner_button_html = ""
        if is_owner:
            owner_button_html = """
            <a href="/owner_panel" class="text-xs bg-red-500/10 text-red-400 hover:bg-red-500/20 hover:text-red-300 font-medium px-3 py-2 rounded-lg border border-red-500/20 transition flex items-center gap-2">
                <i class="fa-solid fa-terminal"></i> Owner Panel
            </a>
            """

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
        client_id = os.getenv("DISCORD_CLIENT_ID", "")
        
        guild_cards_html = ""
        if not admin_guilds:
            guild_cards_html = """
            <div class="col-span-full p-8 text-center glass-panel rounded-2xl border border-white/5">
                <i class="fa-solid fa-server text-4xl text-zinc-600 mb-4"></i>
                <h3 class="text-xl font-bold text-white mb-2">No Servers Found</h3>
                <p class="text-zinc-400">You do not have Administrator or Manage Server permissions in any Discord servers.</p>
            </div>
            """
        else:
            for g in admin_guilds:
                is_in_server = int(g['id']) in bot_guild_ids
                icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get('icon') else f"https://ui-avatars.com/api/?name={urllib.parse.quote(g['name'])}&background=27272a&color=fff"
                
                if is_in_server:
                    action_button = f'<a href="/manage/{g["id"]}" class="px-5 py-2.5 bg-violet-500 hover:bg-violet-600 shadow-lg shadow-violet-500/20 transition-all rounded-xl text-sm text-white font-semibold whitespace-nowrap">Manage Server</a>'
                else:
                    invite_url = f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot"
                    action_button = f'<a href="{invite_url}" target="_blank" class="px-5 py-2.5 bg-zinc-800 hover:bg-zinc-700 transition-all rounded-xl text-sm text-white font-semibold whitespace-nowrap border border-white/10">Invite Bot</a>'

                guild_cards_html += f"""
                <div class="glass-panel p-5 rounded-2xl flex flex-col sm:flex-row items-center justify-between gap-4 border border-white/5 hover:border-violet-500/30 transition-colors group">
                    <div class="flex items-center gap-4 text-center sm:text-left">
                        <img src="{icon_url}" alt="{g['name']}" class="w-14 h-14 rounded-full shadow-md">
                        <div>
                            <h3 class="font-bold text-white text-lg">{g['name']}</h3>
                            <p class="text-xs text-zinc-500">ID: {g['id']}</p>
                        </div>
                    </div>
                    {action_button}
                </div>
                """

        dashboard_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__BOT_NAME__ | Select Server</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                .glass-panel { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); }
            </style>
        </head>
        <body class="bg-[#09090b] text-zinc-300 font-sans min-h-screen flex flex-col selection:bg-violet-500 selection:text-white">

            <nav class="glass-panel sticky top-0 z-50 px-6 py-4 flex justify-between items-center border-b border-white/5">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500 to-pink-500 flex items-center justify-center shadow-lg shadow-violet-500/20">
                        <i class="fa-solid fa-spider text-white text-lg"></i>
                    </div>
                    <span class="text-xl font-bold text-white tracking-wide">__BOT_NAME__<span class="font-light text-zinc-500">.OS</span></span>
                </div>
                
                <div class="flex items-center gap-4">
                    __OWNER_BUTTON__
                    <div class="flex items-center gap-3 bg-white/5 py-1.5 px-3 rounded-full border border-white/5">
                        <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        <span class="text-sm font-medium text-white">__USER_NAME__</span>
                    </div>
                    <a href="/logout" class="text-xs text-red-400 hover:text-red-300 font-medium px-3 py-2 rounded-lg hover:bg-red-400/10 transition">Logout</a>
                </div>
            </nav>

            <main class="flex-1 max-w-4xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8">
                <div>
                    <h1 class="text-3xl font-extrabold text-white mb-2">Select a Server</h1>
                    <p class="text-zinc-400">Choose a server below to configure __BOT_NAME__'s modules and settings.</p>
                </div>

                <div class="grid grid-cols-1 gap-4">
                    __GUILD_CARDS__
                </div>
            </main>
        </body>
        </html>
        """
        
        dashboard_html = dashboard_html.replace("__BOT_NAME__", str(bot_name))
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

        bot_name = self.bot.user.name if self.bot.user else "Recluse"
        server_count = len(self.bot.guilds)
        member_count = sum(g.member_count for g in self.bot.guilds if g.member_count)
        loaded_cogs = ", ".join(self.bot.cogs.keys())

        visitor_html = ""
        banned_html = ""
        
        if hasattr(self.bot, 'db'):
            # Fetch Visitors
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

            # Fetch Banned IPs (These already have auto-unban buttons!)
            bans_cursor = self.bot.db.ip_bans.find().limit(50)
            async for b in bans_cursor:
                banned_html += f"""
                <div class="flex justify-between items-center p-3 border-b border-red-500/10 hover:bg-red-500/5 transition">
                    <span class="font-mono text-red-400 text-sm">{b['ip']}</span>
                    <button onclick="submitIPAction('unban', '{b['ip']}')" class="text-xs bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500 hover:text-white px-2 py-1 rounded border border-emerald-500/20 transition">Unban</button>
                </div>"""
                
            if not banned_html: banned_html = "<p class='text-zinc-500 text-sm p-3'>No IPs are currently banned.</p>"

        owner_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{bot_name} | Developer Override</title>
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

            <main class="flex-1 max-w-6xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8">
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

                <div class="glass-panel p-6 rounded-2xl border border-white/5">
                    <h2 class="text-xl font-bold text-white mb-6 flex items-center gap-2">
                        <i class="fa-solid fa-shield text-red-500"></i> Network Firewall (IP Access)
                    </h2>
                    
                    <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                        <div>
                            <h3 class="text-zinc-300 font-medium mb-3">All Logged Visitors</h3>
                            <div class="bg-[#18181b] rounded-xl border border-white/10 overflow-y-auto max-h-[400px]">
                                {visitor_html}
                            </div>
                        </div>
                        
                        <div>
                            <h3 class="text-red-400 font-medium mb-3">Banned IP Addresses</h3>
                            <div class="bg-red-500/5 rounded-xl border border-red-500/20 overflow-y-auto max-h-[300px] mb-4">
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

                <div class="glass-panel rounded-2xl p-6 border border-white/5 relative overflow-hidden">
                    <h2 class="text-xl font-bold text-white mb-4">Loaded Core Extensions</h2>
                    <p class="text-zinc-400 font-mono text-sm mb-6 bg-black/40 p-4 rounded-xl border border-white/5">{loaded_cogs}</p>
                </div>
            </main>
            
            <script>
                async function submitIPAction(action, ip) {{
                    if(!ip) return alert("Please provide an IP address.");
                    if(action === 'ban' && !confirm(`Are you sure you want to permanently IP ban ${{ip}}?`)) return;
                    // Added unban confirmation
                    if(action === 'unban' && !confirm(`Are you sure you want to unban the IP: ${{ip}}?`)) return;
                    
                    try {{
                        const res = await fetch('/api/ip_action', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ action: action, ip: ip.trim() }})
                        }});
                        
                        if(res.ok) window.location.reload();
                        else alert("Failed to execute action.");
                    }} catch (e) {{
                        console.error(e);
                        alert("Network error.");
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=owner_html, content_type='text/html')
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

        bot_name = self.bot.user.name if self.bot.user else "Recluse"
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
                    <a href="#" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium">
                        <i class="fa-solid fa-chart-pie w-5 text-center"></i> Overview
                    </a>
                    <a href="#" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-wand-magic-sparkles w-5 text-center"></i> Wizard Setup
                    </a>
                    <a href="#" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-box-open w-5 text-center"></i> Miscellaneous
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    <a href="#" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
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
                    <div class="hidden md:block"></div> <div class="flex items-center gap-4">
                        <button class="text-zinc-400 hover:text-white transition"><i class="fa-solid fa-bell"></i></button>
                        <div class="h-5 w-px bg-white/10"></div>
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
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Shard ID <i class="fa-regular fa-copy cursor-pointer hover:text-white"></i></p>
                                    <p class="text-white text-sm font-medium">0</p>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Type</p>
                                    <span class="bg-blue-500 text-white text-[10px] font-bold px-2 py-0.5 rounded shadow-lg shadow-blue-500/20">STANDARD</span>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1 flex items-center gap-2">Members <i class="fa-regular fa-copy cursor-pointer hover:text-white"></i></p>
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
                                <button onclick="openModal('modModal')" class="w-full text-xs bg-white/5 hover:bg-white/10 text-white font-medium py-2 rounded-lg transition border border-white/5"><i class="fa-solid fa-filter"></i> Edit Filters</button>
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
                    <p class="text-zinc-400 text-sm mb-4">Enter words or phrases that should be automatically deleted. Separate each word with a comma.</p>
                    
                    <div class="mb-6">
                        <textarea id="banned_words_input" rows="4" class="w-full bg-[#0d1117] border border-white/10 rounded-xl p-3 text-white focus:outline-none focus:border-blue-500 transition resize-none placeholder-zinc-600">__BANNED_WORDS__</textarea>
                    </div>
                    <div class="flex justify-end gap-3">
                        <button onclick="closeModal('modModal')" class="px-4 py-2 rounded-xl text-zinc-400 hover:text-white font-medium transition">Cancel</button>
                        <button id="saveModBtn" onclick="saveAutomod()" class="px-5 py-2 rounded-xl bg-blue-500 hover:bg-blue-600 shadow-lg text-white font-semibold transition">Save Rules</button>
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

                async function saveAutomod() {
                    const words = document.getElementById('banned_words_input').value;
                    const btn = document.getElementById('saveModBtn'); btn.innerText = 'Saving...';
                    await fetch(`/api/settings/__GUILD_ID__`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'update_automod', words: words }) });
                    closeModal('modModal'); btn.innerText = 'Save Rules';
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
        manage_html = manage_html.replace("__BANNED_WORDS__", banned_words_str)
        
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
            
            return web.json_response({"error": "Invalid action"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

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
        
    async def server_logs(self, request):
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
            return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        # --- FETCH LOGS FROM MONGODB ---
        mod_logs_html = ""
        security_logs_html = ""
        
        if hasattr(self.bot, 'db'):
            # Fetch Moderation Logs (Bans, Kicks, Warns)
            mod_cursor = self.bot.db.mod_logs.find({"guild_id": guild_id_int}).sort("timestamp", -1).limit(50)
            async for log in mod_cursor:
                action = log.get('action', 'Unknown')
                
                # Dynamic styling based on action
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

            # Fetch Security Logs (Automod AI Filters)
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

        # UI Replacements
        bot_name = self.bot.user.name if self.bot.user else "Recluse"
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
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }
                .glass-panel { background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }
                .sidebar-link { transition: all 0.2s; }
                .sidebar-link.active { background-color: #3b82f6; color: white; border-radius: 0.5rem; }
                .sidebar-link:hover:not(.active) { background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }
                /* Custom Scrollbar for tables */
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
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/__GUILD_ID__" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
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
        logs_html = logs_html.replace("__GUILD_ID__", str(guild_id_int))
        logs_html = logs_html.replace("__USER_NAME__", str(user_name))
        logs_html = logs_html.replace("__USER_AVATAR__", str(user_avatar))
        logs_html = logs_html.replace("__MOD_LOGS__", mod_logs_html)
        logs_html = logs_html.replace("__SECURITY_LOGS__", security_logs_html)
        
        return web.Response(text=logs_html, content_type='text/html')

    async def auto_mod(self, request):
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
            return web.Response(text="Recluse is not in this server.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        # --- FETCH CURRENT SETTINGS ---
        banned_words = ["unauthorized_term_1", "prohibited_phrase", "blacklisted_word"] # Defaults
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings and "banned_words" in settings:
                banned_words = settings["banned_words"]

        banned_words_raw = ", ".join(banned_words)
        
        # Generate the HTML pills for the UI
        pills_html = ""
        if not banned_words or (len(banned_words) == 1 and banned_words[0] == ""):
            pills_html = "<p class='text-zinc-500 text-sm'>No words currently blacklisted. The AI will not filter any specific terms.</p>"
        else:
            for word in banned_words:
                if word.strip():
                    pills_html += f'<span class="px-3 py-1.5 bg-red-500/10 border border-red-500/20 text-red-400 text-xs font-mono rounded-md shadow-sm">{word}</span>\n'

        # UI Replacements
        bot_name = self.bot.user.name if self.bot.user else "Recluse"
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
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                body { background: radial-gradient(circle at top right, #111827, #0f1219, #09090b); background-attachment: fixed; }
                .glass-panel { background: #161b22; border: 1px solid rgba(255, 255, 255, 0.05); }
                .sidebar-link { transition: all 0.2s; }
                .sidebar-link.active { background-color: #3b82f6; color: white; border-radius: 0.5rem; }
                .sidebar-link:hover:not(.active) { background-color: rgba(255,255,255,0.05); color: white; border-radius: 0.5rem; }
                .custom-scrollbar::-webkit-scrollbar { width: 6px; }
                .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
                .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 10px; }
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
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">Security</p>
                    <a href="/automod/__GUILD_ID__" class="sidebar-link active flex items-center gap-3 px-3 py-2.5 text-sm font-medium">
                        <i class="fa-solid fa-shield-halved w-5 text-center"></i> Auto Mod Rules
                    </a>
                    
                    <p class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest pl-3 mb-2 mt-6">System</p>
                    <a href="/logs/__GUILD_ID__" class="sidebar-link flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-zinc-400">
                        <i class="fa-solid fa-database w-5 text-center"></i> Logging
                    </a>
                </nav>
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
                                <p class="text-xs text-zinc-400 mb-4">Enter words or exact phrases you wish to ban. Separate each entry with a comma. The AI will enforce these globally across the server.</p>
                                
                                <textarea id="banned_words_input" rows="6" class="w-full bg-[#0d1117] border border-white/10 rounded-lg p-4 text-white text-sm font-mono focus:outline-none focus:border-blue-500 transition resize-none placeholder-zinc-700 shadow-inner" placeholder="e.g. term1, term2, forbidden phrase">__BANNED_WORDS_RAW__</textarea>
                                
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
                            
                            // Reload page after 1 second to update the visual pills
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
        automod_html = automod_html.replace("__GUILD_ID__", str(guild_id_int))
        automod_html = automod_html.replace("__USER_NAME__", str(user_name))
        automod_html = automod_html.replace("__USER_AVATAR__", str(user_avatar))
        automod_html = automod_html.replace("__BANNED_WORDS_RAW__", banned_words_raw)
        automod_html = automod_html.replace("__BANNED_WORDS_PILLS__", pills_html)
        
        return web.Response(text=automod_html, content_type='text/html')    

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
