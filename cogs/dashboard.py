import os
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

DISCORD_API_URL = "https://discord.com/api/v10"
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DASHBOARD_REDIRECT_URI", "https://dashboard.recluse-app.workers.dev/api/auth/callback")

class DashboardAPI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = web.Application()
        self.setup_routes()
        self.runner = None
        self.bot.loop.create_task(self.start_server())

    def setup_routes(self):
        # Health & Bot Status
        self.app.router.add_get("/api/status", self.get_status)

        # Discord OAuth
        self.app.router.add_get("/api/auth/login", self.oauth_login)
        self.app.router.add_get("/api/auth/callback", self.oauth_callback)
        self.app.router.add_post("/api/auth/logout", self.oauth_logout)

        # Current User & Guilds
        self.app.router.add_get("/api/me", self.get_me)
        self.app.router.add_get("/api/guilds/{id}", self.get_guild_details)
        self.app.router.add_post("/api/guilds/{id}/settings", self.update_guild_settings)
        self.app.router.add_post("/api/guilds/{id}/lockdown", self.toggle_lockdown)

    async def start_server(self):
        await self.bot.wait_until_ready()
        port = int(os.getenv("PORT", 8080))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        print(f"[Dashboard API] Server online on port {port}")

    def cog_unload(self):
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())

    # --- Endpoints ---

    async def get_status(self, request):
        return web.json_response({
            "online": True,
            "latency_ms": round(self.bot.latency * 1000, 1),
            "guilds_count": len(self.bot.guilds),
            "users_count": len(self.bot.users)
        })

    async def oauth_login(self, request):
        discord_url = (
            f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify%20guilds"
        )
        return web.HTTPFound(discord_url)

    async def oauth_callback(self, request):
        code = request.query.get("code")
        if not code:
            return web.HTTPBadRequest(text="Missing OAuth code")

        # Exchange code for access token
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{DISCORD_API_URL}/oauth2/token", data=data) as resp:
                if resp.status != 200:
                    return web.HTTPUnauthorized(text="Failed to exchange OAuth token")
                token_data = await resp.json()

        response = web.HTTPFound("/")
        response.set_cookie(
            "recluse_session",
            token_data["access_token"],
            max_age=token_data.get("expires_in", 604800),
            httponly=True,
            secure=True,
            samesite="Lax"
        )
        return response

    async def oauth_logout(self, request):
        response = web.json_response({"success": True})
        response.del_cookie("recluse_session")
        return response

    async def get_me(self, request):
        token = request.cookies.get("recluse_session")
        if not token:
            return web.json_response({"authenticated": False})

        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            # 1. Fetch user profile
            async with session.get(f"{DISCORD_API_URL}/users/@me", headers=headers) as user_resp:
                if user_resp.status != 200:
                    return web.json_response({"authenticated": False})
                user_info = await user_resp.json()

            # 2. Fetch user's servers
            async with session.get(f"{DISCORD_API_URL}/users/@me/guilds", headers=headers) as guilds_resp:
                user_guilds = await guilds_resp.json() if guilds_resp.status == 200 else []

        # Filter to mutual guilds where user has MANAGE_GUILD (0x20) or ADMINISTRATOR (0x8)
        mutual_guilds = []
        for g in user_guilds:
            perms = int(g.get("permissions", 0))
            is_admin = (perms & 0x8 == 0x8) or (perms & 0x20 == 0x20) or g.get("owner", False)
            if not is_admin:
                continue

            bot_guild = self.bot.get_guild(int(g["id"]))
            mutual_guilds.append({
                "id": g["id"],
                "name": g["name"],
                "icon": f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else "https://cdn.discordapp.com/embed/avatars/0.png",
                "member_count": bot_guild.member_count if bot_guild else 0,
                "in_server": bot_guild is not None
            })

        return web.json_response({
            "authenticated": True,
            "user": {
                "discord_id": user_info["id"],
                "username": user_info["username"],
                "avatar": f"https://cdn.discordapp.com/avatars/{user_info['id']}/{user_info['avatar']}.png" if user_info.get("avatar") else "https://cdn.discordapp.com/embed/avatars/0.png",
                "logged_in": True,
                "is_owner": str(user_info["id"]) == os.getenv("OWNER_ID", "")
            },
            "guilds": mutual_guilds
        })

    async def get_guild_details(self, request):
        guild_id = int(request.match_info["id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.HTTPNotFound(text="Bot is not in this guild")

        channels = [{"id": str(c.id), "name": c.name, "type": "voice" if isinstance(c, discord.VoiceChannel) else "text"} for c in guild.channels]
        roles = [{"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}"} for r in guild.roles if not r.is_default()]

        return web.json_response({
            "id": str(guild.id),
            "name": guild.name,
            "icon": guild.icon.url if guild.icon else "",
            "member_count": guild.member_count,
            "in_server": True,
            "channels": channels,
            "roles": roles
        })

    async def update_guild_settings(self, request):
        guild_id = int(request.match_info["id"])
        payload = await request.json()
        # Save payload to your existing database here (PostgreSQL, SQLite, Mongo, etc.)
        return web.json_response({"success": True})

    async def toggle_lockdown(self, request):
        guild_id = int(request.match_info["id"])
        data = await request.json()
        active = data.get("active", False)

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.HTTPNotFound(text="Guild not found")

        # Real Discord action: update @everyone send_messages permission
        everyone_role = guild.default_role
        for channel in guild.text_channels:
            try:
                await channel.set_permissions(everyone_role, send_messages=(False if active else None))
            except Exception:
                pass

        return web.json_response({"success": True, "active": active})

async def setup(bot):
    await bot.add_cog(DashboardAPI(bot))
