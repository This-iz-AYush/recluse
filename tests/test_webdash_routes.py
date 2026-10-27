"""
tests/test_webdash_routes.py — Integration tests for Phase A1 Piece 2 routes:
- GET /health (no DB call, NaN/inf latency handling, ?strict=1 -> 503 when not ready)
- GET /debug/ip (404 when disabled; Owner 200 / Non-Owner 403 / Logged-out 302 when WEBDASH_DEBUG_IP=1)
- OAuth2 & Session lifecycle: /login state cookie, /callback state mismatch 400,
  session rotation on login (revoking old presented cookie), Cookie flags (HttpOnly,
  Secure on X-Forwarded-Proto: https, SameSite=Lax, Path=/, Max-Age=86400),
  POST /logout CSRF enforcement & server-side revocation
- GET / & GET /servers (landing page fast-path, server picker, XSS autoescaping, live guild check)
- GET /manage/{guild_id} (Admin 200 / Custom Role 200 via fetch_member TTL cache /
  Non-member 403 even if session claims permissions=8 / Logged-out 302 / 404 / 400)
- Zero inline event handlers across rendered HTML templates
- Junk-cookie flood against full webdash app -> bounded MongoDB session lookups
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from aiohttp.test_utils import AioHTTPTestCase

from webdash.app import create_webdash_app
from webdash.auth import (
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SessionManager,
    hash_session_token,
)
from webdash.security import InMemoryRateLimiter

INLINE_HANDLER_RE = re.compile(r"\bon(?:click|change|submit|load|error|mouseover|keydown|keyup|input)\s*=", re.IGNORECASE)


class _InMemorySessionCollection:
    """In-memory fake for db.webdash_sessions to test full session lifecycle."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.find_one_calls = 0
        self.insert_one = AsyncMock(side_effect=self._insert_one)
        self.find_one = AsyncMock(side_effect=self._find_one)
        self.update_one = AsyncMock(side_effect=self._update_one)
        self.delete_one = AsyncMock(side_effect=self._delete_one)
        self.delete_many = AsyncMock(side_effect=self._delete_many)
        self.create_index = AsyncMock(return_value="ok")

    async def _insert_one(self, doc: dict):
        self.docs[doc["session_id_hash"]] = dict(doc)
        return MagicMock(inserted_id=doc["session_id_hash"])

    async def _find_one(self, query: dict):
        self.find_one_calls += 1
        h = query.get("session_id_hash")
        if h in self.docs:
            return dict(self.docs[h])
        return None

    async def _update_one(self, query: dict, update: dict):
        h = query.get("session_id_hash")
        if h in self.docs and "$set" in update:
            self.docs[h].update(update["$set"])
        return MagicMock(modified_count=1)

    async def _delete_one(self, query: dict):
        h = query.get("session_id_hash")
        existed = self.docs.pop(h, None) is not None
        return MagicMock(deleted_count=1 if existed else 0)

    async def _delete_many(self, query: dict):
        uid = query.get("discord_id")
        to_del = [k for k, v in self.docs.items() if v.get("discord_id") == uid]
        for k in to_del:
            self.docs.pop(k, None)
        return MagicMock(deleted_count=len(to_del))


class TestWebdashRoutesPiece2(AioHTTPTestCase):
    async def get_application(self):
        os.environ["DISCORD_CLIENT_ID"] = "test_client_id_123"
        os.environ["DISCORD_CLIENT_SECRET"] = "test_client_secret_456"
        os.environ["REDIRECT_URI"] = "https://recluse.example.com/callback"

        self.bot_owner_id = 100000001
        self.admin_user_id = 200000002
        self.mod_user_id = 300000003
        self.outsider_user_id = 400000004
        self.guild_id = 999888777

        self.fake_bot = MagicMock()
        self.fake_bot.db_ready = True
        self.fake_bot.owner_id = self.bot_owner_id
        self.fake_bot.is_ready = MagicMock(return_value=True)
        self.fake_bot.is_closed = MagicMock(return_value=False)
        self.fake_bot.latency = 0.042
        self.fake_bot.user = MagicMock()
        self.fake_bot.user.display_avatar.url = "https://cdn.discordapp.com/avatars/1/bot.webp"

        # Database mock
        self.fake_db = MagicMock()
        self.fake_bot.db = self.fake_db
        self.session_coll = _InMemorySessionCollection()
        self.fake_db.webdash_sessions = self.session_coll

        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "automod_enabled": True,
            "banned_words": ["spam1", "spam2"],
            "default_ai_model": "deepseek",
            "ai_enabled": True,
            "welcome_enabled": True,
            "leveling_enabled": True,
            "mod_enabled": True,
            "autoresponse_enabled": True,
            "sports_enabled": False,
            "misc_enabled": True,
            "disabled_cogs": ["sports"],
            "command_settings": {"ban": {"disabled": False}},
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {"allow": ["overview.view", "automod.*"], "deny": []},
                }
            },
        }
        self.fake_db.guild_settings.find_one = AsyncMock(
            side_effect=lambda q: dict(self.guild_settings_doc)
            if q.get("guild_id") == self.guild_id
            else None
        )

        # Guild mock
        self.fake_guild = MagicMock(spec=discord.Guild)
        self.fake_guild.id = self.guild_id
        self.fake_guild.name = "Recluse <script>alert(1)</script> HQ"
        self.fake_guild.icon = None
        self.fake_guild.member_count = 1234
        self.fake_guild.channels = [MagicMock(), MagicMock(), MagicMock()]
        self.fake_guild.roles = [MagicMock(), MagicMock()]
        self.fake_guild.owner_id = 888888888

        # Members setup
        self.admin_member = MagicMock(spec=discord.Member)
        self.admin_member.id = self.admin_user_id
        self.admin_member.guild_permissions = discord.Permissions(administrator=True)
        role_admin = MagicMock()
        role_admin.id = 111
        self.admin_member.roles = [role_admin]

        self.mod_member = MagicMock(spec=discord.Member)
        self.mod_member.id = self.mod_user_id
        self.mod_member.guild_permissions = discord.Permissions.none()
        role_mod = MagicMock()
        role_mod.id = 555  # Granted overview.view in dashboard_permissions!
        self.mod_member.roles = [role_mod]

        def get_member_side_effect(uid: int):
            if uid == self.admin_user_id:
                return self.admin_member
            # Return None for mod_user_id so we test the fetch_member + TTL cache path!
            return None

        async def fetch_member_side_effect(uid: int):
            if uid == self.mod_user_id:
                return self.mod_member
            raise discord.NotFound(MagicMock(), "Member not found")

        self.fake_guild.get_member = MagicMock(side_effect=get_member_side_effect)
        self.fake_guild.fetch_member = AsyncMock(side_effect=fetch_member_side_effect)

        self.fake_bot.guilds = [self.fake_guild]
        self.fake_bot.get_guild = MagicMock(
            side_effect=lambda gid: self.fake_guild if int(gid) == self.guild_id else None
        )

        # Mock OAuth exchanger for /callback tests
        self.oauth_user_to_return = {
            "id": str(self.admin_user_id),
            "username": "Admin<img src=x onerror=alert(1)>",
            "avatar": "avatarhash123",
        }
        self.oauth_guilds_to_return = [
            {
                "id": str(self.guild_id),
                "name": "Recluse HQ",
                "icon": None,
                "owner": False,
                "permissions": 8,
            },
            {
                "id": "777666555",
                "name": "Uninvited Server",
                "icon": None,
                "owner": True,
                "permissions": 8,
            },
        ]

        async def fake_oauth_client(_code: str):
            return self.oauth_user_to_return, self.oauth_guilds_to_return

        self.limiter = InMemoryRateLimiter(
            limits={
                "auth": (10, 60.0),
                "mutation": (20, 60.0),
                "page": (30, 60.0),
                "pre_session": (25, 60.0),
                "unidentified": (100, 60.0),
            }
        )
        self.session_manager = SessionManager(self.fake_bot)

        app = create_webdash_app(
            self.fake_bot,
            rate_limiter=self.limiter,
            session_manager=self.session_manager,
            oauth_client=fake_oauth_client,
            trusted_hops=1,
            origin_secret="test_worker_secret",
            enable_debug_ip=True,
        )
        return app

    async def _create_test_session(self, discord_id: int, username: str = "User") -> tuple[str, dict]:
        res = await self.session_manager.create_session(
            discord_id=discord_id,
            username=username,
            guilds=[
                # Intentionally include guild_id with permissions=8 even for outsider_user_id
                # to prove guild routes NEVER trust session's stored guild list!
                {"id": str(self.guild_id), "name": "Spoofed Session Guild", "permissions": 8, "owner": True},
            ],
            client_ip="203.0.113.10",
            user_agent="UnitTest/1.0",
        )
        assert res is not None
        return res

    async def test_health_route_ready_nan_inf_and_strict_mode(self):
        # 1. Normal ready bot -> 200, latency_ms=42, zero DB lookups
        initial_db_calls = self.session_coll.find_one_calls
        r1 = await self.client.get("/health")
        self.assertEqual(r1.status, 200)
        d1 = await r1.json()
        self.assertEqual(d1, {"status": "ok", "bot_ready": True, "latency_ms": 42})
        self.assertEqual(self.session_coll.find_one_calls, initial_db_calls)

        # 2. NaN and inf latency -> latency_ms is None (never raises ValueError/500)
        for weird_latency in (float("nan"), float("inf"), float("-inf"), -1.5, None):
            self.fake_bot.latency = weird_latency
            r_lat = await self.client.get("/health")
            self.assertEqual(r_lat.status, 200)
            d_lat = await r_lat.json()
            self.assertIsNone(d_lat["latency_ms"])

        # 3. Bot not ready: normal /health -> 200, /health?strict=1 -> 503
        self.fake_bot.is_ready.return_value = False
        self.fake_bot.latency = 0.050
        r_not_ready = await self.client.get("/health")
        self.assertEqual(r_not_ready.status, 200)
        self.assertEqual(
            await r_not_ready.json(),
            {"status": "ok", "bot_ready": False, "latency_ms": 50},
        )

        r_strict = await self.client.get("/health?strict=1")
        self.assertEqual(r_strict.status, 503)
        self.assertEqual(
            await r_strict.json(),
            {"status": "ok", "bot_ready": False, "latency_ms": 50},
        )

    async def test_debug_ip_route_permissions_and_disabled_default(self):
        # 1. Logged out -> 302 redirect to /login
        r_anon = await self.client.get("/debug/ip", allow_redirects=False)
        self.assertEqual(r_anon.status, 302)
        self.assertEqual(r_anon.headers.get("Location"), "/login")

        # 2. Non-owner (admin_user_id) -> 403
        admin_sid, _ = await self._create_test_session(self.admin_user_id, "Admin")
        r_non_owner = await self.client.get(
            "/debug/ip",
            cookies={SESSION_COOKIE_NAME: admin_sid},
            allow_redirects=False,
        )
        self.assertEqual(r_non_owner.status, 403)

        # 3. Bot Owner -> 200 with full IP diagnostic payload
        owner_sid, _ = await self._create_test_session(self.bot_owner_id, "Owner")
        r_owner = await self.client.get(
            "/debug/ip",
            headers={
                "X-Recluse-Origin-Secret": "test_worker_secret",
                "X-Recluse-Client-IP": "198.51.100.77",
                "X-Forwarded-For": "1.1.1.1, 198.51.100.77",
                "CF-Connecting-IP": "198.51.100.77",
                "CF-IPCountry": "IN",
            },
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner.status, 200)
        data = await r_owner.json()
        self.assertEqual(data["resolved_ip"], "198.51.100.77")
        self.assertTrue(data["ip_trusted"])
        self.assertEqual(data["trusted_source"], "origin_header")
        self.assertEqual(data["cf_headers"].get("CF-IPCountry"), "IN")

        # 4. Verify that when enable_debug_ip=False (default), /debug/ip is not registered (404)
        app_no_debug = create_webdash_app(self.fake_bot, enable_debug_ip=False)
        paths = [r.resource.canonical for r in app_no_debug.router.routes() if r.resource]
        self.assertNotIn("/debug/ip", paths)

    async def test_oauth_flow_session_rotation_cookie_flags_and_logout(self):
        # 1. GET /login over HTTPS sets Secure, HttpOnly, SameSite=Lax state cookie
        r_login = await self.client.get(
            "/login",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Recluse-Origin-Secret": "test_worker_secret",
                "X-Recluse-Client-IP": "203.0.113.10",
            },
            allow_redirects=False,
        )
        self.assertEqual(r_login.status, 302)
        loc = r_login.headers.get("Location", "")
        self.assertIn("discord.com/api/oauth2/authorize", loc)
        state_cookie = r_login.cookies.get(OAUTH_STATE_COOKIE_NAME)
        self.assertIsNotNone(state_cookie)
        state_val = state_cookie.value
        self.assertTrue(state_cookie["httponly"])
        self.assertTrue(state_cookie["secure"])
        self.assertEqual(state_cookie["samesite"], "Lax")
        self.assertEqual(state_cookie["path"], "/")

        # 2. GET /callback with mismatched or non-ASCII state -> 400 (never 500)
        r_bad_state = await self.client.get(
            "/callback?code=testcode&state=wrong_state",
            cookies={OAUTH_STATE_COOKIE_NAME: state_val},
            allow_redirects=False,
        )
        self.assertEqual(r_bad_state.status, 400)

        r_non_ascii_state = await self.client.get(
            "/callback?code=testcode&state=é",
            cookies={OAUTH_STATE_COOKIE_NAME: state_val},
            allow_redirects=False,
        )
        self.assertEqual(r_non_ascii_state.status, 400)

        # 3. Create an existing session to verify session rotation on /callback
        old_sid, _ = await self._create_test_session(self.outsider_user_id, "OldUser")
        old_hash = hash_session_token(old_sid)
        self.assertIn(old_hash, self.session_coll.docs)

        # 4. Valid GET /callback rotates session (revokes old_sid, issues new recluse_sid)
        r_cb = await self.client.get(
            f"/callback?code=valid_code&state={state_val}",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Recluse-Origin-Secret": "test_worker_secret",
                "X-Recluse-Client-IP": "203.0.113.10",
            },
            cookies={
                OAUTH_STATE_COOKIE_NAME: state_val,
                SESSION_COOKIE_NAME: old_sid,
            },
            allow_redirects=False,
        )
        self.assertEqual(r_cb.status, 302)
        self.assertEqual(r_cb.headers.get("Location"), "/")

        # Old session MUST be revoked in db.webdash_sessions!
        self.assertNotIn(old_hash, self.session_coll.docs)

        # New session cookie MUST be new and have HttpOnly, Secure, SameSite=Lax, Path=/, Max-Age=86400
        sid_cookie = r_cb.cookies.get(SESSION_COOKIE_NAME)
        self.assertIsNotNone(sid_cookie)
        new_sid = sid_cookie.value
        self.assertNotEqual(new_sid, old_sid)
        self.assertEqual(len(new_sid), 43)
        self.assertTrue(sid_cookie["httponly"])
        self.assertTrue(sid_cookie["secure"])
        self.assertEqual(sid_cookie["samesite"], "Lax")
        self.assertEqual(sid_cookie["path"], "/")
        self.assertEqual(int(sid_cookie["max-age"]), 86400)

        new_hash = hash_session_token(new_sid)
        self.assertIn(new_hash, self.session_coll.docs)
        csrf_token = self.session_coll.docs[new_hash]["csrf_token"]

        # 5. POST /logout without CSRF token -> 403, session still exists
        r_logout_no_csrf = await self.client.post(
            "/logout",
            cookies={SESSION_COOKIE_NAME: new_sid},
            allow_redirects=False,
        )
        self.assertEqual(r_logout_no_csrf.status, 403)
        self.assertIn(new_hash, self.session_coll.docs)

        # 6. POST /logout with valid CSRF token -> 302, session deleted in DB, cookie cleared
        r_logout_ok = await self.client.post(
            "/logout",
            data={"csrf_token": csrf_token},
            cookies={SESSION_COOKIE_NAME: new_sid},
            allow_redirects=False,
        )
        self.assertEqual(r_logout_ok.status, 302)
        self.assertNotIn(new_hash, self.session_coll.docs)

    async def test_home_and_servers_routes(self):
        # 1. Unauthenticated GET / -> 200 landing page, 0 session DB lookups, no inline handlers
        before_calls = self.session_coll.find_one_calls
        r_home_anon = await self.client.get("/")
        self.assertEqual(r_home_anon.status, 200)
        self.assertEqual(self.session_coll.find_one_calls, before_calls)
        body_anon = await r_home_anon.text()
        self.assertIn("Log In with Discord", body_anon)
        self.assertIsNone(INLINE_HANDLER_RE.search(body_anon))

        # 2. Unauthenticated GET /servers -> 302 to /login
        r_servers_anon = await self.client.get("/servers", allow_redirects=False)
        self.assertEqual(r_servers_anon.status, 302)
        self.assertEqual(r_servers_anon.headers.get("Location"), "/login")

        # 3. Authenticated GET / as Admin -> shows server picker with escaped XSS name
        admin_sid, _ = await self._create_test_session(
            self.admin_user_id,
            "Admin<script>evil()</script>",
        )
        r_home_auth = await self.client.get("/", cookies={SESSION_COOKIE_NAME: admin_sid})
        self.assertEqual(r_home_auth.status, 200)
        body_auth = await r_home_auth.text()
        self.assertIn("Select a Server", body_auth)
        self.assertNotIn("<script>evil()</script>", body_auth)
        self.assertNotIn("<script>alert(1)</script>", body_auth)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body_auth)
        self.assertIn(f"/manage/{self.guild_id}", body_auth)
        self.assertIsNone(INLINE_HANDLER_RE.search(body_auth))

        # 4. Authenticated GET / as Outsider (whose session claims permissions=8 in guild_id,
        #    but live bot.get_guild shows they are NOT a member) -> guild_id MUST NOT be shown!
        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")
        r_outsider = await self.client.get("/servers", cookies={SESSION_COOKIE_NAME: outsider_sid})
        self.assertEqual(r_outsider.status, 200)
        body_outsider = await r_outsider.text()
        self.assertNotIn(f"/manage/{self.guild_id}", body_outsider)

    async def test_guild_overview_route_rbac_and_live_member_cache(self):
        # 1. Logged out -> 302 redirect to /login
        r_anon = await self.client.get(f"/manage/{self.guild_id}", allow_redirects=False)
        self.assertEqual(r_anon.status, 302)
        self.assertEqual(r_anon.headers.get("Location"), "/login")

        # 2. Non-member (even with spoofed session guilds list) -> 403 Forbidden
        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")
        r_outsider = await self.client.get(
            f"/manage/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_outsider.status, 403)

        # 3. Discord Administrator -> 200 OK
        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        r_admin = await self.client.get(
            f"/manage/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_admin.status, 200)
        body_admin = await r_admin.text()
        self.assertIn("Server Overview", body_admin)
        self.assertIn("1,234 Members", body_admin)
        self.assertIn("deepseek", body_admin.lower())
        self.assertNotIn("<script>alert(1)</script>", body_admin)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body_admin)
        self.assertIsNone(INLINE_HANDLER_RE.search(body_admin))

        # 4. Member with custom dashboard_permissions role (mod_user_id, role 555)
        #    resolved via fetch_member fallback and cached in member_cache!
        mod_sid, _ = await self._create_test_session(self.mod_user_id, "ModUser")
        self.fake_guild.fetch_member.reset_mock()
        r_mod1 = await self.client.get(
            f"/manage/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_mod1.status, 200)
        self.assertEqual(self.fake_guild.fetch_member.await_count, 1)

        # Second request hits the 60s member_cache without calling fetch_member again!
        r_mod2 = await self.client.get(
            f"/manage/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_mod2.status, 200)
        self.assertEqual(self.fake_guild.fetch_member.await_count, 1)

        # 5. Missing guild -> 404; invalid guild ID -> 400
        r_missing = await self.client.get(
            "/manage/111222333",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_missing.status, 404)

        r_bad_id = await self.client.get(
            "/manage/not-a-snowflake",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_bad_id.status, 400)

    async def test_junk_cookie_flood_against_webdash_app_bounds_mongo_queries(self):
        # 1. 150 non-plausible junk cookies -> 0 MongoDB find_one calls!
        self.session_coll.find_one_calls = 0
        for i in range(150):
            await self.client.get(
                f"/manage/{self.guild_id}",
                headers={
                    "X-Recluse-Origin-Secret": "test_worker_secret",
                    "X-Recluse-Client-IP": "198.51.100.200",
                },
                cookies={SESSION_COOKIE_NAME: f"short_junk_{i}"},
                allow_redirects=False,
            )
        self.assertEqual(self.session_coll.find_one_calls, 0)

        # 2. 150 plausible 43-char random cookies -> bounded by pre_session limit (25)!
        self.session_coll.find_one_calls = 0
        statuses = []
        for _ in range(150):
            resp = await self.client.get(
                f"/manage/{self.guild_id}",
                headers={
                    "X-Recluse-Origin-Secret": "test_worker_secret",
                    "X-Recluse-Client-IP": "198.51.100.201",
                },
                cookies={SESSION_COOKIE_NAME: secrets.token_urlsafe(32)},
                allow_redirects=False,
            )
            statuses.append(resp.status)
        self.assertEqual(self.session_coll.find_one_calls, 25)
        self.assertIn(429, statuses)

    async def test_checkpoint_3a_http_exceptions_404_and_get_logout_405_have_security_headers(self):
        # 1. GET /nope -> 404 with security headers
        r_nope = await self.client.get("/nope")
        self.assertEqual(r_nope.status, 404)
        self.assertEqual(r_nope.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r_nope.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", r_nope.headers.get("Content-Security-Policy", ""))

        # 2. GET /logout (removed state-changing GET) -> 405 Method Not Allowed with security headers
        r_get_logout = await self.client.get("/logout")
        self.assertEqual(r_get_logout.status, 405)
        self.assertEqual(r_get_logout.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r_get_logout.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", r_get_logout.headers.get("Content-Security-Policy", ""))

    async def test_require_origin_shared_secret_blocks_direct_login_allows_health_and_home(self):
        with patch.dict(os.environ, {"REQUIRE_ORIGIN_SHARED_SECRET": "1"}):
            # 1. Direct-origin GET /health (no origin secret) -> 200 OK (cron pinger allowed)
            r_health = await self.client.get("/health")
            self.assertEqual(r_health.status, 200)

            # 2. Direct-origin unauthenticated GET / (no origin secret) -> 200 OK (cron pinger allowed)
            r_home = await self.client.get("/")
            self.assertEqual(r_home.status, 200)

            # 3. Direct-origin GET /login (missing or wrong origin secret) -> 403 Forbidden
            r_login_direct = await self.client.get("/login", allow_redirects=False)
            self.assertEqual(r_login_direct.status, 403)
            self.assertEqual(r_login_direct.headers.get("X-Frame-Options"), "DENY")

            r_login_wrong = await self.client.get(
                "/login",
                headers={"X-Recluse-Origin-Secret": "wrong_secret"},
                allow_redirects=False,
            )
            self.assertEqual(r_login_wrong.status, 403)

            # 4. Worker-proxied GET /login with valid X-Recluse-Origin-Secret -> 302 Redirect to Discord
            r_login_worker = await self.client.get(
                "/login",
                headers={
                    "X-Recluse-Origin-Secret": "test_worker_secret",
                    "X-Recluse-Client-IP": "198.51.100.55",
                },
                allow_redirects=False,
            )
            self.assertEqual(r_login_worker.status, 302)
            self.assertIn("discord.com/api/oauth2/authorize", r_login_worker.headers.get("Location", ""))

    async def test_login_renders_friendly_error_when_db_ready_is_false(self):
        import webdash

        webdash._DB_NOT_READY_WARNED = False
        self.fake_bot.db_ready = False
        try:
            with self.assertLogs("recluse.webdash", level="WARNING") as cm:
                r_login = await self.client.get(
                    "/login",
                    headers={
                        "X-Recluse-Origin-Secret": "test_worker_secret",
                        "X-Recluse-Client-IP": "198.51.100.55",
                    },
                    allow_redirects=False,
                )
            self.assertEqual(r_login.status, 503)
            body = await r_login.text()
            self.assertIn("Sessions Temporarily Unavailable", body)
            self.assertTrue(
                any("webdash: bot.db_ready is False, sessions disabled" in line for line in cm.output)
            )
        finally:
            self.fake_bot.db_ready = True

    async def test_server_picker_skips_guild_config_cache_for_non_member_guilds(self):
        # Outsider is NOT a member of self.fake_guild and NOT guild/bot owner.
        # Clear GuildConfigCache and reset find_one mock to prove /servers never queries GuildConfigCache for it.
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        self.app[GUILD_CONFIG_CACHE_KEY].clear()
        self.fake_db.guild_settings.find_one.reset_mock()

        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")
        r_servers = await self.client.get(
            "/servers",
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_servers.status, 200)
        self.fake_db.guild_settings.find_one.assert_not_awaited()

    async def test_overview_lockdown_rebuilt_notice_sidebar_and_verified_defaults(self):
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        # Even if stale lockdown_active=True is in DB and automod_enabled/welcome_enabled are absent:
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "lockdown_active": True,
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        r_ov = await self.client.get(
            f"/manage/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_ov.status, 200)
        html = await r_ov.text()

        # 1. Lockdown card shows "Disabled: being rebuilt", never "ENGAGED" or "Channel permission snapshot protection"
        self.assertIn("Disabled: being rebuilt", html)
        self.assertNotIn("ENGAGED", html)
        self.assertNotIn("Channel permission snapshot protection", html)

        # 2. Sidebar has hidden md:flex and lists ONLY Overview (/manage/{guild_id}) in Checkpoint 3A
        self.assertIn("hidden md:flex", html)
        self.assertIn(f'/manage/{self.guild_id}', html)
        self.assertNotIn(f'/modules/{self.guild_id}', html)
        self.assertNotIn(f'/automod/{self.guild_id}', html)
        self.assertNotIn(f'/lockdown/{self.guild_id}', html)

        # 3. Default automod_enabled is False ("Disabled" in AutoMod Status card)
        self.assertIn("Disabled", html)

    async def test_checkpoint_3a_placeholder_routes_return_501_with_rollback_note(self):
        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")
        owner_sid, _ = await self._create_test_session(self.bot_owner_id, "Owner")

        unported_guild_paths = [
            f"/modules/{self.guild_id}",
            f"/automod/{self.guild_id}",
            f"/wizard/{self.guild_id}",
            f"/lockdown/{self.guild_id}",
            f"/misc/{self.guild_id}",
            f"/logs/{self.guild_id}",
        ]
        for path in unported_guild_paths:
            with self.subTest(path=path):
                # Logged out -> 302
                r_anon = await self.client.get(path, allow_redirects=False)
                self.assertEqual(r_anon.status, 302)

                # Non-member -> 403
                r_out = await self.client.get(path, cookies={SESSION_COOKIE_NAME: outsider_sid})
                self.assertEqual(r_out.status, 403)

                # Authorized Admin -> 501 with WEBDASH_ENABLED=0 note
                r_ok = await self.client.get(path, cookies={SESSION_COOKIE_NAME: admin_sid})
                self.assertEqual(r_ok.status, 501)
                text = await r_ok.text()
                self.assertIn("WEBDASH_ENABLED=0", text)

        # /owner_panel: non-owner -> 403, owner -> 501
        r_owner_denied = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_owner_denied.status, 403)

        r_owner_ok = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner_ok.status, 501)
        self.assertIn("WEBDASH_ENABLED=0", await r_owner_ok.text())


class TestDashboardCogWebdashWiring(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_cog_loads_webdash_and_polls_db_ready_in_background(self):
        from cogs.dashboard import Dashboard

        fake_bot = MagicMock()
        fake_bot.db_ready = False
        fake_bot.db = MagicMock()
        fake_bot.db.webdash_sessions.create_index = AsyncMock(return_value="ok")
        fake_bot.db.dashboard_audit.create_index = AsyncMock(return_value="ok")
        fake_bot.wait_until_ready = AsyncMock(side_effect=AssertionError("Must not await wait_until_ready in webdash start_server!"))

        def fake_create_task(coro):
            coro.close()
            return MagicMock()

        fake_bot.loop.create_task = fake_create_task

        with patch.dict(os.environ, {"WEBDASH_ENABLED": "1", "ORIGIN_SHARED_SECRET": "sec"}):
            cog = Dashboard(fake_bot)
            self.assertTrue(getattr(cog, "_webdash_enabled", False))

            # Verify /health is registered on cog.app (which only exists in webdash, not legacy)
            routes = [r.resource.canonical for r in cog.app.router.routes() if r.resource]
            self.assertIn("/health", routes)

            # Test background index polling: flips db_ready=True after 1 poll
            async def flip_ready():
                await asyncio.sleep(0.03)
                fake_bot.db_ready = True

            flipper = asyncio.create_task(flip_ready())
            await cog._ensure_webdash_indexes_bg(poll_interval=0.01, max_wait=0.5)
            await flipper

            self.assertGreaterEqual(fake_bot.db.webdash_sessions.create_index.await_count, 3)
            self.assertGreaterEqual(fake_bot.db.dashboard_audit.create_index.await_count, 2)

    async def test_blocked_aiohttp_jinja2_allows_legacy_and_falls_back_when_enabled(self):
        import importlib
        import importlib.abc
        import sys

        class _BlockAiohttpJinja2Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == "aiohttp_jinja2" or fullname.startswith("aiohttp_jinja2."):
                    raise ImportError("Blocked aiohttp_jinja2 by test MetaPathFinder")
                return None

        finder = _BlockAiohttpJinja2Finder()
        saved_modules = {}
        target_mods = [
            k
            for k in list(sys.modules.keys())
            if k in ("cogs.dashboard", "webdash.app", "webdash.router")
            or k == "aiohttp_jinja2"
            or k.startswith("aiohttp_jinja2.")
        ]
        for mod_name in target_mods:
            saved_modules[mod_name] = sys.modules.pop(mod_name)

        sys.meta_path.insert(0, finder)
        try:
            fake_bot = MagicMock()
            def fake_create_task(coro):
                coro.close()
                return MagicMock()
            fake_bot.loop.create_task = fake_create_task

            # 1. With WEBDASH_ENABLED unset, importing cogs.dashboard and constructing Dashboard succeeds!
            env_unset = {k: v for k, v in os.environ.items() if k != "WEBDASH_ENABLED"}
            with patch.dict(os.environ, env_unset, clear=True):
                dash_mod = importlib.import_module("cogs.dashboard")
                cog_legacy = dash_mod.Dashboard(fake_bot)
                self.assertFalse(getattr(cog_legacy, "_webdash_enabled", False))
                legacy_routes = [r.resource.canonical for r in cog_legacy.app.router.routes() if r.resource]
                self.assertIn("/api/verify_visitor", legacy_routes)

            # 2. With WEBDASH_ENABLED=1 and aiohttp_jinja2 blocked, Dashboard logs the error and falls back to legacy!
            with patch.dict(os.environ, {"WEBDASH_ENABLED": "1"}):
                with self.assertLogs("recluse.dashboard", level="ERROR") as cm:
                    cog_fallback = dash_mod.Dashboard(fake_bot)
                self.assertFalse(getattr(cog_fallback, "_webdash_enabled", False))
                fallback_routes = [r.resource.canonical for r in cog_fallback.app.router.routes() if r.resource]
                self.assertIn("/api/verify_visitor", fallback_routes)
                self.assertTrue(
                    any("falling back to legacy dashboard" in line for line in cm.output)
                )
        finally:
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
            sys.modules.update(saved_modules)


if __name__ == "__main__":
    unittest.main()


