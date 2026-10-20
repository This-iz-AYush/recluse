"""
cogs/dashboard.py — Recluse Web Dashboard
"""

import os
import uuid
import datetime
import urllib.parse
import json
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands


class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.dashboard_maintenance = False

        self.app = web.Application(middlewares=[self.security_middleware])

        self.app.add_routes([
            web.get('/', self.home),
            web.get('/login', self.login),
            web.get('/callback', self.callback),
            web.get('/logout', self.logout),
            web.get('/manage/{guild_id}', self.manage_server),
            web.get('/modules/{guild_id}', self.manage_modules),
            web.get('/logs/{guild_id}', self.server_logs),
            web.get('/automod/{guild_id}', self.auto_mod),
            web.get('/wizard/{guild_id}', self.wizard_setup),
            web.get('/misc/{guild_id}', self.misc_settings),
            web.get('/lockdown/{guild_id}', self.server_lockdown),
            web.get('/owner_panel', self.owner_panel),
            web.post('/api/settings/{guild_id}', self.update_settings),
            web.post('/api/modules/{guild_id}', self.update_modules),
            web.post('/api/ip_action', self.handle_ip_action),
            web.post('/api/verify_visitor', self.verify_visitor),
            web.post('/api/owner_action', self.handle_owner_action)
        ])

        self.runner = None
        self.site = None
        self.bot.loop.create_task(self.start_server())

    def get_bot_avatar(self):
        if self.bot and self.bot.user:
            return str(self.bot.user.display_avatar.url).replace(".webp", ".png")
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    def _get_ambient_bg(self):
        # Global animated mesh gradient for the deeper Glassmorphism aesthetic
        return """
        <div class="fixed inset-0 z-0 overflow-hidden pointer-events-none bg-[#0a0a0c]">
            <div class="absolute top-[-20%] left-[-10%] w-[600px] h-[600px] bg-purple-600/30 rounded-full mix-blend-screen filter blur-[150px] opacity-80 animate-pulse"></div>
            <div class="absolute top-[20%] right-[-10%] w-[500px] h-[500px] bg-blue-600/30 rounded-full mix-blend-screen filter blur-[140px] opacity-80 animate-pulse" style="animation-delay: 2s;"></div>
            <div class="absolute bottom-[-20%] left-[20%] w-[700px] h-[700px] bg-indigo-600/30 rounded-full mix-blend-screen filter blur-[140px] opacity-70 animate-pulse" style="animation-delay: 4s;"></div>
            <div class="absolute top-[40%] left-[40%] w-[400px] h-[400px] bg-teal-500/20 rounded-full mix-blend-screen filter blur-[120px] opacity-60 animate-pulse" style="animation-delay: 1s;"></div>
        </div>
        """

    # ─────────────────────────────────────────────────────────────────────────
    # SECURITY & TELEMETRY MIDDLEWARE
    # ─────────────────────────────────────────────────────────────────────────

    @web.middleware
    async def security_middleware(self, request, handler):
        raw_ip = request.headers.get('X-Forwarded-For', request.remote)
        ip = raw_ip.split(',')[0].strip() if raw_ip else 'Unknown'
        request['visitor_ip'] = ip

        if hasattr(self.bot, 'db') and ip != 'Unknown':
            is_banned = await self.bot.db.ip_bans.find_one({"ip": ip})
            if is_banned:
                return web.Response(text="403 Forbidden: Your IP address has been permanently restricted.", status=403)

        if getattr(self.bot, 'dashboard_maintenance', False):
            allowed_paths = ['/login', '/callback', '/logout', '/owner_panel', '/api/owner_action']
            if request.path not in allowed_paths:
                is_owner = False
                user_session = await self.get_user_session(request)
                if user_session:
                    owner_id = getattr(self.bot, 'owner_id', None)
                    if not owner_id:
                        app_info = await self.bot.application_info()
                        self.bot.owner_id = app_info.owner.id
                        owner_id = self.bot.owner_id
                    if int(user_session['discord_id']) == owner_id:
                        is_owner = True

                if not is_owner:
                    bot_avatar = self.get_bot_avatar()
                    maintenance_html = f"""
                    <!DOCTYPE html>
                    <html lang="en">
                    <head>
                        <meta charset="UTF-8">
                        <title>Recluse | Scheduled Maintenance</title>
                        <link rel="icon" type="image/png" href="{bot_avatar}">
                        <script src="https://cdn.tailwindcss.com"></script>
                        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
                    </head>
                    <body class="bg-transparent text-zinc-100 min-h-screen flex items-center justify-center p-6 relative">
                        {self._get_ambient_bg()}
                        <div class="z-10 max-w-md w-full bg-white/10 backdrop-blur-2xl border border-white/20 rounded-3xl p-10 text-center shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)]">
                            <div class="w-20 h-20 mx-auto rounded-3xl bg-amber-500/20 border border-amber-500/30 flex items-center justify-center mb-6 shadow-lg shadow-amber-500/10">
                                <i class="fa-solid fa-screwdriver-wrench text-amber-400 text-3xl"></i>
                            </div>
                            <h1 class="text-3xl font-bold text-white mb-3">Web Maintenance</h1>
                            <p class="text-zinc-300 text-sm mb-6 leading-relaxed">
                                The management console is currently undergoing maintenance. Discord bot functions and commands remain fully active.
                            </p>
                        </div>
                    </body>
                    </html>
                    """
                    return web.Response(text=maintenance_html, content_type='text/html', status=503)

        if request.path == '/api/verify_visitor':
            return await handler(request)

        is_verified = request.cookies.get("recluse_verified")
        if not is_verified:
            ray_id = str(uuid.uuid4())[:16]
            bot_avatar = self.get_bot_avatar()
            verify_html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Recluse | Security Verification</title>
                <link rel="icon" type="image/png" href="{bot_avatar}">
                <script src="https://cdn.tailwindcss.com"></script>
                <style>
                    .spinner {{
                        border: 2px solid rgba(255,255,255,0.1);
                        width: 24px; height: 24px;
                        border-radius: 50%;
                        border-left-color: #3b82f6;
                        animation: spin 0.8s linear infinite;
                    }}
                    @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
                </style>
            </head>
            <body class="bg-transparent text-zinc-100 min-h-screen flex flex-col justify-between p-8 relative">
                {self._get_ambient_bg()}
                <div class="max-w-xl mx-auto my-auto w-full z-10">
                    <div class="flex items-center gap-4 mb-8">
                        <img src="{bot_avatar}" class="w-12 h-12 rounded-xl border border-white/20 shadow-[0_4px_15px_rgba(0,0,0,0.3)]" alt="Recluse">
                        <div>
                            <h1 class="font-bold text-xl text-white drop-shadow-md">Recluse Security Gateway</h1>
                            <p class="text-sm text-zinc-300 font-mono">DDoS &amp; Bot Protection</p>
                        </div>
                    </div>
                    <div class="bg-white/10 backdrop-blur-2xl border border-white/20 rounded-2xl p-6 mb-4 flex items-center justify-between shadow-[0_8px_32px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)]">
                        <div class="flex items-center gap-5">
                            <div class="spinner"></div>
                            <div>
                                <div class="text-base font-semibold text-white">Verifying browser connection...</div>
                            </div>
                        </div>
                        <span class="text-xs font-mono text-zinc-400 bg-black/30 px-2 py-1 rounded">ID: {ray_id}</span>
                    </div>
                </div>
                <script>
                    setTimeout(async () => {{
                        try {{
                            await fetch('/api/verify_visitor', {{ method: 'POST' }});
                            window.location.reload();
                        }} catch (e) {{ console.error(e); }}
                    }}, 1200);
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
        response.set_cookie('recluse_verified', 'true', max_age=86400 * 7, httponly=True)
        return response

    async def get_user_session(self, request):
        session_id = request.cookies.get("recluse_session")
        if not session_id or not hasattr(self.bot, 'db'):
            return None
        session = await self.bot.db.sessions.find_one({"session_id": session_id})
        if session and (datetime.datetime.utcnow().timestamp() - session.get('created_at', 0) < 86400):
            return session
        return None

    def _render_sidebar(self, guild_id, active_tab, guild_name, guild_icon):
        tabs = [
            ("overview", f"/manage/{guild_id}", "fa-chart-pie", "Overview"),
            ("modules", f"/modules/{guild_id}", "fa-layer-group", "Modules & Commands"),
            ("automod", f"/automod/{guild_id}", "fa-shield-halved", "Auto Moderation"),
            ("wizard", f"/wizard/{guild_id}", "fa-wand-magic-sparkles", "Setup Wizard"),
            ("lockdown", f"/lockdown/{guild_id}", "fa-lock", "Server Lockdown"),
            ("misc", f"/misc/{guild_id}", "fa-sliders", "Utilities & Misc"),
            ("logs", f"/logs/{guild_id}", "fa-database", "Audit Logs")
        ]

        links_html = ""
        for key, href, icon, title in tabs:
            is_active = key == active_tab
            active_classes = "bg-white/15 text-white border-white/30 shadow-[0_4px_15px_rgba(0,0,0,0.2)] font-medium" if is_active else "text-zinc-400 hover:text-white hover:bg-white/10 border-transparent transition-all"
            links_html += f"""
            <a href="{href}" class="flex items-center gap-3 px-4 py-3 rounded-xl border text-sm transition {active_classes}">
                <i class="fa-solid {icon} w-5 text-center text-lg"></i>
                <span>{title}</span>
            </a>
            """

        return f"""
        <aside class="w-72 bg-black/30 backdrop-blur-3xl border-r border-white/10 flex flex-col flex-shrink-0 z-20 shadow-[4px_0_24px_rgba(0,0,0,0.2)]">
            <div class="p-6 border-b border-white/10 flex items-center gap-4">
                <img src="{guild_icon}" class="w-12 h-12 rounded-xl object-cover border border-white/30 shadow-md" alt="{guild_name}">
                <div class="overflow-hidden">
                    <h2 class="font-bold text-base text-white truncate drop-shadow-sm">{guild_name}</h2>
                    <p class="text-xs font-mono text-zinc-400 mt-0.5">{guild_id}</p>
                </div>
            </div>
            <nav class="flex-1 p-4 space-y-1.5 overflow-y-auto">
                {links_html}
            </nav>
            <div class="p-4 border-t border-white/10">
                <a href="/" class="flex items-center gap-3 px-4 py-3 text-sm font-medium text-zinc-400 hover:text-white rounded-xl hover:bg-white/10 transition shadow-sm border border-transparent hover:border-white/20">
                    <i class="fa-solid fa-arrow-left"></i>
                    <span>Switch Server</span>
                </a>
            </div>
        </aside>
        """

    # ─────────────────────────────────────────────────────────────────────────
    # AUTHENTICATION
    # ─────────────────────────────────────────────────────────────────────────

    async def login(self, request):
        client_id = os.getenv("DISCORD_CLIENT_ID")
        redirect_uri = os.getenv("REDIRECT_URI")
        if not client_id or not redirect_uri:
            return web.Response(text="Configuration Error: Client ID or Redirect URI not defined.", status=500)
        oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&response_type=code&scope=identify%20guilds"
        raise web.HTTPFound(oauth_url)

    async def callback(self, request):
        code = request.query.get("code")
        if not code:
            return web.Response(text="Missing OAuth authorization code.", status=400)

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
                if resp.status != 200:
                    return web.Response(text="Failed to exchange authorization token with Discord.", status=500)
                token_data = await resp.json()
                access_token = token_data.get("access_token")

            async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                user_data = await resp.json()

        session_id = str(uuid.uuid4())
        if hasattr(self.bot, 'db'):
            await self.bot.db.sessions.update_one(
                {"discord_id": user_data["id"]},
                {"$set": {
                    "session_id": session_id,
                    "username": user_data.get("username", "Unknown"),
                    "avatar": user_data.get("avatar", ""),
                    "access_token": access_token,
                    "created_at": datetime.datetime.utcnow().timestamp()
                }},
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

    # ─────────────────────────────────────────────────────────────────────────
    # HOME
    # ─────────────────────────────────────────────────────────────────────────

    async def home(self, request):
        user_session = await self.get_user_session(request)
        bot_avatar = self.get_bot_avatar()
        client_id = os.getenv("DISCORD_CLIENT_ID", "")
        invite_link = f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot%20applications.commands"

        if not user_session:
            landing_html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Recluse | Discord Moderation</title>
                <link rel="icon" type="image/png" href="{bot_avatar}">
                <script src="https://cdn.tailwindcss.com"></script>
                <style>
                    /* Typewriter Animation */
                    @keyframes typing {{
                        from {{ width: 0 }}
                        to {{ width: 100% }}
                    }}
                    @keyframes blink-caret {{
                        from, to {{ border-color: transparent }}
                        50% {{ border-color: rgba(255, 255, 255, 0.8); }}
                    }}
                    .typewriter-text {{
                        display: inline-block;
                        overflow: hidden;
                        border-right: 4px solid rgba(255, 255, 255, 0.8);
                        white-space: nowrap;
                        margin: 0 auto;
                        animation: typing 2.5s steps(30, end), blink-caret .75s step-end infinite;
                        max-width: 100%;
                    }}
                    
                    /* Fade-in up Animation */
                    @keyframes fadeInUp {{
                        from {{ opacity: 0; transform: translateY(30px); }}
                        to {{ opacity: 1; transform: translateY(0); }}
                    }}
                    .fade-in-up {{
                        animation: fadeInUp 1s ease-out forwards;
                        opacity: 0;
                    }}
                    .delay-1 {{ animation-delay: 0.8s; }}
                    .delay-2 {{ animation-delay: 1.2s; }}
                </style>
            </head>
            <body class="bg-transparent text-zinc-100 min-h-screen flex flex-col font-sans relative">
                {self._get_ambient_bg()}
                <header class="border-b border-white/10 bg-black/30 backdrop-blur-2xl px-8 py-5 flex items-center justify-between z-10 w-full shadow-lg">
                    <div class="flex items-center gap-3 font-bold text-lg max-w-7xl mx-auto w-full justify-between">
                        <div class="flex items-center gap-4">
                            <img src="{bot_avatar}" class="w-10 h-10 rounded-xl border border-white/20 shadow-lg" alt="Recluse">
                            <span class="text-xl tracking-wide">Recluse</span>
                        </div>
                        <a href="/login" class="px-5 py-2.5 text-sm font-semibold rounded-xl bg-blue-600/90 hover:bg-blue-500 backdrop-blur-md text-white transition shadow-[0_4px_20px_0_rgba(37,99,235,0.4),inset_0_1px_1px_rgba(255,255,255,0.2)] border border-blue-500/50">Log In with Discord</a>
                    </div>
                </header>

                <main class="flex-1 max-w-7xl mx-auto w-full px-8 flex flex-col items-center justify-center text-center py-20 z-10 overflow-hidden">
                    <div class="inline-block max-w-full">
                        <h1 class="text-4xl sm:text-6xl md:text-7xl font-extrabold tracking-tight text-white mb-6 drop-shadow-2xl typewriter-text pr-2">Complete server governance.</h1>
                    </div>
                    <p class="text-zinc-300 max-w-2xl text-lg sm:text-xl mb-12 leading-relaxed drop-shadow-md fade-in-up delay-1">Control modules, enforce granular command restrictions, automate raid lockdowns, and integrate generative models from a unified dashboard.</p>
                    <div class="flex flex-wrap gap-5 justify-center fade-in-up delay-2">
                        <a href="/login" class="px-8 py-4 rounded-xl bg-blue-600/90 hover:bg-blue-500 font-bold text-base transition shadow-[0_8px_30px_rgba(37,99,235,0.5),inset_0_1px_1px_rgba(255,255,255,0.2)] border border-blue-500/50">Open Management Console</a>
                        <a href="{invite_link}" target="_blank" class="px-8 py-4 rounded-xl bg-white/10 hover:bg-white/20 backdrop-blur-xl border border-white/20 font-bold text-base transition shadow-[0_8px_30px_rgba(0,0,0,0.3),inset_0_1px_1px_rgba(255,255,255,0.1)] text-white">Add to Server</a>
                    </div>
                </main>
            </body>
            </html>
            """
            return web.Response(text=landing_html, content_type='text/html')

        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=18181b&color=fff"

        app_info = await self.bot.application_info()
        is_owner = int(user_session['discord_id']) == app_info.owner.id
        owner_badge = '<a href="/owner_panel" class="px-3 py-1.5 rounded-xl bg-red-500/20 border border-red-500/40 backdrop-blur-xl shadow-lg text-red-300 text-xs font-bold hover:bg-red-500/30 transition"><i class="fa-solid fa-terminal mr-2"></i>Developer System</a>' if is_owner else ""

        user_guilds = []
        access_token = user_session.get("access_token")
        if access_token:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://discord.com/api/users/@me/guilds", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                    if resp.status == 200:
                        user_guilds = await resp.json()

        admin_guilds = [g for g in user_guilds if (int(g.get('permissions', 0)) & 0x8) == 0x8 or (int(g.get('permissions', 0)) & 0x20) == 0x20]
        bot_guild_ids = [g.id for g in self.bot.guilds]

        cards_html = ""
        for g in admin_guilds:
            in_server = int(g['id']) in bot_guild_ids
            icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get('icon') else f"https://ui-avatars.com/api/?name={urllib.parse.quote(g['name'])}&background=18181b&color=fff"
            target_url = f"/manage/{g['id']}" if in_server else invite_link
            btn_text = "Configure Settings" if in_server else "Setup Bot"
            btn_class = "bg-blue-600/90 hover:bg-blue-500 text-white shadow-lg border border-blue-500/50" if in_server else "bg-white/10 hover:bg-white/20 text-zinc-100 border border-white/20"

            cards_html += f"""
            <div class="bg-white/10 backdrop-blur-3xl border border-white/20 rounded-3xl p-6 flex flex-col justify-between hover:border-white/30 transition-all shadow-[0_8px_32px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] group hover:-translate-y-1">
                <div class="flex items-center gap-5 mb-6">
                    <img src="{icon_url}" class="w-16 h-16 rounded-2xl object-cover border border-white/30 shadow-xl group-hover:scale-105 transition-transform" alt="{g['name']}">
                    <div class="overflow-hidden">
                        <h3 class="font-bold text-white text-lg truncate drop-shadow-md">{g['name']}</h3>
                        <span class="text-xs font-mono text-zinc-300 bg-black/30 px-2 py-0.5 rounded-md mt-1 inline-block">{g['id']}</span>
                    </div>
                </div>
                <a href="{target_url}" class="w-full py-3 rounded-xl {btn_class} backdrop-blur-md font-bold text-sm text-center transition">{btn_text}</a>
            </div>
            """

        selector_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>Recluse | Select Server</title>
            <link rel="icon" type="image/png" href="{bot_avatar}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
            <style>
                .fade-in-up {{
                    animation: fadeInUp 0.8s ease-out forwards;
                    opacity: 0;
                    transform: translateY(20px);
                }}
                @keyframes fadeInUp {{
                    to {{ opacity: 1; transform: translateY(0); }}
                }}
            </style>
        </head>
        <body class="bg-transparent text-zinc-100 min-h-screen flex flex-col relative">
            {self._get_ambient_bg()}
            <header class="bg-black/30 backdrop-blur-2xl border-b border-white/10 px-8 py-4 flex items-center justify-between z-10 shadow-xl">
                <div class="flex items-center gap-4 font-bold text-xl">
                    <img src="{bot_avatar}" class="w-10 h-10 rounded-xl border border-white/20 shadow-md" alt="Recluse">
                    <span class="drop-shadow-md">Recluse Console</span>
                </div>
                <div class="flex items-center gap-5">
                    {owner_badge}
                    <div class="flex items-center gap-3 bg-white/10 border border-white/20 px-4 py-2 rounded-xl backdrop-blur-xl shadow-inner">
                        <img src="{user_avatar}" class="w-7 h-7 rounded-lg object-cover shadow-sm" alt="{user_name}">
                        <span class="text-sm font-bold text-white drop-shadow-sm">{user_name}</span>
                    </div>
                    <a href="/logout" class="text-zinc-300 hover:text-white text-lg transition drop-shadow hover:bg-white/10 p-2 rounded-xl"><i class="fa-solid fa-right-from-bracket"></i></a>
                </div>
            </header>
            <main class="flex-1 max-w-7xl w-full mx-auto p-10 z-10 fade-in-up">
                <div class="mb-10 text-center">
                    <h1 class="text-4xl font-extrabold text-white mb-3 drop-shadow-xl tracking-tight">Select a Server</h1>
                    <p class="text-base text-zinc-300 drop-shadow-sm">Choose a community to manage modules, automod, and permissions.</p>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {cards_html}
                </div>
            </main>
        </body>
        </html>
        """
        return web.Response(text=selector_html, content_type='text/html')

    # ─────────────────────────────────────────────────────────────────────────
    # MODULES & COMMANDS (Lazy Loaded Granular Dropdowns)
    # ─────────────────────────────────────────────────────────────────────────

    async def manage_modules(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)

        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Forbidden", status=403)

        settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id}) if hasattr(self.bot, 'db') else {}
        settings = settings or {}
        disabled_cogs = settings.get("disabled_cogs", [])
        command_settings = settings.get("command_settings", {})

        # Prepare JSON payloads for the frontend to prevent massive DOM lag[cite: 1]
        channels_data = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
        roles_data = [{"id": str(r.id), "name": r.name} for r in guild.roles if r.name != "@everyone"]
        
        channels_json = json.dumps(channels_data)
        roles_json = json.dumps(roles_data)
        cmd_settings_json = json.dumps(command_settings)

        modules_html = ""
        for cog_name, cog in self.bot.cogs.items():
            if cog_name.lower() in ["dashboard", "jishaku", "database", "owner"]:
                continue

            cog_checked = "" if cog_name in disabled_cogs else "checked"
            cog_opacity = "opacity-60 grayscale-[30%]" if cog_name in disabled_cogs else ""

            # Hierarchical Command parsing
            cmds_dict = {}
            if hasattr(cog, 'get_commands'):
                for cmd in cog.get_commands():
                    if cmd.name not in cmds_dict:
                        cmds_dict[cmd.name] = {"desc": cmd.help or cmd.description or "No description", "subs": []}
                    if isinstance(cmd, commands.Group):
                        for sub in cmd.commands:
                            cmds_dict[cmd.name]["subs"].append(sub.name)
                            
            if hasattr(cog, 'walk_app_commands'):
                for app_cmd in cog.walk_app_commands():
                    if app_cmd.name not in cmds_dict:
                        cmds_dict[app_cmd.name] = {"desc": app_cmd.description or "No description", "subs": []}
                    if hasattr(app_cmd, 'commands'):
                        for sub in app_cmd.commands:
                            cmds_dict[app_cmd.name]["subs"].append(sub.name)

            commands_list_html = ""
            for cmd_name, cmd_data in cmds_dict.items():
                db_data = command_settings.get(cmd_name, {})
                dis_subs = db_data.get("disabled_subcommands", [])

                # Subcommands remain hardcoded since there are very few of them
                subs_html = ""
                if cmd_data["subs"]:
                    subs_list = ""
                    for sub in cmd_data["subs"]:
                        sub_checked = "" if sub in dis_subs else "checked"
                        subs_list += f"""
                        <label class="flex items-center justify-between p-3.5 bg-black/30 border border-white/10 rounded-xl cursor-pointer hover:bg-white/10 transition shadow-inner">
                            <span class="text-sm font-bold text-zinc-200 drop-shadow-sm">{cmd_name} {sub}</span>
                            <div class="relative inline-block w-10 align-middle select-none">
                                <input type="checkbox" value="{sub}" class="subchk_{cmd_name} absolute block w-5 h-5 rounded-full bg-white border-2 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-zinc-600 focus:outline-none" {sub_checked} style="right: {'0' if sub_checked else 'auto'}; left: {'auto' if sub_checked else '0'}; border-color: {'#3b82f6' if sub_checked else '#52525b'};">
                                <div class="block overflow-hidden h-5 rounded-full cursor-pointer transition-colors duration-300" style="background-color: {'rgba(59,130,246,0.8)' if sub_checked else '#3f3f46'};"></div>
                            </div>
                        </label>
                        """
                    subs_html = f"""
                    <div class="mt-5 pt-5 border-t border-white/10">
                        <h4 class="text-sm font-extrabold text-white mb-3 tracking-wide">Sub Commands:</h4>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">{subs_list}</div>
                    </div>
                    """

                # Lazy-loaded Dropdown Blocks
                commands_list_html += f"""
                <div class="cmd-block mb-4 border border-white/10 bg-white/5 shadow-[0_4px_15px_rgba(0,0,0,0.2),inset_0_1px_1px_rgba(255,255,255,0.05)] rounded-2xl overflow-visible transition-all" data-cmd="{cmd_name}">
                    <div class="p-5 flex items-center justify-between cursor-pointer hover:bg-white/10 transition rounded-2xl" onclick="toggleCmdSettings('{cmd_name}')">
                        <div>
                            <span class="text-base font-extrabold text-white tracking-wide">/{cmd_name}</span>
                            <p class="text-sm text-zinc-400 mt-1 truncate max-w-lg leading-relaxed">{cmd_data['desc']}</p>
                        </div>
                        <i class="fa-solid fa-chevron-down text-zinc-400 text-sm transition transform" id="arr_{cmd_name}"></i>
                    </div>
                    
                    <div id="settings_{cmd_name}" class="hidden p-6 border-t border-white/10 bg-black/40 rounded-b-2xl backdrop-blur-xl">
                        <h4 class="text-sm font-extrabold text-white mb-4 tracking-wide">Additional Permissions ({cmd_name})</h4>
                        <div class="space-y-4">
                            
                            <!-- Allowed Channels -->
                            <div class="relative z-40 custom-dropdown">
                                <label class="text-xs font-bold text-zinc-300 mb-2 block uppercase tracking-wider">Allowed Channels</label>
                                <div class="bg-white/5 border border-white/10 p-3 rounded-xl flex justify-between items-center cursor-pointer hover:border-blue-500/50 hover:bg-white/10 transition shadow-inner" onclick="toggleDrop('{cmd_name}', 'allowed_channels', event)">
                                    <span class="text-sm text-zinc-400 select-none truncate font-medium" id="lbl_{cmd_name}_allowed_channels">Select Channel</span>
                                    <i class="fa-solid fa-caret-down text-zinc-500 text-sm"></i>
                                </div>
                                <div id="drop_{cmd_name}_allowed_channels" class="hidden absolute top-full left-0 w-full mt-2 bg-[#121215]/95 backdrop-blur-3xl border border-white/20 rounded-xl shadow-[0_15px_40px_0_rgba(0,0,0,0.6),inset_0_1px_1px_rgba(255,255,255,0.1)] z-50 max-h-56 overflow-y-auto p-2 custom-scrollbar"></div>
                            </div>

                            <!-- Ignored Channels -->
                            <div class="relative z-30 custom-dropdown">
                                <label class="text-xs font-bold text-zinc-300 mb-2 block uppercase tracking-wider">Ignored Channels</label>
                                <div class="bg-white/5 border border-white/10 p-3 rounded-xl flex justify-between items-center cursor-pointer hover:border-blue-500/50 hover:bg-white/10 transition shadow-inner" onclick="toggleDrop('{cmd_name}', 'ignored_channels', event)">
                                    <span class="text-sm text-zinc-400 select-none truncate font-medium" id="lbl_{cmd_name}_ignored_channels">Select Channel</span>
                                    <i class="fa-solid fa-caret-down text-zinc-500 text-sm"></i>
                                </div>
                                <div id="drop_{cmd_name}_ignored_channels" class="hidden absolute top-full left-0 w-full mt-2 bg-[#121215]/95 backdrop-blur-3xl border border-white/20 rounded-xl shadow-[0_15px_40px_0_rgba(0,0,0,0.6),inset_0_1px_1px_rgba(255,255,255,0.1)] z-50 max-h-56 overflow-y-auto p-2 custom-scrollbar"></div>
                            </div>

                            <!-- Allowed Roles -->
                            <div class="relative z-20 custom-dropdown">
                                <label class="text-xs font-bold text-zinc-300 mb-2 block uppercase tracking-wider">Allowed Roles</label>
                                <div class="bg-white/5 border border-white/10 p-3 rounded-xl flex justify-between items-center cursor-pointer hover:border-blue-500/50 hover:bg-white/10 transition shadow-inner" onclick="toggleDrop('{cmd_name}', 'allowed_roles', event)">
                                    <span class="text-sm text-zinc-400 select-none truncate font-medium" id="lbl_{cmd_name}_allowed_roles">Select Role</span>
                                    <i class="fa-solid fa-caret-down text-zinc-500 text-sm"></i>
                                </div>
                                <div id="drop_{cmd_name}_allowed_roles" class="hidden absolute top-full left-0 w-full mt-2 bg-[#121215]/95 backdrop-blur-3xl border border-white/20 rounded-xl shadow-[0_15px_40px_0_rgba(0,0,0,0.6),inset_0_1px_1px_rgba(255,255,255,0.1)] z-50 max-h-56 overflow-y-auto p-2 custom-scrollbar"></div>
                            </div>

                            <!-- Ignored Roles -->
                            <div class="relative z-10 custom-dropdown">
                                <label class="text-xs font-bold text-zinc-300 mb-2 block uppercase tracking-wider">Ignored Roles</label>
                                <div class="bg-white/5 border border-white/10 p-3 rounded-xl flex justify-between items-center cursor-pointer hover:border-blue-500/50 hover:bg-white/10 transition shadow-inner" onclick="toggleDrop('{cmd_name}', 'ignored_roles', event)">
                                    <span class="text-sm text-zinc-400 select-none truncate font-medium" id="lbl_{cmd_name}_ignored_roles">Select Role</span>
                                    <i class="fa-solid fa-caret-down text-zinc-500 text-sm"></i>
                                </div>
                                <div id="drop_{cmd_name}_ignored_roles" class="hidden absolute top-full left-0 w-full mt-2 bg-[#121215]/95 backdrop-blur-3xl border border-white/20 rounded-xl shadow-[0_15px_40px_0_rgba(0,0,0,0.6),inset_0_1px_1px_rgba(255,255,255,0.1)] z-50 max-h-56 overflow-y-auto p-2 custom-scrollbar"></div>
                            </div>
                        </div>
                        {subs_html}
                    </div>
                </div>
                """

            if not commands_list_html:
                commands_list_html = "<div class='py-4 text-sm text-zinc-500 italic text-center bg-black/20 rounded-xl border border-white/5'>No registered command endpoints in this module.</div>"

            modules_html += f"""
            <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl overflow-visible mb-8 transition-all {cog_opacity}" id="cog-card-{cog_name}">
                <div class="p-6 flex items-center justify-between bg-black/30 cursor-pointer rounded-t-3xl hover:bg-black/40 transition-colors" onclick="toggleAccordion('{cog_name}')">
                    <div class="flex items-center gap-5">
                        <div class="w-8 h-8 rounded-full bg-white/10 flex items-center justify-center border border-white/20 shadow-inner">
                            <i class="fa-solid fa-chevron-right text-white text-sm transition-transform transform" id="arrow-{cog_name}"></i>
                        </div>
                        <div>
                            <h3 class="font-extrabold text-white text-lg tracking-wide drop-shadow-md">{cog_name} Module</h3>
                            <p class="text-sm text-zinc-300 mt-1">Configure commands and granular permissions for {cog_name}</p>
                        </div>
                    </div>
                    <div class="flex items-center gap-4 bg-black/40 px-4 py-2 rounded-xl border border-white/10 shadow-inner" onclick="event.stopPropagation()">
                        <span class="text-xs text-white font-bold uppercase tracking-wider">Status</span>
                        <label class="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" value="{cog_name}" class="sr-only peer cog-toggle" id="cog-toggle-{cog_name}" {cog_checked}>
                            <div class="w-12 h-6 bg-black/60 border border-white/20 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600/90 peer-checked:border-blue-400"></div>
                        </label>
                    </div>
                </div>
                <div class="p-8 bg-black/20 border-t border-white/20 hidden rounded-b-3xl" id="body-{cog_name}">
                    {commands_list_html}
                </div>
            </div>
            """

        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "modules", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Modules</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
            <style>
                .custom-scrollbar::-webkit-scrollbar {{ width: 8px; }} 
                .custom-scrollbar::-webkit-scrollbar-track {{ background: transparent; }} 
                .custom-scrollbar::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 4px; border: 1px solid rgba(255,255,255,0.05); }}
                .custom-scrollbar::-webkit-scrollbar-thumb:hover {{ background: rgba(255,255,255,0.25); }}
            </style>
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <div>
                        <h1 class="font-extrabold text-lg text-white drop-shadow-md">Module & Command Controller</h1>
                        <p class="text-sm text-zinc-300 mt-0.5">Granular permissions with sub-command access</p>
                    </div>
                    <button onclick="saveModuleConfiguration()" id="saveBtn" class="px-6 py-3 rounded-xl bg-blue-600/90 hover:bg-blue-500 text-white font-bold text-sm transition shadow-[0_4px_20px_0_rgba(37,99,235,0.4),inset_0_1px_1px_rgba(255,255,255,0.2)] border border-blue-500/50 flex items-center gap-3">
                        <i class="fa-solid fa-cloud-arrow-up text-lg"></i><span>Save Configuration</span>
                    </button>
                </header>

                <div class="flex-1 overflow-y-auto p-10 custom-scrollbar" onclick="closeAllDrops()">
                    <div class="max-w-5xl mx-auto">
                        <div class="mb-8 p-5 rounded-2xl bg-blue-900/30 backdrop-blur-xl border border-blue-500/40 flex items-start gap-4 shadow-[0_8px_32px_0_rgba(0,0,0,0.3),inset_0_1px_1px_rgba(255,255,255,0.1)]">
                            <i class="fa-solid fa-circle-info text-blue-400 mt-1 text-xl drop-shadow-md"></i>
                            <p class="text-sm text-zinc-200 leading-relaxed">
                                Click on any command to open its <b>Additional Permissions</b> panel. You can explicitly allow or deny execution per channel or role, and toggle specific sub-commands on or off. The dashboard is now highly optimized for speed!
                            </p>
                        </div>
                        {modules_html}
                    </div>
                </div>
            </main>

            <script>
                // Load payload locally in JS to prevent DOM rendering lag 
                window.guildChannels = {channels_json};
                window.guildRoles = {roles_json};
                window.commandSettings = {cmd_settings_json};

                // Initialize Labels on Load to show pre-saved states instantly
                document.addEventListener('DOMContentLoaded', () => {{
                    for (const cmd in window.commandSettings) {{
                        const settings = window.commandSettings[cmd];
                        ['allowed_channels', 'ignored_channels', 'allowed_roles', 'ignored_roles'].forEach(type => {{
                            const lbl = document.getElementById(`lbl_${{cmd}}_${{type}}`);
                            if (lbl && settings[type]) {{
                                const count = settings[type].length;
                                if (count > 0) {{
                                    lbl.innerText = count === 1 ? '1 item selected' : `${{count}} items selected`;
                                    lbl.classList.add('text-blue-400', 'font-bold');
                                    lbl.classList.remove('text-zinc-400', 'font-medium');
                                }}
                            }}
                        }});
                    }}
                }});

                function toggleAccordion(cogName) {{
                    const body = document.getElementById('body-' + cogName);
                    const arrow = document.getElementById('arrow-' + cogName);
                    if (body.classList.contains('hidden')) {{
                        body.classList.remove('hidden'); arrow.classList.add('rotate-90');
                    }} else {{
                        body.classList.add('hidden'); arrow.classList.remove('rotate-90');
                    }}
                }}

                function toggleCmdSettings(cmdName) {{
                    const settings = document.getElementById('settings_' + cmdName);
                    const arrow = document.getElementById('arr_' + cmdName);
                    if(settings.classList.contains('hidden')) {{
                        settings.classList.remove('hidden'); arrow.classList.add('rotate-180');
                    }} else {{
                        settings.classList.add('hidden'); arrow.classList.remove('rotate-180');
                    }}
                }}

                let activeDrop = null;
                
                // Lazy-Render Dropdown on Click to eradicate UI Lag
                function toggleDrop(cmdName, type, event) {{
                    event.stopPropagation();
                    const dropId = `drop_${{cmdName}}_${{type}}`;
                    const drop = document.getElementById(dropId);
                    
                    if (activeDrop && activeDrop !== drop) activeDrop.classList.add('hidden');
                    
                    if (drop.classList.contains('hidden')) {{
                        if (drop.innerHTML.trim() === "") {{
                            renderDropdown(cmdName, type, drop);
                        }}
                        drop.classList.remove('hidden');
                        activeDrop = drop;
                    }} else {{
                        drop.classList.add('hidden');
                        activeDrop = null;
                    }}
                }}

                function renderDropdown(cmdName, type, dropContainer) {{
                    const isChannel = type.includes('channels');
                    const items = isChannel ? window.guildChannels : window.guildRoles;
                    const selected = (window.commandSettings[cmdName] && window.commandSettings[cmdName][type]) ? window.commandSettings[cmdName][type] : [];
                    
                    let html = "";
                    items.forEach(item => {{
                        const isSel = selected.includes(item.id) ? "checked" : "";
                        const icon = isChannel ? "fa-hashtag" : "fa-at";
                        html += `
                            <label class="flex items-center gap-4 p-3 hover:bg-white/10 rounded-xl cursor-pointer transition border border-transparent hover:border-white/10">
                                <input type="checkbox" value="${{item.id}}" class="form-checkbox h-5 w-5 text-blue-500 rounded border-white/30 bg-black/50 shadow-inner focus:ring-0 focus:ring-offset-0" ${{isSel}} onchange="handleCheckbox('${{cmdName}}', '${{type}}', '${{item.id}}', this.checked)">
                                <span class="text-sm font-bold text-zinc-200"><i class="fa-solid ${{icon}} text-zinc-400 mr-2"></i>${{item.name}}</span>
                            </label>
                        `;
                    }});
                    
                    dropContainer.innerHTML = `<div class="flex flex-col gap-1">${{html || "<div class='p-4 text-sm text-zinc-500 text-center italic bg-black/20 rounded-lg border border-white/5'>None available</div>"}}</div>`;
                }}

                function handleCheckbox(cmdName, type, val, isChecked) {{
                    if (!window.commandSettings[cmdName]) window.commandSettings[cmdName] = {{ allowed_channels: [], ignored_channels: [], allowed_roles: [], ignored_roles: [], disabled_subcommands: [] }};
                    if (!window.commandSettings[cmdName][type]) window.commandSettings[cmdName][type] = [];
                    
                    const arr = window.commandSettings[cmdName][type];
                    if (isChecked) {{
                        if (!arr.includes(val)) arr.push(val);
                    }} else {{
                        window.commandSettings[cmdName][type] = arr.filter(id => id !== val);
                    }}
                    
                    const lbl = document.getElementById(`lbl_${{cmdName}}_${{type}}`);
                    const count = window.commandSettings[cmdName][type].length;
                    if (count === 0) {{
                        lbl.innerText = type.includes('channels') ? 'Select Channel' : 'Select Role';
                        lbl.classList.remove('text-blue-400', 'font-bold');
                        lbl.classList.add('text-zinc-400', 'font-medium');
                    }} else {{
                        lbl.innerText = count === 1 ? '1 item selected' : `${{count}} items selected`;
                        lbl.classList.add('text-blue-400', 'font-bold');
                        lbl.classList.remove('text-zinc-400', 'font-medium');
                    }}
                }}

                function closeAllDrops() {{
                    if(activeDrop) {{ activeDrop.classList.add('hidden'); activeDrop = null; }}
                }}

                document.querySelectorAll('input[type="checkbox"]').forEach(chk => {{
                    if(chk.className.includes('subchk')) {{
                        chk.addEventListener('change', function() {{
                            if(this.checked) {{
                                this.style.right = '0'; this.style.left = 'auto'; this.style.borderColor = '#3b82f6';
                                this.nextElementSibling.style.backgroundColor = 'rgba(59,130,246,0.8)';
                            }} else {{
                                this.style.left = '0'; this.style.right = 'auto'; this.style.borderColor = '#52525b';
                                this.nextElementSibling.style.backgroundColor = '#3f3f46';
                            }}
                        }});
                    }}
                }});

                document.querySelectorAll('.cog-toggle').forEach(cogToggle => {{
                    cogToggle.addEventListener('change', function() {{
                        const cogName = this.value;
                        const card = document.getElementById('cog-card-' + cogName);
                        if (this.checked) card.classList.remove('opacity-60', 'grayscale-[30%]');
                        else card.classList.add('opacity-60', 'grayscale-[30%]');
                    }});
                }});

                async function saveModuleConfiguration() {{
                    const btn = document.getElementById('saveBtn');
                    const originalHtml = btn.innerHTML;
                    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-lg"></i><span>Saving...</span>';
                    btn.disabled = true;

                    const disabledCogs = [];
                    document.querySelectorAll('.cog-toggle:not(:checked)').forEach(t => disabledCogs.push(t.value));
                    
                    // Subcommands are still managed via DOM state because there are so few
                    document.querySelectorAll('.cmd-block').forEach(block => {{
                        const cmdName = block.dataset.cmd;
                        const getDisSubs = () => Array.from(block.querySelectorAll(`.subchk_${{cmdName}}:not(:checked)`)).map(e => e.value);
                        
                        if (!window.commandSettings[cmdName]) window.commandSettings[cmdName] = {{}};
                        window.commandSettings[cmdName].disabled_subcommands = getDisSubs();
                    }});

                    try {{
                        const res = await fetch('/api/modules/{guild_id}', {{
                            method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ disabled_cogs: disabledCogs, command_settings: window.commandSettings }})
                        }});
                        if (res.ok) {{
                            btn.innerHTML = '<i class="fa-solid fa-check text-lg"></i><span>Saved Successfully</span>';
                            btn.classList.remove('bg-blue-600/90', 'hover:bg-blue-500', 'border-blue-500/50');
                            btn.classList.add('bg-emerald-600/90', 'border-emerald-500/50');
                            setTimeout(() => {{
                                btn.innerHTML = originalHtml;
                                btn.classList.remove('bg-emerald-600/90', 'border-emerald-500/50');
                                btn.classList.add('bg-blue-600/90', 'hover:bg-blue-500', 'border-blue-500/50');
                                btn.disabled = false;
                            }}, 2000);
                        }} else throw new Error("Failed to save");
                    }} catch (e) {{
                        alert("Error saving settings: " + e.message);
                        btn.innerHTML = originalHtml; btn.disabled = false;
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    async def update_modules(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.json_response({"error": "Unauthorized"}, status=401)

        guild_id = int(request.match_info.get('guild_id'))
        data = await request.json()

        if hasattr(self.bot, 'db'):
            await self.bot.db.guild_settings.update_one(
                {"guild_id": guild_id},
                {"$set": {
                    "disabled_cogs": data.get("disabled_cogs", []), 
                    "command_settings": data.get("command_settings", {})
                }},
                upsert=True
            )
        return web.json_response({"success": True})

    # ─────────────────────────────────────────────────────────────────────────
    # OVERVIEW, AUTOMOD, LOCKDOWN, LOGS
    # ─────────────────────────────────────────────────────────────────────────

    async def manage_server(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Bot is not in this server.", status=404)

        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Forbidden", status=403)

        settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id}) if hasattr(self.bot, 'db') else {}
        settings = settings or {}

        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "overview", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Overview</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <span class="font-extrabold text-lg text-white drop-shadow-md tracking-wide">Server Overview</span>
                </header>
                <div class="flex-1 overflow-y-auto p-10">
                    <div class="max-w-4xl mx-auto space-y-8">
                        <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-8">
                            <div class="flex items-center gap-6">
                                <img src="{guild_icon}" class="w-24 h-24 rounded-2xl border border-white/30 shadow-xl" alt="{guild.name}">
                                <div>
                                    <h1 class="text-3xl font-extrabold text-white mb-2 drop-shadow-md">{guild.name}</h1>
                                    <p class="text-sm font-mono text-zinc-300 mb-4 bg-black/30 px-3 py-1 rounded-lg inline-block shadow-inner">{guild.id}</p>
                                    <div class="flex items-center gap-4">
                                        <span class="px-4 py-2 rounded-xl bg-black/40 border border-white/10 backdrop-blur-md text-sm font-bold text-zinc-100 shadow-inner">
                                            <i class="fa-solid fa-users mr-2 text-blue-400"></i> {guild.member_count:,} Members
                                        </span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
                            <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_32px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-6 hover:-translate-y-1 transition-transform">
                                <div class="text-[11px] font-bold text-zinc-300 uppercase mb-3 tracking-widest flex items-center gap-2"><i class="fa-solid fa-shield-halved text-blue-400"></i> Automod Status</div>
                                <div class="text-2xl font-extrabold text-white">{"Active" if settings.get("automod_enabled", True) else "Disabled"}</div>
                            </div>
                            <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_32px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-6 hover:-translate-y-1 transition-transform">
                                <div class="text-[11px] font-bold text-zinc-300 uppercase mb-3 tracking-widest flex items-center gap-2"><i class="fa-solid fa-lock text-emerald-400"></i> Lockdown</div>
                                <div class="text-2xl font-extrabold {"text-red-400 drop-shadow-[0_0_15px_rgba(248,113,113,0.5)]" if settings.get("lockdown_active", False) else "text-emerald-400 drop-shadow-[0_0_15px_rgba(52,211,153,0.5)]"}">
                                    {"ENGAGED" if settings.get("lockdown_active", False) else "SECURE"}
                                </div>
                            </div>
                            <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_32px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-6 hover:-translate-y-1 transition-transform">
                                <div class="text-[11px] font-bold text-zinc-300 uppercase mb-3 tracking-widest flex items-center gap-2"><i class="fa-solid fa-network-wired text-purple-400"></i> Default AI Core</div>
                                <div class="text-2xl font-extrabold text-white capitalize">{settings.get("default_ai_model", "nexusify")}</div>
                            </div>
                        </div>
                    </div>
                </div>
            </main>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    async def auto_mod(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Server not found", status=404)

        settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id}) if hasattr(self.bot, 'db') else {}
        settings = settings or {}
        banned_words = settings.get("banned_words", ["unauthorized_term_1", "prohibited_phrase"])
        words_string = ", ".join(banned_words)

        pills = "".join(f'<span class="px-4 py-2 rounded-xl bg-red-500/20 border border-red-500/30 backdrop-blur-md text-sm font-bold text-red-200 shadow-sm">{w}</span>' for w in banned_words if w.strip())
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "automod", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Auto Moderation</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <span class="font-extrabold text-lg text-white drop-shadow-md tracking-wide">Automated Content Filtration</span>
                </header>
                <div class="flex-1 overflow-y-auto p-10">
                    <div class="max-w-4xl mx-auto space-y-8">
                        <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-8">
                            <h2 class="font-extrabold text-white text-lg mb-2 drop-shadow-sm flex items-center gap-3"><i class="fa-solid fa-filter text-blue-400"></i>Banned Lexicon</h2>
                            <p class="text-sm text-zinc-300 mb-6">Enter exact words or phrases to filter automatically. Separate items with commas.</p>
                            <textarea id="bannedWords" rows="5" class="w-full bg-black/40 border border-white/20 backdrop-blur-xl rounded-2xl p-5 text-sm font-mono text-zinc-100 focus:outline-none focus:border-blue-500/70 focus:bg-black/60 transition shadow-[inset_0_2px_4px_rgba(0,0,0,0.5)] leading-relaxed">{words_string}</textarea>
                            <div class="mt-6 flex justify-end">
                                <button onclick="saveBannedWords()" id="saveWordsBtn" class="px-6 py-3 rounded-xl bg-blue-600/90 hover:bg-blue-500 text-white font-bold text-sm transition border border-blue-500/50 shadow-[0_4px_20px_0_rgba(37,99,235,0.4),inset_0_1px_1px_rgba(255,255,255,0.2)]">Update Filters</button>
                            </div>
                        </div>
                        <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-8">
                            <h3 class="font-extrabold text-white text-base mb-5 tracking-wide">Active Filters Loaded</h3>
                            <div class="flex flex-wrap gap-3">{pills if pills else '<span class="text-sm text-zinc-400 italic bg-black/30 px-4 py-2 rounded-xl border border-white/10">No active word restrictions.</span>'}</div>
                        </div>
                    </div>
                </div>
            </main>
            <script>
                async function saveBannedWords() {{
                    const words = document.getElementById('bannedWords').value;
                    const btn = document.getElementById('saveWordsBtn'); btn.innerText = 'Updating...';
                    await fetch('/api/settings/{guild_id}', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: 'update_automod', words: words }})
                    }});
                    window.location.reload();
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    async def server_lockdown(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Server not found", status=404)

        settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id}) if hasattr(self.bot, 'db') else {}
        settings = settings or {}
        is_locked = settings.get("lockdown_active", False)

        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "lockdown", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Lockdown</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <span class="font-extrabold text-lg text-white drop-shadow-sm tracking-wide">Emergency Protocols</span>
                </header>
                <div class="flex-1 overflow-y-auto p-10 flex items-center justify-center">
                    <div class="max-w-lg w-full bg-white/10 backdrop-blur-3xl border border-white/20 rounded-[2rem] p-12 text-center shadow-[0_15px_50px_0_rgba(0,0,0,0.5),inset_0_1px_1px_rgba(255,255,255,0.15)]">
                        <div class="w-24 h-24 mx-auto rounded-3xl {'bg-red-500/20 border border-red-500/30 text-red-400 drop-shadow-[0_0_20px_rgba(248,113,113,0.6)] shadow-[inset_0_2px_10px_rgba(248,113,113,0.2)]' if is_locked else 'bg-emerald-500/20 border border-emerald-500/30 text-emerald-400 drop-shadow-[0_0_20px_rgba(52,211,153,0.6)] shadow-[inset_0_2px_10px_rgba(52,211,153,0.2)]'} flex items-center justify-center mb-8 text-4xl shadow-xl">
                            <i class="fa-solid {'fa-lock' if is_locked else 'fa-lock-open'}"></i>
                        </div>
                        <h2 class="text-3xl font-extrabold text-white mb-4 tracking-tight">Emergency Lockdown</h2>
                        <p class="text-sm text-zinc-300 mb-10 leading-relaxed px-4">
                            Severing send permissions from the <code class="bg-black/50 px-2 py-1 rounded-lg text-white border border-white/10 shadow-inner font-bold">@everyone</code> role freezes conversation channels instantaneously to curb active raids.
                        </p>
                        <button onclick="toggleLockdown({str(not is_locked).lower()})" id="lockBtn" class="w-full py-4 rounded-xl font-extrabold text-base text-white transition-all border shadow-xl {'bg-emerald-600/90 hover:bg-emerald-500 border-emerald-500/50 shadow-[0_8px_30px_rgba(52,211,153,0.4),inset_0_1px_1px_rgba(255,255,255,0.2)]' if is_locked else 'bg-red-600/90 hover:bg-red-500 border-red-500/50 shadow-[0_8px_30px_rgba(239,68,68,0.4),inset_0_1px_1px_rgba(255,255,255,0.2)]'}">
                            {"Lift Server Lockdown" if is_locked else "Engage Server Lockdown"}
                        </button>
                    </div>
                </div>
            </main>
            <script>
                async function toggleLockdown(targetState) {{
                    if (targetState && !confirm("ENGAGE LOCKDOWN: This revokes Send Messages from @everyone. Proceed?")) return;
                    const btn = document.getElementById('lockBtn'); btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i>Processing...';
                    const res = await fetch('/api/settings/{guild_id}', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: 'toggle_lockdown', state: targetState }})
                    }});
                    const data = await res.json();
                    if (!res.ok) alert(data.error || "Execution failed");
                    window.location.reload();
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    async def server_logs(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Server not found", status=404)

        rows_html = ""
        if hasattr(self.bot, 'db'):
            cursor = self.bot.db.security_logs.find({"guild_id": guild_id}).sort("timestamp", -1).limit(50)
            async for log in cursor:
                ts = datetime.datetime.fromtimestamp(log.get('timestamp', 0)).strftime('%Y-%m-%d %H:%M')
                rows_html += f"""
                <tr class="border-b border-white/10 hover:bg-white/10 transition-colors text-sm text-zinc-200">
                    <td class="py-4 px-6 font-mono text-zinc-400">{ts}</td>
                    <td class="py-4 px-6 font-bold text-white drop-shadow-sm">{log.get('action')}</td>
                    <td class="py-4 px-6 font-mono text-blue-300 bg-blue-500/5 px-2 rounded">{log.get('user_id')}</td>
                    <td class="py-4 px-6 truncate max-w-md">{log.get('content', '')}</td>
                </tr>
                """

        if not rows_html:
            rows_html = '<tr><td colspan="4" class="py-12 text-center text-sm text-zinc-400 italic bg-black/20">No telemetry logged yet.</td></tr>'

        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "logs", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Security Logs</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
            <style>
                .custom-scrollbar::-webkit-scrollbar {{ width: 8px; height: 8px; }} 
                .custom-scrollbar::-webkit-scrollbar-track {{ background: transparent; }} 
                .custom-scrollbar::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 4px; }}
            </style>
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <span class="font-extrabold text-lg text-white drop-shadow-sm tracking-wide">Automated Security Audits</span>
                </header>
                <div class="flex-1 overflow-y-auto p-10 custom-scrollbar">
                    <div class="max-w-6xl mx-auto bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl overflow-hidden">
                        <div class="overflow-x-auto custom-scrollbar">
                            <table class="w-full text-left whitespace-nowrap">
                                <thead class="bg-black/40 border-b border-white/20 text-xs font-bold text-zinc-300 uppercase tracking-widest shadow-inner">
                                    <tr>
                                        <th class="py-5 px-6">Timestamp</th>
                                        <th class="py-5 px-6">Action</th>
                                        <th class="py-5 px-6">Subject</th>
                                        <th class="py-5 px-6">Payload</th>
                                    </tr>
                                </thead>
                                <tbody>{rows_html}</tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </main>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    # ─────────────────────────────────────────────────────────────────────────
    # WIZARD & MISC RESTORATION (Updated to SaaS UI)
    # ─────────────────────────────────────────────────────────────────────────

    async def wizard_setup(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)

        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id}) if hasattr(self.bot, 'db') else {}
        settings = settings or {}

        ai_model = settings.get("default_ai_model", "nexusify")
        nexusify_checked = "checked" if ai_model == "nexusify" else ""
        gemini_checked = "checked" if ai_model == "gemini" else ""
        sarvam_checked = "checked" if ai_model == "sarvam" else ""

        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "wizard", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Wizard</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
            <style>
                input:checked + div {{ border-color: rgba(59, 130, 246, 0.8); background-color: rgba(255,255,255,0.15); box-shadow: 0 8px 30px rgba(59, 130, 246, 0.3), inset 0 1px 1px rgba(255,255,255,0.2); transform: translateY(-2px); }}
            </style>
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <span class="font-extrabold text-lg text-white drop-shadow-md tracking-wide">Setup Wizard</span>
                </header>
                <div class="flex-1 overflow-y-auto p-10 flex items-center justify-center">
                    <div class="max-w-3xl w-full bg-white/10 backdrop-blur-3xl border border-white/20 rounded-[2.5rem] p-10 shadow-[0_15px_50px_0_rgba(0,0,0,0.5),inset_0_1px_1px_rgba(255,255,255,0.15)]">
                        <div class="mb-10 border-b border-white/10 pb-6 flex items-center gap-4">
                            <div class="p-3 bg-blue-500/20 rounded-xl border border-blue-500/30 text-blue-400 text-xl shadow-inner"><i class="fa-solid fa-microchip"></i></div>
                            <div>
                                <h2 class="text-2xl font-extrabold text-white drop-shadow-md tracking-tight">Select Default AI Core</h2>
                                <p class="text-sm text-zinc-300 mt-1">Choose the primary generative model for chat interactions in this server.</p>
                            </div>
                        </div>
                        <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-10">
                            <label class="relative cursor-pointer group">
                                <input type="radio" name="ai_model" value="nexusify" class="peer sr-only" {nexusify_checked}>
                                <div class="flex flex-col p-6 rounded-2xl border border-white/10 bg-black/30 backdrop-blur-md transition-all duration-300 peer-checked:border-blue-500 hover:border-white/30 hover:bg-white/5">
                                    <i class="fa-solid fa-network-wired text-3xl text-blue-400 mb-4 drop-shadow-[0_0_15px_rgba(96,165,250,0.6)]"></i>
                                    <span class="text-white font-extrabold text-base tracking-wide">Nexusify</span>
                                    <span class="text-zinc-300 font-medium text-xs mt-1.5 bg-black/40 py-1 px-2 rounded-lg inline-block text-center border border-white/5">Grok-3 architecture</span>
                                </div>
                            </label>
                            <label class="relative cursor-pointer group">
                                <input type="radio" name="ai_model" value="gemini" class="peer sr-only" {gemini_checked}>
                                <div class="flex flex-col p-6 rounded-2xl border border-white/10 bg-black/30 backdrop-blur-md transition-all duration-300 hover:border-white/30 hover:bg-white/5" style="input:checked + div {{ border-color: rgba(16, 185, 129, 0.8); box-shadow: 0 8px 30px rgba(16, 185, 129, 0.3), inset 0 1px 1px rgba(255,255,255,0.2); }}">
                                    <i class="fa-brands fa-google text-3xl text-emerald-400 mb-4 drop-shadow-[0_0_15px_rgba(52,211,153,0.6)]"></i>
                                    <span class="text-white font-extrabold text-base tracking-wide">Gemini</span>
                                    <span class="text-zinc-300 font-medium text-xs mt-1.5 bg-black/40 py-1 px-2 rounded-lg inline-block text-center border border-white/5">Flash 2.5 vision</span>
                                </div>
                            </label>
                            <label class="relative cursor-pointer group">
                                <input type="radio" name="ai_model" value="sarvam" class="peer sr-only" {sarvam_checked}>
                                <div class="flex flex-col p-6 rounded-2xl border border-white/10 bg-black/30 backdrop-blur-md transition-all duration-300 hover:border-white/30 hover:bg-white/5" style="input:checked + div {{ border-color: rgba(249, 115, 22, 0.8); box-shadow: 0 8px 30px rgba(249, 115, 22, 0.3), inset 0 1px 1px rgba(255,255,255,0.2); }}">
                                    <i class="fa-solid fa-language text-3xl text-orange-400 mb-4 drop-shadow-[0_0_15px_rgba(251,146,60,0.6)]"></i>
                                    <span class="text-white font-extrabold text-base tracking-wide">Sarvam</span>
                                    <span class="text-zinc-300 font-medium text-xs mt-1.5 bg-black/40 py-1 px-2 rounded-lg inline-block text-center border border-white/5">Multilingual text</span>
                                </div>
                            </label>
                        </div>
                        <div class="flex justify-end">
                            <button onclick="saveSetup()" id="saveBtn" class="px-8 py-3.5 rounded-xl bg-blue-600/90 hover:bg-blue-500 text-white font-bold text-sm transition shadow-[0_4px_20px_0_rgba(37,99,235,0.5),inset_0_1px_1px_rgba(255,255,255,0.2)] border border-blue-500/50 flex items-center gap-2">
                                <i class="fa-solid fa-check"></i> Save Configuration
                            </button>
                        </div>
                    </div>
                </div>
            </main>
            <script>
                async function saveSetup() {{
                    const btn = document.getElementById('saveBtn'); btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';
                    const model = document.querySelector('input[name="ai_model"]:checked').value;
                    await fetch('/api/settings/{guild_id}', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: 'update_ai_model', model: model }})
                    }});
                    btn.innerHTML = '<i class="fa-solid fa-check-double"></i> Saved!';
                    btn.classList.replace('bg-blue-600/90', 'bg-emerald-600/90');
                    btn.classList.replace('border-blue-500/50', 'border-emerald-500/50');
                    setTimeout(() => {{ 
                        btn.innerHTML = '<i class="fa-solid fa-check"></i> Save Configuration'; 
                        btn.classList.replace('bg-emerald-600/90', 'bg-blue-600/90');
                        btn.classList.replace('border-emerald-500/50', 'border-blue-500/50');
                    }}, 2500);
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    async def misc_settings(self, request):
        user_session = await self.get_user_session(request)
        if not user_session: return web.HTTPFound('/login')

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.Response(text="Recluse is not in this server.", status=404)

        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied.", status=403)

        settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id}) if hasattr(self.bot, 'db') else {}
        settings = settings or {}

        misc_checked = "checked" if settings.get("misc_enabled", True) else ""
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=18181b&color=fff"
        sidebar_html = self._render_sidebar(guild_id, "misc", guild.name, guild_icon)

        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>{guild.name} | Misc</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
            <style>
                .toggle-checkbox:checked {{ right: 0; border-color: rgba(59, 130, 246, 0.9); }}
                .toggle-checkbox:checked + .toggle-label {{ background-color: rgba(59,130,246,0.8); box-shadow: 0 0 15px rgba(59,130,246,0.5); }}
            </style>
        </head>
        <body class="bg-transparent text-zinc-100 h-screen flex overflow-hidden font-sans relative">
            {self._get_ambient_bg()}
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden z-10 relative">
                <header class="h-20 border-b border-white/10 bg-black/30 backdrop-blur-2xl px-10 flex items-center justify-between shrink-0 shadow-xl">
                    <span class="font-extrabold text-lg text-white drop-shadow-sm tracking-wide">Miscellaneous Utilities</span>
                </header>
                <div class="flex-1 overflow-y-auto p-10">
                    <div class="max-w-4xl mx-auto bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_8px_40px_0_rgba(0,0,0,0.4),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-3xl p-10">
                        <div class="flex justify-between items-center pb-2">
                            <div class="flex gap-5 items-center">
                                <div class="w-14 h-14 rounded-2xl bg-teal-500/20 border border-teal-500/30 flex items-center justify-center text-teal-400 text-2xl shadow-inner">
                                    <i class="fa-solid fa-sliders"></i>
                                </div>
                                <div>
                                    <h3 class="text-white font-extrabold text-xl drop-shadow-md tracking-tight">Dynamic AFK & Information Tools</h3>
                                    <p class="text-sm text-zinc-300 mt-2">Enables <code class="bg-black/50 px-2 py-1 rounded-md text-white border border-white/10 font-bold shadow-inner">/afk</code>, <code class="bg-black/50 px-2 py-1 rounded-md text-white border border-white/10 font-bold shadow-inner">/whois</code>, and <code class="bg-black/50 px-2 py-1 rounded-md text-white border border-white/10 font-bold shadow-inner">/serverinfo</code>.</p>
                                </div>
                            </div>
                            <div class="relative inline-block w-14 align-middle select-none transition duration-200 ease-in ml-6">
                                <input type="checkbox" id="toggleMisc" {misc_checked} class="toggle-checkbox absolute block w-7 h-7 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-zinc-600 focus:outline-none"/>
                                <label class="toggle-label block overflow-hidden h-7 rounded-full bg-black/60 border border-white/20 cursor-pointer transition-all duration-300 shadow-inner"></label>
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
                        this.style.borderColor = 'rgba(59, 130, 246, 0.9)'; label.style.backgroundColor = 'rgba(59,130,246,0.8)';
                        label.style.boxShadow = '0 0 15px rgba(59,130,246,0.5)';
                    }} else {{
                        this.style.right = 'auto'; this.style.left = '0';
                        this.style.borderColor = '#52525b'; label.style.backgroundColor = 'rgba(0,0,0,0.6)';
                        label.style.boxShadow = 'inset 0 2px 4px rgba(0,0,0,0.5)';
                    }}
                    await fetch('/api/settings/{guild_id}', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: 'toggle', module: 'toggleMisc', enabled: this.checked }})
                    }});
                }});
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    # ─────────────────────────────────────────────────────────────────────────
    # OWNER PANEL & IP FIREWALL
    # ─────────────────────────────────────────────────────────────────────────

    async def owner_panel(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.HTTPFound('/login')

        app_info = await self.bot.application_info()
        if int(user_session['discord_id']) != app_info.owner.id:
            return web.Response(text="Developer clearance required.", status=403)

        cogs_html = ""
        for ext in list(self.bot.extensions.keys()):
            clean = ext.replace("cogs.", "")
            cogs_html += f"""
            <div class="flex items-center justify-between py-3.5 border-b border-white/10 hover:bg-white/10 transition-colors px-4 rounded-xl text-sm font-medium">
                <span class="font-mono text-zinc-200">{ext}</span>
                <button onclick="reloadModule('{clean}')" class="px-4 py-1.5 rounded-lg bg-black/40 border border-white/20 hover:bg-white/20 hover:text-white text-zinc-300 transition-all font-bold shadow-inner">
                    Reload
                </button>
            </div>
            """

        ip_rows = ""
        if hasattr(self.bot, 'db'):
            async for v in self.bot.db.visit_logs.find().sort("last_visit", -1).limit(30):
                last_user = v.get('last_user', 'Guest')
                user_badge_color = "bg-violet-500/30 text-violet-200 border-violet-500/50 shadow-[0_0_10px_rgba(139,92,246,0.3)]" if last_user != 'Guest' else "bg-black/50 text-zinc-300 border-white/10 shadow-inner"
                ip_rows += f"""
                <div class="flex items-center justify-between py-4 border-b border-white/10 hover:bg-white/10 transition-colors px-4 rounded-xl text-sm filter-row">
                    <div>
                        <span class="font-mono font-bold text-white ip-text drop-shadow-sm">{v.get('ip')}</span>
                        <span class="ml-3 text-xs px-2.5 py-1 rounded-md border {user_badge_color} user-text backdrop-blur-md font-semibold tracking-wide">{last_user}</span>
                        <div class="text-xs text-zinc-400 mt-2 font-medium bg-black/30 inline-block px-2 py-0.5 rounded border border-white/5">Hits: {v.get('hits', 1)}</div>
                    </div>
                    <button onclick="ipAction('ban', '{v.get('ip')}')" class="px-4 py-2 rounded-xl bg-red-600/20 text-red-300 hover:bg-red-600 hover:text-white border border-red-500/40 transition-all font-bold shadow-sm">
                        Ban IP
                    </button>
                </div>
                """

        owner_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>Recluse | Developer Root</title>
            <link rel="icon" type="image/png" href="{self.get_bot_avatar()}">
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
        </head>
        <body class="bg-transparent text-zinc-100 min-h-screen p-8 relative">
            {self._get_ambient_bg()}
            <div class="max-w-7xl mx-auto space-y-8 z-10 relative">
                <div class="flex items-center justify-between border-b border-white/20 pb-6 mb-10 bg-black/20 backdrop-blur-xl p-6 rounded-3xl shadow-lg">
                    <div class="flex items-center gap-5">
                        <div class="w-14 h-14 rounded-2xl bg-red-500/20 border border-red-500/40 flex items-center justify-center text-red-400 text-2xl shadow-[inset_0_2px_10px_rgba(248,113,113,0.3)]">
                            <i class="fa-solid fa-terminal"></i>
                        </div>
                        <div>
                            <h1 class="text-3xl font-extrabold text-white drop-shadow-lg tracking-tight">System Override Console</h1>
                            <p class="text-sm text-zinc-300 font-mono mt-2 bg-black/40 px-3 py-1 rounded-lg inline-block border border-white/10 shadow-inner">Guilds: {len(self.bot.guilds)} | Cogs: {len(self.bot.cogs)}</p>
                        </div>
                    </div>
                    <a href="/" class="text-sm font-bold px-5 py-2.5 rounded-xl bg-white/10 border border-white/20 backdrop-blur-xl text-zinc-200 hover:text-white hover:bg-white/20 transition shadow-sm">Exit to Home</a>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_15px_40px_0_rgba(0,0,0,0.5),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-[2rem] p-8">
                        <h2 class="font-extrabold text-white text-lg mb-6 drop-shadow-md flex items-center gap-3"><i class="fa-solid fa-rotate text-blue-400"></i> Hot-Reload Cogs</h2>
                        <div class="overflow-y-auto max-h-[600px] pr-4 custom-scrollbar">
                            {cogs_html}
                        </div>
                    </div>

                    <div class="bg-white/10 backdrop-blur-3xl border border-white/20 shadow-[0_15px_40px_0_rgba(0,0,0,0.5),inset_0_1px_1px_rgba(255,255,255,0.15)] rounded-[2rem] p-8 flex flex-col h-[700px]">
                        <h2 class="font-extrabold text-white text-lg mb-5 drop-shadow-md flex items-center gap-3"><i class="fa-solid fa-radar text-emerald-400"></i> Ingress Telemetry</h2>
                        <input type="text" id="ipSearch" placeholder="Search IP or Discord Username..." class="w-full bg-black/40 border border-white/20 backdrop-blur-2xl rounded-2xl p-4 text-sm font-medium text-white mb-6 focus:outline-none focus:border-blue-500/70 focus:bg-black/60 transition shadow-[inset_0_2px_10px_rgba(0,0,0,0.5)]">
                        <div class="overflow-y-auto flex-1 pr-4 custom-scrollbar" id="ipContainer">
                            {ip_rows if ip_rows else '<p class="text-sm text-zinc-400 italic px-4 bg-black/20 py-4 rounded-xl text-center border border-white/5">No logged hits.</p>'}
                        </div>
                    </div>
                </div>
            </div>

            <script>
                document.getElementById('ipSearch').addEventListener('input', function(e) {{
                    const query = e.target.value.toLowerCase();
                    document.querySelectorAll('.filter-row').forEach(row => {{
                        const ip = row.querySelector('.ip-text').innerText.toLowerCase();
                        const user = row.querySelector('.user-text').innerText.toLowerCase();
                        if (ip.includes(query) || user.includes(query)) row.style.display = 'flex';
                        else row.style.display = 'none';
                    }});
                }});

                async function reloadModule(name) {{
                    const res = await fetch('/api/owner_action', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: 'reload_cog', cog: name }})
                    }});
                    if(res.ok) alert(name + " reloaded successfully.");
                    else alert("Failed to reload " + name);
                }}

                async function ipAction(act, ip) {{
                    if(!confirm(`Confirm ${{act}} on IP ${{ip}}?`)) return;
                    await fetch('/api/ip_action', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: act, ip: ip }})
                    }});
                    window.location.reload();
                }}
            </script>
            <style>
                .custom-scrollbar::-webkit-scrollbar {{ width: 8px; }}
                .custom-scrollbar::-webkit-scrollbar-track {{ background: transparent; }}
                .custom-scrollbar::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 4px; border: 1px solid rgba(255,255,255,0.05); }}
                .custom-scrollbar::-webkit-scrollbar-thumb:hover {{ background: rgba(255,255,255,0.25); }}
            </style>
        </body>
        </html>
        """
        return web.Response(text=owner_html, content_type='text/html')

    # ─────────────────────────────────────────────────────────────────────────
    # BACKEND API ACTION DISPATCHERS
    # ─────────────────────────────────────────────────────────────────────────

    async def update_settings(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.json_response({"error": "Unauthorized"}, status=401)

        guild_id = int(request.match_info.get('guild_id'))
        guild = self.bot.get_guild(guild_id)
        if not guild: return web.json_response({"error": "Guild missing"}, status=404)

        data = await request.json()
        action = data.get('action')

        if action == 'update_automod':
            words_string = data.get('words', '')
            words = [w.strip().lower() for w in words_string.split(',') if w.strip()]
            if hasattr(self.bot, 'db'):
                await self.bot.db.guild_settings.update_one(
                    {"guild_id": guild_id},
                    {"$set": {"banned_words": words}},
                    upsert=True
                )
            return web.json_response({"success": True})

        elif action == 'update_ai_model':
            model = data.get('model')
            if hasattr(self.bot, 'db'):
                await self.bot.db.guild_settings.update_one(
                    {"guild_id": guild_id},
                    {"$set": {"default_ai_model": model}},
                    upsert=True
                )
            return web.json_response({"success": True})

        elif action == 'toggle':
            module = data.get('module')
            enabled = data.get('enabled')
            if module == 'toggleMisc' and hasattr(self.bot, 'db'):
                await self.bot.db.guild_settings.update_one(
                    {"guild_id": guild_id},
                    {"$set": {"misc_enabled": enabled}},
                    upsert=True
                )
            return web.json_response({"success": True})

        elif action == 'toggle_lockdown':
            state = data.get('state', True)
            if hasattr(self.bot, 'db'):
                await self.bot.db.guild_settings.update_one(
                    {"guild_id": guild_id},
                    {"$set": {"lockdown_active": state}},
                    upsert=True
                )
            try:
                await guild.default_role.edit(send_messages=not state, reason="Dashboard lockdown toggle")
                return web.json_response({"success": True})
            except discord.Forbidden:
                return web.json_response({"error": "Missing Manage Roles permission in Discord"}, status=403)

        return web.json_response({"error": "Invalid action"}, status=400)

    async def handle_owner_action(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.json_response({"error": "Unauthorized"}, status=401)

        app_info = await self.bot.application_info()
        if int(user_session['discord_id']) != app_info.owner.id:
            return web.json_response({"error": "Forbidden"}, status=403)

        data = await request.json()
        action = data.get('action')

        if action == 'reload_cog':
            cog_name = data.get('cog')
            if not cog_name.startswith('cogs.'):
                cog_name = f"cogs.{cog_name}"
            try:
                await self.bot.reload_extension(cog_name)
                return web.json_response({"success": True})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"error": "Unknown action"}, status=400)

    async def handle_ip_action(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.json_response({"error": "Unauthorized"}, status=401)

        app_info = await self.bot.application_info()
        if int(user_session['discord_id']) != app_info.owner.id:
            return web.json_response({"error": "Forbidden"}, status=403)

        data = await request.json()
        action = data.get('action')
        ip = data.get('ip')

        if not ip or not hasattr(self.bot, 'db'):
            return web.json_response({"error": "Bad payload"}, status=400)

        if action == 'ban':
            await self.bot.db.ip_bans.update_one(
                {"ip": ip},
                {"$set": {"banned_at": datetime.datetime.utcnow().timestamp()}},
                upsert=True
            )
        elif action == 'unban':
            await self.bot.db.ip_bans.delete_one({"ip": ip})

        return web.json_response({"success": True})

    # ─────────────────────────────────────────────────────────────────────────
    # LIFECYCLE MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    async def start_server(self):
        port = int(os.getenv("PORT", 8080))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', port)
        try:
            await self.site.start()
            print(f"🌐 Dashboard operational on http://0.0.0.0:{port}")
        except Exception as e:
            print(f"❌ Failed to bind dashboard port: {e}")

        await self.bot.wait_until_ready()

    async def cog_unload(self):
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())


async def setup(bot):
    await bot.add_cog(Dashboard(bot))
