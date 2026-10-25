import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
import discord

from cogs.dashboard import Dashboard, normalize_ip


class AsyncCursorMock:
    def __init__(self, items=None):
        self.items = items or []

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for item in self.items:
            yield item


class TestDashboardRoutes(AioHTTPTestCase):
    async def get_application(self):
        self.fake_bot = MagicMock()
        self.fake_bot.dashboard_maintenance = False
        self.fake_bot.user = MagicMock()
        self.fake_bot.user.display_avatar.url = "https://cdn.discordapp.com/avatars/1/avatar.png"
        self.fake_bot.cogs = {}
        self.fake_bot.guilds = []
        self.fake_bot.owner_id = 123456789
        self.fake_bot.application_info = AsyncMock(return_value=MagicMock(owner=MagicMock(id=123456789)))
        self.fake_bot.extensions = {"cogs.core": MagicMock()}

        # Database mock
        self.fake_db = MagicMock()
        self.fake_bot.db = self.fake_db
        self.fake_db.ip_bans.find_one = AsyncMock(return_value=None)
        self.fake_db.sessions.find_one = AsyncMock(return_value={
            "session_id": "valid_session",
            "discord_id": "123456789",
            "username": "TestUser",
            "created_at": time.time(),
        })
        self.fake_db.guild_settings.find_one = AsyncMock(return_value={
            "guild_id": 999999,
            "banned_words": ["badword1", "badword2"],
            "default_ai_model": "nexusify",
            "misc_enabled": True,
            "lockdown_active": False,
            "disabled_cogs": [],
            "command_settings": {},
        })
        self.fake_db.guild_settings.update_one = AsyncMock(return_value=True)
        self.fake_db.security_logs.find.return_value = AsyncCursorMock([
            {"timestamp": time.time(), "action": "LOCKDOWN", "user_id": "123456789", "content": "Engaged"}
        ])
        self.fake_db.visit_logs.update_one = AsyncMock(return_value=True)
        self.fake_db.visit_logs.find.return_value = AsyncCursorMock([])

        # Guild setup
        self.guild_id = 999999
        self.fake_guild = MagicMock(spec=discord.Guild)
        self.fake_guild.id = self.guild_id
        self.fake_guild.name = "Test Guild"
        self.fake_guild.icon = None
        self.fake_guild.member_count = 50
        self.fake_guild.members = []
        self.fake_guild.text_channels = []
        self.fake_guild.roles = []
        self.fake_guild.default_role = MagicMock()
        self.fake_guild.default_role.edit = AsyncMock()
        self.fake_guild.owner_id = 111111111  # Someone else

        self.fake_bot.get_guild.return_value = self.fake_guild

        # Suppress start_server port binding during init
        def fake_create_task(coro):
            coro.close()
            return MagicMock()
        self.fake_bot.loop.create_task = fake_create_task

        async def dummy_start_server(dash_self):
            return None

        with patch.object(Dashboard, 'start_server', dummy_start_server):
            self.dashboard_cog = Dashboard(self.fake_bot)

        return self.dashboard_cog.app

    def setUp(self):
        super().setUp()
        self.cookies = {"recluse_session": "valid_session", "recluse_verified": "true"}

    def _set_admin_role(self, is_admin: bool):
        if is_admin:
            fake_member = MagicMock(spec=discord.Member)
            fake_member.guild_permissions = discord.Permissions(administrator=True)
            self.fake_guild.get_member.return_value = fake_member
            self.fake_guild.fetch_member = AsyncMock(return_value=fake_member)
        else:
            self.fake_guild.get_member.return_value = None
            self.fake_guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), 'Not Found'))

    @unittest_run_loop
    async def test_manage_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/manage/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/manage admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/manage/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/manage non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_modules_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/modules/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/modules admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/modules/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/modules non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_automod_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/automod/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/automod admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/automod/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/automod non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_lockdown_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/lockdown/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/lockdown admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/lockdown/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/lockdown non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_logs_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/logs/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/logs admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/logs/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/logs non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_wizard_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/wizard/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/wizard admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/wizard/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/wizard non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_misc_route(self):
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.get(f'/misc/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/misc admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.get(f'/misc/{self.guild_id}', cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"/misc non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_api_settings_route(self):
        payload = {"action": "update_automod", "words": "spam, test"}
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.post(f'/api/settings/{self.guild_id}', json=payload, cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"POST /api/settings admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.post(f'/api/settings/{self.guild_id}', json=payload, cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"POST /api/settings non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_api_modules_route(self):
        payload = {"disabled_cogs": [], "command_settings": {}}
        # Admin: expect 200
        self._set_admin_role(True)
        resp = await self.client.post(f'/api/modules/{self.guild_id}', json=payload, cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"POST /api/modules admin failed with {resp.status}")

        # Non-member: expect 403
        self._set_admin_role(False)
        resp = await self.client.post(f'/api/modules/{self.guild_id}', json=payload, cookies=self.cookies)
        self.assertEqual(resp.status, 403, f"POST /api/modules non-member failed with {resp.status}")

    @unittest_run_loop
    async def test_home_route(self):
        # Home route without session
        resp = await self.client.get('/')
        self.assertEqual(resp.status, 200, f"/ without session failed with {resp.status}")

        # Home route with session
        resp = await self.client.get('/', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/ with session failed with {resp.status}")

    @unittest_run_loop
    async def test_owner_panel_route(self):
        # Owner: expect 200
        resp = await self.client.get('/owner_panel', cookies=self.cookies)
        self.assertEqual(resp.status, 200, f"/owner_panel owner failed with {resp.status}")

        # Non-owner: expect 403
        non_owner_cookies = {"recluse_session": "non_owner_session", "recluse_verified": "true"}
        async def mock_sessions_find_one(filter_dict):
            sid = filter_dict.get("session_id")
            if sid == "non_owner_session":
                return {
                    "session_id": "non_owner_session",
                    "discord_id": "999888777",
                    "username": "NonOwner",
                    "created_at": time.time(),
                }
            return {
                "session_id": "valid_session",
                "discord_id": "123456789",
                "username": "TestUser",
                "created_at": time.time(),
            }
        self.fake_db.sessions.find_one = AsyncMock(side_effect=mock_sessions_find_one)
        resp = await self.client.get('/owner_panel', cookies=non_owner_cookies)
        self.assertEqual(resp.status, 403, f"/owner_panel non-owner failed with {resp.status}")


class TestNormalizeIpHelper(unittest.TestCase):
    def test_normalize_ip_inputs(self):
        test_cases = [
            ("1.2.3.4", "1.2.3.4"),
            ("::1", "::1"),
            ("fe80::1%eth0", "invalid"),
            ("fe80::1%<img src=x onerror=alert(1)>", "invalid"),
            ("fe80::1%x');alert(1);'", "invalid"),
            ("not-an-ip", "invalid"),
        ]
        for input_val, expected in test_cases:
            with self.subTest(input_val=input_val):
                result = normalize_ip(input_val)
                self.assertEqual(result, expected, f"Failed for input {input_val!r}: got {result!r}, expected {expected!r}")


if __name__ == '__main__':
    unittest.main()
