"""
cogs/dashboard.py — Recluse Web Dashboard
"""

import os
import uuid
import datetime
import urllib.parse
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
import random
import string


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
                    <body class="bg-[#09090b] text-zinc-100 min-h-screen flex items-center justify-center p-6">
                        <div class="max-w-md w-full bg-[#121215] border border-zinc-800/80 rounded-2xl p-8 text-center shadow-2xl">
                            <div class="w-16 h-16 mx-auto rounded-2xl bg-amber-500/10 border border-amber-500/20 flex items-center justify-center mb-6">
                                <i class="fa-solid fa-screwdriver-wrench text-amber-400 text-2xl"></i>
                            </div>
                            <h1 class="text-2xl font-bold text-white mb-2">Web Maintenance</h1>
                            <p class="text-zinc-400 text-sm mb-6 leading-relaxed">
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
                        width: 22px; height: 22px;
                        border-radius: 50%;
                        border-left-color: #3b82f6;
                        animation: spin 0.8s linear infinite;
                    }}
                    @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
                </style>
            </head>
            <body class="bg-[#09090b] text-zinc-100 min-h-screen flex flex-col justify-between p-8">
                <div class="max-w-xl mx-auto my-auto w-full">
                    <div class="flex items-center gap-3 mb-6">
                        <img src="{bot_avatar}" class="w-10 h-10 rounded-xl border border-zinc-800" alt="Recluse">
                        <div>
                            <h1 class="font-bold text-lg text-white">Recluse Security Gateway</h1>
                            <p class="text-xs text-zinc-500 font-mono">DDoS &amp; Bot Protection</p>
                        </div>
                    </div>
                    <div class="bg-[#121215] border border-zinc-800 rounded-xl p-6 mb-4 flex items-center justify-between">
                        <div class="flex items-center gap-4">
                            <div class="spinner"></div>
                            <div>
                                <div class="text-sm font-semibold text-white">Verifying browser connection...</div>
                            </div>
                        </div>
                        <span class="text-xs font-mono text-zinc-600">ID: {ray_id}</span>
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
            active_classes = "bg-blue-600/10 text-blue-400 border-blue-500/30 font-medium" if is_active else "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40 border-transparent"
            links_html += f"""
            <a href="{href}" class="flex items-center gap-3 px-3.5 py-2.5 rounded-xl border text-sm transition {active_classes}">
                <i class="fa-solid {icon} w-4 text-center"></i>
                <span>{title}</span>
            </a>
            """

        return f"""
        <aside class="w-64 bg-[#0d0d10] border-r border-zinc-800/80 flex flex-col flex-shrink-0 z-20">
            <div class="p-4 border-b border-zinc-800/80 flex items-center gap-3">
                <img src="{guild_icon}" class="w-10 h-10 rounded-xl object-cover border border-zinc-700/60" alt="{guild_name}">
                <div class="overflow-hidden">
                    <h2 class="font-bold text-sm text-white truncate">{guild_name}</h2>
                    <p class="text-[11px] font-mono text-zinc-500">{guild_id}</p>
                </div>
            </div>
            <nav class="flex-1 p-3 space-y-1 overflow-y-auto">
                {links_html}
            </nav>
            <div class="p-3 border-t border-zinc-800/80">
                <a href="/" class="flex items-center gap-2.5 px-3 py-2 text-xs font-medium text-zinc-400 hover:text-white rounded-lg hover:bg-zinc-800/40 transition">
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
            </head>
            <body class="bg-[#09090b] text-zinc-100 min-h-screen flex flex-col font-sans">
                <header class="border-b border-zinc-800/80 px-8 py-5 flex items-center justify-between max-w-7xl mx-auto w-full">
                    <div class="flex items-center gap-3 font-bold text-lg">
                        <img src="{bot_avatar}" class="w-8 h-8 rounded-xl border border-zinc-800" alt="Recluse">
                        <span>Recluse</span>
                    </div>
                    <a href="/login" class="px-4 py-2 text-sm font-semibold rounded-lg bg-blue-600 hover:bg-blue-500 text-white transition">Log In with Discord</a>
                </header>

                <main class="flex-1 max-w-7xl mx-auto w-full px-8 flex flex-col items-center justify-center text-center py-20">
                    <h1 class="text-4xl sm:text-6xl font-extrabold tracking-tight text-white max-w-3xl mb-6">Complete server governance.</h1>
                    <p class="text-zinc-400 max-w-xl text-base sm:text-lg mb-10 leading-relaxed">Control modules, enforce granular command restrictions, automate raid lockdowns, and integrate generative models from a unified dashboard.</p>
                    <div class="flex flex-wrap gap-4 justify-center">
                        <a href="/login" class="px-6 py-3.5 rounded-xl bg-blue-600 hover:bg-blue-500 font-semibold text-sm transition shadow-lg shadow-blue-600/20">Open Management Console</a>
                        <a href="{invite_link}" target="_blank" class="px-6 py-3.5 rounded-xl bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 font-semibold text-sm transition">Add to Server</a>
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
        owner_badge = '<a href="/owner_panel" class="px-3 py-1.5 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs font-semibold hover:bg-red-500/20 transition"><i class="fa-solid fa-terminal mr-1.5"></i>Developer System</a>' if is_owner else ""

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
            btn_text = "Configure" if in_server else "Setup Bot"
            btn_class = "bg-blue-600 hover:bg-blue-500 text-white" if in_server else "bg-zinc-800 hover:bg-zinc-700 text-zinc-300"

            cards_html += f"""
            <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-5 flex flex-col justify-between hover:border-zinc-700 transition">
                <div class="flex items-center gap-4 mb-5">
                    <img src="{icon_url}" class="w-14 h-14 rounded-2xl object-cover border border-zinc-800" alt="{g['name']}">
                    <div class="overflow-hidden">
                        <h3 class="font-bold text-white text-base truncate">{g['name']}</h3>
                        <span class="text-xs font-mono text-zinc-500">{g['id']}</span>
                    </div>
                </div>
                <a href="{target_url}" class="w-full py-2.5 rounded-xl {btn_class} font-semibold text-xs text-center transition">{btn_text}</a>
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
        </head>
        <body class="bg-[#09090b] text-zinc-100 min-h-screen flex flex-col">
            <header class="border-b border-zinc-800/80 px-8 py-4 flex items-center justify-between">
                <div class="flex items-center gap-3 font-bold text-lg">
                    <img src="{bot_avatar}" class="w-8 h-8 rounded-xl border border-zinc-800" alt="Recluse">
                    <span>Recluse Console</span>
                </div>
                <div class="flex items-center gap-4">
                    {owner_badge}
                    <div class="flex items-center gap-3 bg-zinc-900 border border-zinc-800 px-3 py-1.5 rounded-xl">
                        <img src="{user_avatar}" class="w-6 h-6 rounded-lg object-cover" alt="{user_name}">
                        <span class="text-xs font-semibold text-zinc-300">{user_name}</span>
                    </div>
                    <a href="/logout" class="text-zinc-500 hover:text-zinc-300 text-sm transition"><i class="fa-solid fa-right-from-bracket"></i></a>
                </div>
            </header>
            <main class="flex-1 max-w-6xl w-full mx-auto p-8">
                <div class="mb-8">
                    <h1 class="text-2xl font-bold text-white mb-1">Select a Server</h1>
                    <p class="text-sm text-zinc-400">Choose a community to manage modules, automod, and permissions.</p>
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
                    {cards_html}
                </div>
            </main>
        </body>
        </html>
        """
        return web.Response(text=selector_html, content_type='text/html')

    # ─────────────────────────────────────────────────────────────────────────
    # MODULES & COMMANDS
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
        disabled_cmds = settings.get("disabled_cmds", [])

        modules_html = ""
        for cog_name, cog in self.bot.cogs.items():
            if cog_name.lower() in ["dashboard", "jishaku", "database", "owner"]:
                continue

            cog_checked = "" if cog_name in disabled_cogs else "checked"
            cog_opacity = "opacity-60" if cog_name in disabled_cogs else ""

            # Safely grab both prefix commands and app commands (slash commands)
            all_cmds = []
            
            # 1. Prefix Commands
            if hasattr(cog, 'walk_commands'):
                for cmd in cog.walk_commands():
                    all_cmds.append({
                        "name": cmd.qualified_name,
                        "desc": cmd.help or cmd.description or "No description specified.",
                        "is_sub": cmd.parent is not None
                    })
            
            # 2. Slash Commands
            if hasattr(cog, 'walk_app_commands'):
                for app_cmd in cog.walk_app_commands():
                    all_cmds.append({
                        "name": app_cmd.name,
                        "desc": app_cmd.description or "No description specified.",
                        "is_sub": False # Simplification for app commands
                    })

            # Deduplicate by name just in case of hybrid commands
            seen = set()
            unique_cmds = []
            for cmd in all_cmds:
                if cmd["name"] not in seen:
                    seen.add(cmd["name"])
                    unique_cmds.append(cmd)

            commands_list_html = ""
            for cmd in unique_cmds:
                indent_class = "pl-8 border-l-2 border-zinc-800 ml-4 my-1" if cmd["is_sub"] else "py-3 border-b border-zinc-800/60 last:border-0"
                cmd_checked = "" if (cmd["name"] in disabled_cmds or cog_name in disabled_cogs) else "checked"

                commands_list_html += f"""
                <div class="flex items-center justify-between {indent_class} transition">
                    <div>
                        <div class="flex items-center gap-2">
                            <span class="text-xs font-mono font-bold text-zinc-200">/{cmd["name"]}</span>
                        </div>
                        <p class="text-xs text-zinc-500 mt-0.5">{cmd["desc"]}</p>
                    </div>
                    <label class="relative inline-flex items-center cursor-pointer ml-4">
                        <input type="checkbox" value="{cmd["name"]}" class="sr-only peer cmd-toggle" data-cog="{cog_name}" {cmd_checked}>
                        <div class="w-8 h-4 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-3 after:w-3 after:transition-all peer-checked:bg-blue-600"></div>
                    </label>
                </div>
                """

            if not commands_list_html:
                commands_list_html = "<div class='py-3 text-xs text-zinc-600 italic'>No registered command endpoints in this module.</div>"

            modules_html += f"""
            <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl overflow-hidden mb-5 transition {cog_opacity}" id="cog-card-{cog_name}">
                <div class="p-5 flex items-center justify-between bg-zinc-900/40 cursor-pointer" onclick="toggleAccordion('{cog_name}')">
                    <div class="flex items-center gap-3">
                        <i class="fa-solid fa-chevron-right text-zinc-500 text-xs transition transform" id="arrow-{cog_name}"></i>
                        <div>
                            <h3 class="font-bold text-white text-sm tracking-wide">{cog_name} Module</h3>
                            <p class="text-xs text-zinc-500">Configure command access for {cog_name}</p>
                        </div>
                    </div>
                    <div class="flex items-center gap-3" onclick="event.stopPropagation()">
                        <span class="text-xs text-zinc-500 font-mono">Module Status</span>
                        <label class="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" value="{cog_name}" class="sr-only peer cog-toggle" id="cog-toggle-{cog_name}" {cog_checked}>
                            <div class="w-10 h-5 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-blue-600"></div>
                        </label>
                    </div>
                </div>
                <div class="px-6 py-2 bg-[#0e0e11] border-t border-zinc-800/60 hidden" id="body-{cog_name}">
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
            <style>::-webkit-scrollbar {{ width: 6px; }} ::-webkit-scrollbar-track {{ background: #09090b; }} ::-webkit-scrollbar-thumb {{ background: #27272a; border-radius: 3px; }}</style>
        </head>
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <div>
                        <h1 class="font-bold text-sm text-white">Module & Command Controller</h1>
                        <p class="text-xs text-zinc-500">Dyno-style granular permissions</p>
                    </div>
                    <button onclick="saveModuleConfiguration()" id="saveBtn" class="px-5 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 text-white font-semibold text-xs transition shadow-lg shadow-blue-600/20 flex items-center gap-2">
                        <i class="fa-solid fa-cloud-arrow-up"></i><span>Save Configuration</span>
                    </button>
                </header>

                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-4xl mx-auto">
                        <div class="mb-6 p-4 rounded-xl bg-zinc-900/60 border border-zinc-800 flex items-start gap-3">
                            <i class="fa-solid fa-circle-info text-blue-400 mt-0.5 text-sm"></i>
                            <p class="text-xs text-zinc-400 leading-relaxed">
                                Disabling an entire category blocks all its commands immediately. Unchecking an individual command blocks only that specific action while leaving the parent module active.
                            </p>
                        </div>
                        {modules_html}
                    </div>
                </div>
            </main>

            <script>
                function toggleAccordion(cogName) {{
                    const body = document.getElementById('body-' + cogName);
                    const arrow = document.getElementById('arrow-' + cogName);
                    if (body.classList.contains('hidden')) {{
                        body.classList.remove('hidden'); arrow.classList.add('rotate-90');
                    }} else {{
                        body.classList.add('hidden'); arrow.classList.remove('rotate-90');
                    }}
                }}

                document.querySelectorAll('.cog-toggle').forEach(cogToggle => {{
                    cogToggle.addEventListener('change', function() {{
                        const cogName = this.value;
                        const isChecked = this.checked;
                        const card = document.getElementById('cog-card-' + cogName);
                        if (isChecked) card.classList.remove('opacity-60');
                        else card.classList.add('opacity-60');

                        document.querySelectorAll(`.cmd-toggle[data-cog="${{cogName}}"]`).forEach(cmdToggle => {{
                            cmdToggle.checked = isChecked;
                        }});
                    }});
                }});

                async function saveModuleConfiguration() {{
                    const btn = document.getElementById('saveBtn');
                    const originalHtml = btn.innerHTML;
                    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i><span>Saving...</span>';
                    btn.disabled = true;

                    const disabledCogs = [];
                    document.querySelectorAll('.cog-toggle:not(:checked)').forEach(t => disabledCogs.push(t.value));
                    const disabledCmds = [];
                    document.querySelectorAll('.cmd-toggle:not(:checked)').forEach(t => disabledCmds.push(t.value));

                    try {{
                        const res = await fetch('/api/modules/{guild_id}', {{
                            method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ disabled_cogs: disabledCogs, disabled_cmds: disabledCmds }})
                        }});
                        if (res.ok) {{
                            btn.innerHTML = '<i class="fa-solid fa-check"></i><span>Saved Successfully</span>';
                            btn.classList.remove('bg-blue-600', 'hover:bg-blue-500');
                            btn.classList.add('bg-emerald-600');
                            setTimeout(() => {{
                                btn.innerHTML = originalHtml;
                                btn.classList.remove('bg-emerald-600');
                                btn.classList.add('bg-blue-600', 'hover:bg-blue-500');
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
                    "disabled_cmds": data.get("disabled_cmds", [])
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
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <span class="font-bold text-sm text-white">Server Overview</span>
                </header>
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-4xl mx-auto space-y-6">
                        <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-6">
                            <div class="flex items-center gap-5">
                                <img src="{guild_icon}" class="w-20 h-20 rounded-2xl border border-zinc-800" alt="{guild.name}">
                                <div>
                                    <h1 class="text-2xl font-bold text-white mb-1">{guild.name}</h1>
                                    <p class="text-xs font-mono text-zinc-500 mb-3">ID: {guild.id}</p>
                                    <div class="flex items-center gap-3">
                                        <span class="px-2.5 py-1 rounded-lg bg-zinc-900 border border-zinc-800 text-xs font-mono text-zinc-300">
                                            <i class="fa-solid fa-users mr-1.5 text-zinc-500"></i> {guild.member_count:,} Members
                                        </span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-3 gap-5">
                            <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-5">
                                <div class="text-xs font-mono text-zinc-500 uppercase mb-2">Automod Status</div>
                                <div class="text-xl font-bold text-white">{"Active" if settings.get("automod_enabled", True) else "Disabled"}</div>
                            </div>
                            <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-5">
                                <div class="text-xs font-mono text-zinc-500 uppercase mb-2">Lockdown</div>
                                <div class="text-xl font-bold {"text-red-400" if settings.get("lockdown_active", False) else "text-emerald-400"}">
                                    {"ENGAGED" if settings.get("lockdown_active", False) else "SECURE"}
                                </div>
                            </div>
                            <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-5">
                                <div class="text-xs font-mono text-zinc-500 uppercase mb-2">Default AI Core</div>
                                <div class="text-xl font-bold text-white capitalize">{settings.get("default_ai_model", "nexusify")}</div>
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

        pills = "".join(f'<span class="px-2.5 py-1 rounded-lg bg-zinc-900 border border-zinc-800 text-xs font-mono text-red-400">{w}</span>' for w in banned_words if w.strip())
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
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <span class="font-bold text-sm text-white">Automated Content Filtration</span>
                </header>
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-4xl mx-auto space-y-6">
                        <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-6">
                            <h2 class="font-bold text-white text-base mb-2">Banned Lexicon</h2>
                            <p class="text-xs text-zinc-400 mb-4">Enter exact words or phrases to filter automatically. Separate items with commas.</p>
                            <textarea id="bannedWords" rows="4" class="w-full bg-[#09090b] border border-zinc-800 rounded-xl p-4 text-xs font-mono text-zinc-200 focus:outline-none focus:border-blue-500 transition">{words_string}</textarea>
                            <div class="mt-4 flex justify-end">
                                <button onclick="saveBannedWords()" id="saveWordsBtn" class="px-5 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 text-white font-semibold text-xs transition">Update Filters</button>
                            </div>
                        </div>
                        <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-6">
                            <h3 class="font-bold text-white text-sm mb-3">Active Filters Loaded</h3>
                            <div class="flex flex-wrap gap-2">{pills if pills else '<span class="text-xs text-zinc-600">No active word restrictions.</span>'}</div>
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
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <span class="font-bold text-sm text-white">Emergency Protocols</span>
                </header>
                <div class="flex-1 overflow-y-auto p-8 flex items-center justify-center">
                    <div class="max-w-md w-full bg-[#121215] border border-zinc-800/80 rounded-2xl p-8 text-center">
                        <div class="w-16 h-16 mx-auto rounded-2xl {'bg-red-500/10 border border-red-500/20 text-red-500' if is_locked else 'bg-emerald-500/10 border border-emerald-500/20 text-emerald-500'} flex items-center justify-center mb-5 text-2xl">
                            <i class="fa-solid {'fa-lock' if is_locked else 'fa-lock-open'}"></i>
                        </div>
                        <h2 class="text-xl font-bold text-white mb-1">Emergency Lockdown</h2>
                        <p class="text-xs text-zinc-400 mb-6 leading-relaxed">
                            Severing send permissions from the <code>@everyone</code> role freezes conversation channels instantaneously to curb raids.
                        </p>
                        <button onclick="toggleLockdown({str(not is_locked).lower()})" id="lockBtn" class="w-full py-3 rounded-xl font-semibold text-xs text-white transition {'bg-emerald-600 hover:bg-emerald-500' if is_locked else 'bg-red-600 hover:bg-red-500'}">
                            {"Lift Server Lockdown" if is_locked else "Engage Server Lockdown"}
                        </button>
                    </div>
                </div>
            </main>
            <script>
                async function toggleLockdown(targetState) {{
                    if (targetState && !confirm("ENGAGE LOCKDOWN: This revokes Send Messages from @everyone. Proceed?")) return;
                    const btn = document.getElementById('lockBtn'); btn.innerText = 'Processing...';
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
                <tr class="border-b border-zinc-800/60 text-xs text-zinc-300">
                    <td class="py-3 px-4 font-mono text-zinc-400">{ts}</td>
                    <td class="py-3 px-4 font-semibold text-white">{log.get('action')}</td>
                    <td class="py-3 px-4 font-mono text-zinc-400">{log.get('user_id')}</td>
                    <td class="py-3 px-4 truncate max-w-xs">{log.get('content', '')}</td>
                </tr>
                """

        if not rows_html:
            rows_html = '<tr><td colspan="4" class="py-8 text-center text-xs text-zinc-500">No telemetry logged yet.</td></tr>'

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
        </head>
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <span class="font-bold text-sm text-white">Automated Security Audits</span>
                </header>
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-5xl mx-auto bg-[#121215] border border-zinc-800/80 rounded-2xl overflow-hidden">
                        <table class="w-full text-left">
                            <thead class="bg-zinc-900/60 border-b border-zinc-800 text-[11px] font-mono text-zinc-400 uppercase">
                                <tr>
                                    <th class="py-3 px-4">Timestamp</th>
                                    <th class="py-3 px-4">Action</th>
                                    <th class="py-3 px-4">Subject</th>
                                    <th class="py-3 px-4">Payload</th>
                                </tr>
                            </thead>
                            <tbody>{rows_html}</tbody>
                        </table>
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
        </head>
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <span class="font-bold text-sm text-white">Setup Wizard</span>
                </header>
                <div class="flex-1 overflow-y-auto p-8 flex items-center justify-center">
                    <div class="max-w-2xl w-full bg-[#121215] border border-zinc-800/80 rounded-2xl p-8">
                        <div class="mb-6">
                            <h2 class="text-xl font-bold text-white">Select Default AI Core</h2>
                            <p class="text-xs text-zinc-400 mt-1">Choose the primary generative model for chat interactions in this server.</p>
                        </div>
                        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
                            <label class="flex flex-col p-4 rounded-xl border border-zinc-800 bg-[#09090b] cursor-pointer hover:border-blue-500 transition group relative overflow-hidden">
                                <input type="radio" name="ai_model" value="nexusify" class="absolute right-4 top-4 text-blue-600 bg-zinc-800" {nexusify_checked}>
                                <i class="fa-solid fa-network-wired text-xl text-blue-400 mb-2"></i>
                                <span class="text-white font-bold text-sm">Nexusify</span>
                                <span class="text-zinc-500 text-[10px] mt-1">Grok-3 architecture</span>
                            </label>
                            <label class="flex flex-col p-4 rounded-xl border border-zinc-800 bg-[#09090b] cursor-pointer hover:border-blue-500 transition group relative overflow-hidden">
                                <input type="radio" name="ai_model" value="gemini" class="absolute right-4 top-4 text-blue-600 bg-zinc-800" {gemini_checked}>
                                <i class="fa-brands fa-google text-xl text-emerald-400 mb-2"></i>
                                <span class="text-white font-bold text-sm">Gemini</span>
                                <span class="text-zinc-500 text-[10px] mt-1">Flash 2.5 vision</span>
                            </label>
                            <label class="flex flex-col p-4 rounded-xl border border-zinc-800 bg-[#09090b] cursor-pointer hover:border-blue-500 transition group relative overflow-hidden">
                                <input type="radio" name="ai_model" value="sarvam" class="absolute right-4 top-4 text-blue-600 bg-zinc-800" {sarvam_checked}>
                                <i class="fa-solid fa-language text-xl text-orange-400 mb-2"></i>
                                <span class="text-white font-bold text-sm">Sarvam</span>
                                <span class="text-zinc-500 text-[10px] mt-1">Multilingual text</span>
                            </label>
                        </div>
                        <div class="flex justify-end">
                            <button onclick="saveSetup()" id="saveBtn" class="px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-500 text-white font-semibold text-xs transition">Save Configuration</button>
                        </div>
                    </div>
                </div>
            </main>
            <script>
                async function saveSetup() {{
                    const btn = document.getElementById('saveBtn'); btn.innerText = 'Saving...';
                    const model = document.querySelector('input[name="ai_model"]:checked').value;
                    await fetch('/api/settings/{guild_id}', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ action: 'update_ai_model', model: model }})
                    }});
                    btn.innerText = 'Saved!';
                    setTimeout(() => {{ btn.innerText = 'Save Configuration'; }}, 2000);
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
                .toggle-checkbox:checked {{ right: 0; border-color: #2563eb; }}
                .toggle-checkbox:checked + .toggle-label {{ background-color: #2563eb; }}
            </style>
        </head>
        <body class="bg-[#09090b] text-zinc-100 h-screen flex overflow-hidden font-sans">
            {sidebar_html}
            <main class="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b]">
                <header class="h-16 border-b border-zinc-800/80 px-8 flex items-center justify-between shrink-0">
                    <span class="font-bold text-sm text-white">Miscellaneous Utilities</span>
                </header>
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-3xl mx-auto bg-[#121215] border border-zinc-800/80 rounded-2xl p-6">
                        <div class="flex justify-between items-center mb-6 border-b border-zinc-800 pb-4">
                            <div>
                                <h3 class="text-white font-bold text-sm">Dynamic AFK & Information Tools</h3>
                                <p class="text-xs text-zinc-400 mt-1">Enables <code>/afk</code>, <code>/whois</code>, and <code>/serverinfo</code>.</p>
                            </div>
                            <div class="relative inline-block w-10 align-middle select-none transition duration-200 ease-in ml-4">
                                <input type="checkbox" id="toggleMisc" {misc_checked} class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-zinc-600"/>
                                <label class="toggle-label block overflow-hidden h-5 rounded-full bg-zinc-700 cursor-pointer transition-colors duration-300"></label>
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
                        this.style.borderColor = '#2563eb'; label.style.backgroundColor = '#2563eb';
                    }} else {{
                        this.style.right = 'auto'; this.style.left = '0';
                        this.style.borderColor = '#52525b'; label.style.backgroundColor = '#3f3f46';
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
    # OWNER PANEL & IP FIREWALL (Restored Discord User Mappings)
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
            <div class="flex items-center justify-between py-2 border-b border-zinc-800/60 last:border-0 text-xs">
                <span class="font-mono text-zinc-300">{ext}</span>
                <button onclick="reloadModule('{clean}')" class="px-2.5 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-200 transition font-mono">
                    Reload
                </button>
            </div>
            """

        ip_rows = ""
        if hasattr(self.bot, 'db'):
            async for v in self.bot.db.visit_logs.find().sort("last_visit", -1).limit(30):
                last_user = v.get('last_user', 'Guest')
                user_badge_color = "bg-violet-500/10 text-violet-400 border-violet-500/20" if last_user != 'Guest' else "bg-zinc-800 text-zinc-400 border-zinc-700"
                ip_rows += f"""
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/60 last:border-0 text-xs filter-row">
                    <div>
                        <span class="font-mono text-zinc-300 ip-text">{v.get('ip')}</span>
                        <span class="ml-2 text-[10px] px-2 py-0.5 rounded border {user_badge_color} user-text">{last_user}</span>
                        <div class="text-[10px] text-zinc-500 mt-1">Hits: {v.get('hits', 1)}</div>
                    </div>
                    <button onclick="ipAction('ban', '{v.get('ip')}')" class="px-3 py-1.5 rounded bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition font-semibold">
                        Ban
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
        <body class="bg-[#09090b] text-zinc-100 min-h-screen p-8">
            <div class="max-w-6xl mx-auto space-y-6">
                <div class="flex items-center justify-between border-b border-zinc-800 pb-5">
                    <div>
                        <h1 class="text-xl font-bold text-white">System Override Console</h1>
                        <p class="text-xs text-zinc-500 font-mono">Guilds: {len(self.bot.guilds)} | Cogs: {len(self.bot.cogs)}</p>
                    </div>
                    <a href="/" class="text-xs text-zinc-400 hover:text-white">Exit to Home</a>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-6">
                        <h2 class="font-bold text-white text-sm mb-4">Hot-Reload Cogs</h2>
                        <div class="overflow-y-auto max-h-[500px] pr-2 custom-scrollbar">
                            {cogs_html}
                        </div>
                    </div>

                    <div class="bg-[#121215] border border-zinc-800/80 rounded-2xl p-6 flex flex-col h-[580px]">
                        <h2 class="font-bold text-white text-sm mb-2">Ingress Telemetry</h2>
                        <input type="text" id="ipSearch" placeholder="Search IP or Discord Username..." class="w-full bg-[#09090b] border border-zinc-800 rounded-lg p-2.5 text-xs text-white mb-4 focus:outline-none focus:border-blue-500 transition">
                        <div class="overflow-y-auto flex-1 pr-2 custom-scrollbar" id="ipContainer">
                            {ip_rows if ip_rows else '<p class="text-xs text-zinc-600">No logged hits.</p>'}
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
                .custom-scrollbar::-webkit-scrollbar {{ width: 6px; }}
                .custom-scrollbar::-webkit-scrollbar-track {{ background: transparent; }}
                .custom-scrollbar::-webkit-scrollbar-thumb {{ background: #27272a; border-radius: 3px; }}
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
