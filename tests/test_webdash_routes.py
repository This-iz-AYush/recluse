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

        self.guild_settings_doc: dict | None = {
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
            "disabled_cogs": ["Sports"],
            "command_settings": {"ban": {"disabled": False}},
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {"allow": ["overview.view", "automod.*"], "deny": []},
                }
            },
        }
        self.fake_db.guild_settings.find_one = AsyncMock(
            side_effect=lambda q: dict(self.guild_settings_doc)
            if (self.guild_settings_doc is not None and q.get("guild_id") == self.guild_id)
            else None
        )

        async def _fake_guild_settings_update_one(query: dict, update: dict, upsert: bool = False):
            if query.get("guild_id") != self.guild_id:
                return MagicMock(matched_count=0, modified_count=0, upserted_id=None)

            doc = self.guild_settings_doc
            if doc is None:
                if not upsert:
                    return MagicMock(matched_count=0, modified_count=0, upserted_id=None)
                doc = {"guild_id": self.guild_id}
                self.guild_settings_doc = doc
                matched = 0
                upserted_id = "new_id_1"
            else:
                current_rev = doc.get("dashboard_rev", 0)
                if not isinstance(current_rev, int) or isinstance(current_rev, bool):
                    current_rev = 0
                if "$or" in query:
                    if current_rev != 0:
                        return MagicMock(matched_count=0, modified_count=0, upserted_id=None)
                elif "dashboard_rev" in query:
                    if current_rev != query["dashboard_rev"]:
                        return MagicMock(matched_count=0, modified_count=0, upserted_id=None)
                matched = 1
                upserted_id = None

            if "$set" in update and isinstance(update["$set"], dict):
                doc.update(update["$set"])
            if "$inc" in update and isinstance(update["$inc"], dict):
                for inc_k, inc_v in update["$inc"].items():
                    prev_v = doc.get(inc_k, 0)
                    if not isinstance(prev_v, int) or isinstance(prev_v, bool):
                        prev_v = 0
                    doc[inc_k] = prev_v + int(inc_v)

            return MagicMock(matched_count=matched, modified_count=1, upserted_id=upserted_id)

        self.fake_db.guild_settings.update_one = AsyncMock(side_effect=_fake_guild_settings_update_one)

        self.audit_rows: list[dict] = []

        async def _fake_audit_insert_one(entry: dict):
            self.audit_rows.append(dict(entry))
            return MagicMock(inserted_id=f"audit_{len(self.audit_rows)}")

        self.fake_db.dashboard_audit.insert_one = AsyncMock(side_effect=_fake_audit_insert_one)

        self.ip_ban_docs: dict[str, dict] = {}

        class _InMemoryIpBansCursor:
            def __init__(self, docs: list[dict]):
                self._docs = list(docs)

            def limit(self, n: int):
                self._docs = self._docs[: int(n)]
                return self

            def __aiter__(self):
                self._idx = 0
                return self

            async def __anext__(self):
                if self._idx >= len(self._docs):
                    raise StopAsyncIteration
                item = self._docs[self._idx]
                self._idx += 1
                return item

        self.fake_db.ip_bans.find = MagicMock(
            side_effect=lambda *a, **kw: _InMemoryIpBansCursor(list(self.ip_ban_docs.values()))
        )
        self.fake_db.ip_bans.find_one = AsyncMock(
            side_effect=lambda q: dict(self.ip_ban_docs[q["ip"]])
            if isinstance(q, dict) and q.get("ip") in self.ip_ban_docs
            else None
        )

        async def _fake_ip_bans_update_one(query: dict, update: dict, upsert: bool = False):
            ip_val = str(query.get("ip", ""))
            doc = self.ip_ban_docs.get(ip_val)
            if doc is None:
                if not upsert:
                    return MagicMock(matched_count=0, modified_count=0, upserted_id=None)
                doc = {"ip": ip_val}
                self.ip_ban_docs[ip_val] = doc
            if "$set" in update and isinstance(update["$set"], dict):
                doc.update(update["$set"])
            return MagicMock(matched_count=1, modified_count=1, upserted_id=None)

        async def _fake_ip_bans_delete_one(query: dict):
            ip_val = str(query.get("ip", ""))
            existed = self.ip_ban_docs.pop(ip_val, None) is not None
            return MagicMock(deleted_count=1 if existed else 0)

        self.fake_db.ip_bans.update_one = AsyncMock(side_effect=_fake_ip_bans_update_one)
        self.fake_db.ip_bans.delete_one = AsyncMock(side_effect=_fake_ip_bans_delete_one)

        self.fake_ai_cog = MagicMock()
        self.fake_ai_cog._invalidate_guild_cfg = MagicMock()
        self.fake_bot.get_cog = MagicMock(
            side_effect=lambda name: self.fake_ai_cog if name == "AI" else None
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
        overview_body = await r_ov.text()

        # 1. Lockdown card shows "Disabled: being rebuilt", never "ENGAGED" or "Channel permission snapshot protection"
        self.assertIn("Disabled: being rebuilt", overview_body)
        self.assertNotIn("ENGAGED", overview_body)
        self.assertNotIn("Channel permission snapshot protection", overview_body)

        # 2. Sidebar has hidden md:flex and lists all Checkpoint 3B pages
        #    (overview, modules, automod, lockdown, wizard, misc, logs)
        self.assertIn("hidden md:flex", overview_body)
        self.assertIn(f"/manage/{self.guild_id}", overview_body)
        self.assertIn(f"/modules/{self.guild_id}", overview_body)
        self.assertIn(f"/automod/{self.guild_id}", overview_body)
        self.assertIn(f"/lockdown/{self.guild_id}", overview_body)
        self.assertIn(f"/wizard/{self.guild_id}", overview_body)
        self.assertIn(f"/misc/{self.guild_id}", overview_body)
        self.assertIn(f"/logs/{self.guild_id}", overview_body)

        # 3. Default automod_enabled is False ("Disabled" in AutoMod Status card)
        self.assertIn("Disabled", overview_body)

    async def test_checkpoint_3b_batch1_automod_lockdown_and_misc_routes(self):
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "automod_enabled": False,
            "banned_words": ["badword1", "<script>xss()</script>"],
            "misc_enabled": False,
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {"allow": ["overview.view", "automod.*"], "deny": []},
                }
            },
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        mod_sid, _ = await self._create_test_session(self.mod_user_id, "ModUser")
        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")

        batch1_paths = [
            f"/automod/{self.guild_id}",
            f"/lockdown/{self.guild_id}",
            f"/misc/{self.guild_id}",
        ]
        for path in batch1_paths:
            with self.subTest(path=path):
                # Logged out -> 302 to /login
                r_anon = await self.client.get(path, allow_redirects=False)
                self.assertEqual(r_anon.status, 302)
                self.assertEqual(r_anon.headers.get("Location"), "/login")

                # Non-member -> 403
                r_out = await self.client.get(path, cookies={SESSION_COOKIE_NAME: outsider_sid})
                self.assertEqual(r_out.status, 403)

                # Missing guild -> 404; invalid snowflake -> 400
                route_prefix = path.rsplit("/", 1)[0]
                r_missing = await self.client.get(
                    f"{route_prefix}/111222333",
                    cookies={SESSION_COOKIE_NAME: admin_sid},
                )
                self.assertEqual(r_missing.status, 404)

                r_bad_id = await self.client.get(
                    f"{route_prefix}/not-a-snowflake",
                    cookies={SESSION_COOKIE_NAME: admin_sid},
                )
                self.assertEqual(r_bad_id.status, 400)

        # 1. GET /automod/{guild_id} — ModUser (has automod.*) -> 200, escaped XSS, zero inline handlers,
        #    and sidebar filtered to ONLY overview + automod (no lockdown, wizard, misc, or logs)
        r_automod = await self.client.get(
            f"/automod/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_automod.status, 200)
        automod_body = await r_automod.text()
        self.assertIn("Automated Content Filtration", automod_body)
        self.assertIn("badword1", automod_body)
        self.assertNotIn("<script>xss()</script>", automod_body)
        self.assertIn("&lt;script&gt;xss()&lt;/script&gt;", automod_body)
        self.assertIn("AutoMod: Disabled", automod_body)
        self.assertIn(f"/automod/{self.guild_id}", automod_body)
        self.assertNotIn(f"/lockdown/{self.guild_id}", automod_body)
        self.assertNotIn(f"/wizard/{self.guild_id}", automod_body)
        self.assertNotIn(f"/misc/{self.guild_id}", automod_body)
        self.assertNotIn(f"/logs/{self.guild_id}", automod_body)
        self.assertIsNone(INLINE_HANDLER_RE.search(automod_body))

        # 1b. AutoMod banned_words display cap at 500 entries with "+N more" note
        self.guild_settings_doc["banned_words"] = [f"term_{i}" for i in range(1, 513)]
        self.app[GUILD_CONFIG_CACHE_KEY].clear()
        r_automod_cap = await self.client.get(
            f"/automod/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_automod_cap.status, 200)
        automod_cap_body = await r_automod_cap.text()
        self.assertIn("512 words", automod_cap_body)
        self.assertIn("term_1", automod_cap_body)
        self.assertIn("term_500", automod_cap_body)
        self.assertNotIn("term_501", automod_cap_body)
        self.assertIn("+12 more", automod_cap_body)

        # 2. GET /lockdown/{guild_id} — ModUser (lacks antinuke.view) -> 403; AdminUser -> 200
        r_lock_mod = await self.client.get(
            f"/lockdown/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_lock_mod.status, 403)

        r_lock_admin = await self.client.get(
            f"/lockdown/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_lock_admin.status, 200)
        lock_body = await r_lock_admin.text()
        self.assertIn("Disabled: being rebuilt", lock_body)
        self.assertIn("Lockdown is being rebuilt and is temporarily disabled", lock_body)
        self.assertIsNone(INLINE_HANDLER_RE.search(lock_body))

        # 3. GET /misc/{guild_id} — ModUser (lacks settings.view) -> 403; AdminUser -> 200
        r_misc_mod = await self.client.get(
            f"/misc/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_misc_mod.status, 403)

        r_misc_admin = await self.client.get(
            f"/misc/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_misc_admin.status, 200)
        misc_body = await r_misc_admin.text()
        self.assertIn("Dynamic AFK &amp; Information Tools", misc_body)
        self.assertIn("Disabled", misc_body)
        self.assertIsNone(INLINE_HANDLER_RE.search(misc_body))

    async def test_checkpoint_3b_batch2_wizard_and_logs_with_support_preset_audit_split(self):
        import datetime
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        class _AsyncListCursor:
            def __init__(self, items: list[dict]) -> None:
                self._items = list(items)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for item in self._items:
                    yield item

        self.fake_db.security_logs.find = MagicMock(
            side_effect=lambda _q: _AsyncListCursor(
                [
                    {
                        "guild_id": self.guild_id,
                        "timestamp": 1761523200.0,
                        "action": "Automod — Banned Word",
                        "user_id": 777888999,
                        "content": "blocked <script>evil()</script> message",
                    }
                ]
            )
        )
        self.fake_db.dashboard_audit.find = MagicMock(
            side_effect=lambda _q: _AsyncListCursor(
                [
                    {
                        "guild_id": self.guild_id,
                        "actor_id": self.admin_user_id,
                        "actor_name": "Admin<script>xss()</script>",
                        "module": "automod",
                        "action": "update",
                        "changes": {
                            "automod_enabled": {"old": False, "new": True},
                        },
                        "created_at": datetime.datetime(2026, 10, 27, 1, 30, tzinfo=datetime.timezone.utc),
                    }
                ]
            )
        )

        # Configure role 555 (ModUser) with the built-in "Support" preset
        # (webdash/permissions.py:90-95 -> overview.view, tickets.*, moderation.view, logging.view; NO audit.view)
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "default_ai_model": "deepseek",
            "ai_enabled": True,
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {"allow": ["Support"], "deny": []},
                }
            },
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        support_sid, _ = await self._create_test_session(self.mod_user_id, "SupportUser")
        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")

        batch2_paths = [
            f"/wizard/{self.guild_id}",
            f"/logs/{self.guild_id}",
        ]
        for path in batch2_paths:
            with self.subTest(path=path):
                # Logged out -> 302 to /login
                r_anon = await self.client.get(path, allow_redirects=False)
                self.assertEqual(r_anon.status, 302)
                self.assertEqual(r_anon.headers.get("Location"), "/login")

                # Non-member -> 403
                r_out = await self.client.get(path, cookies={SESSION_COOKIE_NAME: outsider_sid})
                self.assertEqual(r_out.status, 403)

                # Missing guild -> 404; invalid snowflake -> 400
                route_prefix = path.rsplit("/", 1)[0]
                r_missing = await self.client.get(
                    f"{route_prefix}/111222333",
                    cookies={SESSION_COOKIE_NAME: admin_sid},
                )
                self.assertEqual(r_missing.status, 404)

                r_bad_id = await self.client.get(
                    f"{route_prefix}/not-a-snowflake",
                    cookies={SESSION_COOKIE_NAME: admin_sid},
                )
                self.assertEqual(r_bad_id.status, 400)

        # 1. GET /wizard/{guild_id}:
        #    - SupportUser lacks settings.view -> 403
        r_wiz_support = await self.client.get(
            f"/wizard/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: support_sid},
        )
        self.assertEqual(r_wiz_support.status, 403)

        #    - AdminUser -> 200, renders real cogs/ai.py models (auto, deepseek, groq, gemini), never nexusify/sarvam
        r_wiz_admin = await self.client.get(
            f"/wizard/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_wiz_admin.status, 200)
        wiz_body = await r_wiz_admin.text()
        self.assertIn("Select Default AI Core", wiz_body)
        self.assertIn('value="auto"', wiz_body)
        self.assertIn('value="deepseek"', wiz_body)
        self.assertIn('value="groq"', wiz_body)
        self.assertIn('value="gemini"', wiz_body)
        self.assertNotIn("nexusify", wiz_body.lower())
        self.assertNotIn("sarvam", wiz_body.lower())
        self.assertIsNone(INLINE_HANDLER_RE.search(wiz_body))

        # 2. GET /logs/{guild_id} as Support-preset user (has logging.view, lacks audit.view):
        #    - Sees security_logs section (with escaped XSS), does NOT see dashboard_audit section,
        #      and db.dashboard_audit.find is NEVER called!
        self.fake_db.security_logs.find.reset_mock()
        self.fake_db.dashboard_audit.find.reset_mock()

        r_logs_support = await self.client.get(
            f"/logs/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: support_sid},
        )
        self.assertEqual(r_logs_support.status, 200)
        logs_support_body = await r_logs_support.text()
        self.assertIn("Bot Security &amp; AutoMod Events", logs_support_body)
        self.assertIn("Automod — Banned Word", logs_support_body)
        self.assertNotIn("<script>evil()</script>", logs_support_body)
        self.assertIn("&lt;script&gt;evil()&lt;/script&gt;", logs_support_body)
        self.assertNotIn("Dashboard Configuration Audit History", logs_support_body)
        self.assertNotIn("dashboardAuditSection", logs_support_body)
        self.assertEqual(self.fake_db.security_logs.find.call_count, 1)
        self.assertEqual(self.fake_db.dashboard_audit.find.call_count, 0)
        self.assertIsNone(INLINE_HANDLER_RE.search(logs_support_body))

        # 3. GET /logs/{guild_id} as AdminUser (has both logging.view and audit.view):
        #    - Sees BOTH security_logs and dashboard_audit (with escaped actor name and field diff)
        r_logs_admin = await self.client.get(
            f"/logs/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_logs_admin.status, 200)
        logs_admin_body = await r_logs_admin.text()
        self.assertIn("Bot Security &amp; AutoMod Events", logs_admin_body)
        self.assertIn("Dashboard Configuration Audit History", logs_admin_body)
        self.assertIn("dashboardAuditSection", logs_admin_body)
        self.assertIn("automod_enabled", logs_admin_body)
        self.assertNotIn("<script>xss()</script>", logs_admin_body)
        self.assertIn("Admin&lt;script&gt;xss()&lt;/script&gt;", logs_admin_body)
        self.assertEqual(self.fake_db.dashboard_audit.find.call_count, 1)
        self.assertIsNone(INLINE_HANDLER_RE.search(logs_admin_body))

        # 4. Verify security_logs content truncation at 300 characters with an ellipsis ("…")
        long_payload = "A" * 350
        self.fake_db.security_logs.find = MagicMock(
            side_effect=lambda _q: _AsyncListCursor(
                [
                    {
                        "guild_id": self.guild_id,
                        "timestamp": 1761523200.0,
                        "action": "Long Content Test",
                        "user_id": 777888999,
                        "content": long_payload,
                    }
                ]
            )
        )
        r_logs_trunc = await self.client.get(
            f"/logs/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: support_sid},
        )
        self.assertEqual(r_logs_trunc.status, 200)
        logs_trunc_body = await r_logs_trunc.text()
        self.assertIn(("A" * 300) + "…", logs_trunc_body)
        self.assertNotIn("A" * 301, logs_trunc_body)

    async def test_logs_query_raises_and_timeout_show_database_unavailable_message(self):
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {"allow": ["Support"], "deny": []},
                }
            },
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        support_sid, _ = await self._create_test_session(self.mod_user_id, "SupportUser")

        # 1. Both security_logs.find and dashboard_audit.find raise RuntimeError
        self.fake_db.security_logs.find = MagicMock(side_effect=RuntimeError("Mongo network error"))
        self.fake_db.dashboard_audit.find = MagicMock(side_effect=RuntimeError("Mongo network error"))

        r_raise = await self.client.get(
            f"/logs/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_raise.status, 200)
        raise_body = await r_raise.text()
        self.assertEqual(raise_body.count("Could not load logs (database unavailable)"), 2)
        self.assertNotIn("No security events logged yet.", raise_body)
        self.assertNotIn("No configuration changes recorded yet.", raise_body)

        # 2. Both cursors hang past the 2.0s timeout (tested both with real 2.0s timeout on SupportUser
        #    and patched fast timeout for dashboard_audit)
        class _HangingCursor:
            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                await asyncio.sleep(5.0)
                if False:
                    yield {}

        self.fake_db.security_logs.find = MagicMock(side_effect=lambda _q: _HangingCursor())
        self.fake_db.dashboard_audit.find = MagicMock(side_effect=lambda _q: _HangingCursor())

        r_timeout_support = await self.client.get(
            f"/logs/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: support_sid},
        )
        self.assertEqual(r_timeout_support.status, 200)
        timeout_support_body = await r_timeout_support.text()
        self.assertIn("Could not load logs (database unavailable)", timeout_support_body)
        self.assertNotIn("No security events logged yet.", timeout_support_body)
        self.assertEqual(self.fake_db.dashboard_audit.find.call_count, 0)

    async def test_checkpoint_3b_batch3_modules_protected_cog_recovery_and_wired_command_keys(self):
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        # Configure live guild channels and roles
        ch_gen = MagicMock()
        ch_gen.id = 1001
        ch_gen.name = "general"
        ch_bot = MagicMock()
        ch_bot.id = 1002
        ch_bot.name = "bot-commands"
        self.fake_guild.text_channels = [ch_gen, ch_bot]

        r_everyone = MagicMock()
        r_everyone.id = self.guild_id
        r_everyone.name = "@everyone"
        r_mod = MagicMock()
        r_mod.id = 2001
        r_mod.name = "Moderator"
        r_muted = MagicMock()
        r_muted.id = 2002
        r_muted.name = "Muted"
        self.fake_guild.roles = [r_everyone, r_mod, r_muted]

        # Build fake cogs on self.fake_bot.cogs:
        # Protected cogs: Core, Database, Dashboard, Owner, Jishaku
        # Configurable cogs: Moderation, Sports
        def _make_cog(cmds_spec: list[tuple[str, str, list[str]]]):
            cog = MagicMock()
            prefix_cmds = []
            for cname, cdesc, subs in cmds_spec:
                cmd_obj = MagicMock()
                cmd_obj.name = cname
                cmd_obj.help = cdesc
                cmd_obj.description = cdesc
                sub_objs = []
                for sname in subs:
                    sobj = MagicMock()
                    sobj.name = sname
                    sub_objs.append(sobj)
                cmd_obj.commands = sub_objs
                prefix_cmds.append(cmd_obj)
            cog.get_commands = MagicMock(return_value=prefix_cmds)
            cog.walk_app_commands = MagicMock(return_value=[])
            return cog

        self.fake_bot.cogs = {
            "Core": _make_cog([("help", "Show help menu", [])]),
            "Database": _make_cog([]),
            "Dashboard": _make_cog([]),
            "Owner": _make_cog([]),
            "Jishaku": _make_cog([]),
            "Moderation": _make_cog(
                [
                    ("ban", "Ban a member <script>xss()</script>", ["temp", "perm"]),
                ]
            ),
            "Sports": _make_cog([("nba", "NBA scores", [])]),
        }

        # Case A: Normal state — Core, Database, Dashboard, Owner, Jishaku are enabled (not in disabled_cogs).
        # All 5 protected cogs MUST be hidden from /modules/{guild_id}.
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "disabled_cogs": ["Sports"],
            "disabled_cmds": ["purge", "evil<script>alert(1)</script>"],
            "command_settings": {
                "ban": {
                    "disabled": True,  # Legacy unwired top-level key — must NOT be exposed as a command toggle
                    "disabled_subcommands": ["temp"],
                    "allowed_channels": ["1001"],
                    "ignored_channels": ["1002"],
                    "allowed_roles": ["2001"],
                    "ignored_roles": ["2002"],
                }
            },
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {"allow": ["Support"], "deny": []},
                }
            },
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        support_sid, _ = await self._create_test_session(self.mod_user_id, "SupportUser")
        outsider_sid, _ = await self._create_test_session(self.outsider_user_id, "Outsider")

        # RBAC checks: Logged out -> 302, Outsider -> 403, SupportUser (lacks modules.view) -> 403,
        # Missing guild -> 404, Invalid snowflake -> 400
        r_anon = await self.client.get(f"/modules/{self.guild_id}", allow_redirects=False)
        self.assertEqual(r_anon.status, 302)
        self.assertEqual(r_anon.headers.get("Location"), "/login")

        r_out = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_out.status, 403)

        r_sup = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: support_sid},
        )
        self.assertEqual(r_sup.status, 403)

        r_missing = await self.client.get(
            "/modules/111222333",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_missing.status, 404)

        r_bad = await self.client.get(
            "/modules/not-a-snowflake",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_bad.status, 400)

        # AdminUser -> 200 OK
        r_mod_ok = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_mod_ok.status, 200)
        mod_body = await r_mod_ok.text()

        # Verify all 5 protected cogs are hidden when enabled
        self.assertNotIn("Core Module", mod_body)
        self.assertNotIn("Database Module", mod_body)
        self.assertNotIn("Dashboard Module", mod_body)
        self.assertNotIn("Owner Module", mod_body)
        self.assertNotIn("Jishaku Module", mod_body)

        # Verify Moderation and Sports are shown
        self.assertIn("Moderation Module", mod_body)
        self.assertIn("Sports Module", mod_body)

        # Verify only the 5 wired command_settings keys are rendered and resolved to live channel/role names
        self.assertIn("allowed_channels", mod_body)
        self.assertIn("#general", mod_body)
        self.assertIn("ignored_channels", mod_body)
        self.assertIn("#bot-commands", mod_body)
        self.assertIn("allowed_roles", mod_body)
        self.assertIn("@Moderator", mod_body)
        self.assertIn("ignored_roles", mod_body)
        self.assertIn("@Muted", mod_body)
        self.assertIn("disabled_subcommands", mod_body)
        self.assertIn("/ban temp", mod_body)
        self.assertIn("/ban perm", mod_body)

        # Verify read-only disabled_cmds is displayed and escaped
        self.assertIn("disabled_cmds", mod_body)
        self.assertIn("/purge", mod_body)
        self.assertNotIn("<script>alert(1)</script>", mod_body)
        self.assertIn("/evil&lt;script&gt;alert(1)&lt;/script&gt;", mod_body)
        self.assertNotIn("<script>xss()</script>", mod_body)
        self.assertIn("&lt;script&gt;xss()&lt;/script&gt;", mod_body)
        self.assertIsNone(INLINE_HANDLER_RE.search(mod_body))

        # Case B: Protected-Cog Recovery Rule — Server has "Core" in disabled_cogs (from legacy UI).
        # "Core Module" MUST now be displayed with a clear "Protected (Re-enable Only)" badge
        # while the other 4 enabled protected cogs stay hidden!
        self.guild_settings_doc["disabled_cogs"] = ["Core", "Sports"]
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        r_recovery = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_recovery.status, 200)
        recovery_body = await r_recovery.text()
        self.assertIn("Core Module", recovery_body)
        self.assertIn("Protected (Re-enable Only)", recovery_body)
        self.assertIn('data-protected="true"', recovery_body)
        self.assertNotIn("Database Module", recovery_body)
        self.assertNotIn("Dashboard Module", recovery_body)
        self.assertNotIn("Owner Module", recovery_body)
        self.assertNotIn("Jishaku Module", recovery_body)
        self.assertIsNone(INLINE_HANDLER_RE.search(recovery_body))

    async def test_checkpoint_3b_batch3_owner_panel_separate_flags_and_normalized_escaped_ips(self):
        class _AsyncListCursor:
            def __init__(self, items: list[dict]) -> None:
                self._items = list(items)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for item in self._items:
                    yield item

        self.fake_bot.extensions = {
            "cogs.admin": MagicMock(),
            "cogs.moderation": MagicMock(),
        }
        self.fake_db.visit_logs.find = MagicMock(
            side_effect=lambda: _AsyncListCursor(
                [
                    {
                        "ip": "::ffff:203.0.113.7",
                        "last_user": "Visitor<script>xss()</script>",
                        "hits": 9,
                    },
                    {
                        "ip": '1.2.3.4" data-evil="1',
                        "last_user": "Guest",
                        "hits": 2,
                    },
                ]
            )
        )
        self.fake_db.ip_bans.find = MagicMock(
            side_effect=lambda: _AsyncListCursor(
                [
                    {"ip": "::ffff:198.51.100.44"},
                ]
            )
        )

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        owner_sid, _ = await self._create_test_session(self.bot_owner_id, "OwnerUser")

        # 1. Logged out -> 302 to /login
        r_anon = await self.client.get("/owner_panel", allow_redirects=False)
        self.assertEqual(r_anon.status, 302)
        self.assertEqual(r_anon.headers.get("Location"), "/login")

        # 2. Non-owner Admin -> 403 Forbidden
        r_denied = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_denied.status, 403)

        # 3. Bot Owner with global_lockdown=True, dashboard_maintenance=False -> 200 OK
        self.fake_bot.global_lockdown = True
        self.fake_bot.dashboard_maintenance = False

        r_owner1 = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner1.status, 200)
        owner_body1 = await r_owner1.text()

        # Verify both distinct flags and the note that legacy showed neither
        self.assertIn("bot.global_lockdown (bot.py:33, 82)", owner_body1)
        self.assertIn("Global Command Lockdown", owner_body1)
        self.assertIn("Active (Locked)", owner_body1)
        self.assertIn("bot.dashboard_maintenance (cogs/dashboard.py:43, 122)", owner_body1)
        self.assertIn("Web Dashboard Maintenance", owner_body1)
        self.assertIn("Operational", owner_body1)
        self.assertIn("showed neither flag", owner_body1)

        # Verify loaded extensions use data-cog attributes
        self.assertIn("cogs.admin", owner_body1)
        self.assertIn('data-cog="admin"', owner_body1)
        self.assertIn("cogs.moderation", owner_body1)
        self.assertIn('data-cog="moderation"', owner_body1)

        # Verify IPs are normalized via normalize_ip (::ffff:203.0.113.7 -> 203.0.113.7;
        # malformed IP -> "invalid") and XSS in last_user is escaped
        self.assertIn('data-ip="203.0.113.7"', owner_body1)
        self.assertNotIn("::ffff:203.0.113.7", owner_body1)
        self.assertIn('data-ip="invalid"', owner_body1)
        self.assertNotIn('data-evil="1', owner_body1)
        self.assertIn('data-ip="198.51.100.44"', owner_body1)
        self.assertNotIn("<script>xss()</script>", owner_body1)
        self.assertIn("Visitor&lt;script&gt;xss()&lt;/script&gt;", owner_body1)
        self.assertIsNone(INLINE_HANDLER_RE.search(owner_body1))

        # 4. Flip flags: global_lockdown=False, dashboard_maintenance=True
        self.fake_bot.global_lockdown = False
        self.fake_bot.dashboard_maintenance = True
        r_owner2 = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner2.status, 200)
        owner_body2 = await r_owner2.text()
        self.assertIn("Inactive (Normal)", owner_body2)
        self.assertIn("Maintenance On", owner_body2)
        self.assertIsNone(INLINE_HANDLER_RE.search(owner_body2))

    async def test_batch0_modules_exact_case_disabled_cogs_and_lowercase_core(self):
        """
        Batch 0 fix:
        - `bot.py:110` and `cogs/core.py:745, 803` compare `cog_name in disabled_cogs` with exact case.
        - When `'core'` (lowercase) is stored in `disabled_cogs`, `Core` is NOT disabled at runtime
          and MUST NOT be shown as disabled in `/modules/{guild_id}` (and remains hidden as an
          enabled protected cog).
        - Case-insensitive matching is kept ONLY for hiding protected cogs.
        """
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        def _make_simple_cog():
            c = MagicMock()
            c.get_commands = MagicMock(return_value=[])
            c.walk_app_commands = MagicMock(return_value=[])
            return c

        self.fake_bot.cogs = {
            "Core": _make_simple_cog(),
            "Database": _make_simple_cog(),
            "Dashboard": _make_simple_cog(),
            "Owner": _make_simple_cog(),
            "Jishaku": _make_simple_cog(),
            "Moderation": _make_simple_cog(),
            "Sports": _make_simple_cog(),
        }

        # Store lowercase 'core' and lowercase 'sports' in disabled_cogs
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "disabled_cogs": ["core", "sports"],
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        r_mod = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_mod.status, 200)
        page_text = await r_mod.text()

        # 1. 'core' (lowercase) MUST NOT cause Core to be shown as disabled or recovered
        self.assertNotIn("Core Module", page_text)
        self.assertNotIn("core Module", page_text)
        self.assertNotIn("Protected (Re-enable Only)", page_text)

        # 2. 'sports' (lowercase) does not match 'Sports' (exact case), so Sports Module is shown as Enabled (checked)
        self.assertIn("Sports Module", page_text)
        sports_idx = page_text.index("Sports Module")
        sports_card_slice = page_text[max(0, sports_idx - 200) : sports_idx + 800]
        self.assertIn("Enabled", sports_card_slice)
        self.assertNotIn("Disabled", sports_card_slice)

    async def test_checkpoint_3c_batch1_page_scripts_fresh_reads_and_501_word_cap(self):
        """
        Verify Batch 3C-1 template & GET handler requirements:
        - `base.html` defines `getCsrfToken()` once and uses `{% block scripts %}`.
        - Edit pages (`/automod`, `/wizard`, `/misc`) include the save script (`fetch("/api/settings/...")`)
          for authorized editors.
        - `/wizard` save controls (`#saveBtn` and save script) appear ONLY when the user has `ai.edit`
          (not merely `settings.view` or `settings.edit`).
        - Edit pages bypass `GuildConfigCache` and read fresh from MongoDB, rendering `data-dashboard-rev`.
        - When stored `banned_words` has > 500 entries (501+), the automod editor is locked read-only
          with a notice even for users with `automod.edit`, so the 500-word display cap can never
          truncate unseen entries on save.
        """
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        mod_sid, _ = await self._create_test_session(self.mod_user_id, "ModUser")

        # 1. Seed GuildConfigCache with stale data (rev=1), then mutate Mongo doc directly (rev=7)
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "automod_enabled": True,
            "banned_words": ["stale_word"],
            "default_ai_model": "auto",
            "misc_enabled": True,
            "dashboard_rev": 1,
            "dashboard_permissions": {
                "role_mappings": {
                    # Role 555 has settings.view and settings.edit, but NOT ai.edit!
                    "555": {
                        "allow": ["overview.view", "settings.view", "settings.edit", "automod.*"],
                        "deny": [],
                    },
                }
            },
        }
        self.app[GUILD_CONFIG_CACHE_KEY].clear()
        # Warm GuildConfigCache via overview
        await self.client.get(f"/manage/{self.guild_id}", cookies={SESSION_COOKIE_NAME: admin_sid})

        # Now update Mongo directly WITHOUT invalidating GuildConfigCache
        self.guild_settings_doc["banned_words"] = ["fresh_word_alpha"]
        self.guild_settings_doc["default_ai_model"] = "gemini"
        self.guild_settings_doc["misc_enabled"] = False
        self.guild_settings_doc["dashboard_rev"] = 7

        # 2. GET /automod as Admin: reads fresh Mongo ("fresh_word_alpha", rev=7) and includes save script
        r_auto = await self.client.get(
            f"/automod/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_auto.status, 200)
        auto_text = await r_auto.text()
        self.assertIn("function getCsrfToken()", auto_text)
        self.assertIn("fresh_word_alpha", auto_text)
        self.assertNotIn("stale_word", auto_text)
        self.assertIn('data-dashboard-rev="7"', auto_text)
        self.assertIn(f'/api/settings/{self.guild_id}', auto_text)
        self.assertIn('"update_automod"', auto_text)
        self.assertIsNone(INLINE_HANDLER_RE.search(auto_text))

        # 3. GET /misc as Admin: reads fresh Mongo (misc_enabled=False, rev=7) and includes save script
        r_misc = await self.client.get(
            f"/misc/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_misc.status, 200)
        misc_text = await r_misc.text()
        self.assertIn('data-dashboard-rev="7"', misc_text)
        self.assertIn(f'/api/settings/{self.guild_id}', misc_text)
        self.assertIn('"toggleMisc"', misc_text)
        self.assertIsNone(INLINE_HANDLER_RE.search(misc_text))

        # 4. GET /wizard as Admin (has ai.edit): reads fresh Mongo (gemini, rev=7) and renders save button + script
        r_wiz_admin = await self.client.get(
            f"/wizard/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_wiz_admin.status, 200)
        wiz_admin_text = await r_wiz_admin.text()
        self.assertIn('data-dashboard-rev="7"', wiz_admin_text)
        self.assertIn('id="saveBtn"', wiz_admin_text)
        self.assertIn(f'/api/settings/{self.guild_id}', wiz_admin_text)
        self.assertIn('"update_ai_model"', wiz_admin_text)
        self.assertIsNone(INLINE_HANDLER_RE.search(wiz_admin_text))

        # 5. GET /wizard as ModUser (has settings.view + settings.edit, LACKS ai.edit):
        #    MUST NOT render #saveBtn or the save script!
        r_wiz_mod = await self.client.get(
            f"/wizard/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_wiz_mod.status, 200)
        wiz_mod_text = await r_wiz_mod.text()
        self.assertNotIn('id="saveBtn"', wiz_mod_text)
        self.assertNotIn('"update_ai_model"', wiz_mod_text)
        self.assertIn("ai.edit", wiz_mod_text)

        # 6. 501+ stored banned_words: editor is read-only with notice even for Admin (automod.edit)
        self.guild_settings_doc["banned_words"] = [f"word_{i}" for i in range(1, 502)]
        r_auto_501 = await self.client.get(
            f"/automod/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_auto_501.status, 200)
        auto_501_text = await r_auto_501.text()
        self.assertIn('id="overCapNotice"', auto_501_text)
        self.assertIn("exceeding the 500-word display cap", auto_501_text)
        self.assertIn("readonly", auto_501_text)
        self.assertNotIn('"update_automod"', auto_501_text)

    async def test_checkpoint_3c_batch1_settings_mutations_concurrency_and_audit(self):
        """
        Verify POST /api/settings/{guild_id} happy paths, optimistic concurrency (dashboard_rev),
        cache invalidation (GuildConfigCache + ai_cog._invalidate_guild_cfg), and AuditLogger rows:
        1. `update_automod` when `dashboard_rev` is missing in DB (0 -> 1)
        2. `update_ai_model` (1 -> 2)
        3. `toggle` with `module == "toggleMisc"` (2 -> 3)
        4. Unchanged value save still writes a non-empty `changes` audit row (3 -> 4)
        5. Stale `dashboard_rev` returns 409 Conflict with reload notice and does not mutate or audit
        6. `toggle_lockdown` returns 501 Not Implemented with zero writes
        """
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        # Start with a guild_settings doc that has NO dashboard_rev field (missing = 0)
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "automod_enabled": True,
            "banned_words": ["oldword"],
            "default_ai_model": "auto",
            "misc_enabled": True,
        }
        self.audit_rows.clear()
        self.fake_ai_cog._invalidate_guild_cfg.reset_mock()

        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "AdminActor")
        csrf = admin_doc["csrf_token"]
        headers = {
            "X-CSRF-Token": csrf,
            "X-Recluse-Origin-Secret": "test_worker_secret",
            "X-Recluse-Client-IP": "198.51.100.88",
        }

        # Prime GuildConfigCache so we can verify invalidation
        await self.app[GUILD_CONFIG_CACHE_KEY].get(self.guild_id)

        # 1. update_automod (expected_rev=0 -> new_rev=1)
        r1 = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_automod",
                "banned_words": ["  BadWordOne ", "BADWORDTWO", "badwordone"],
                "dashboard_rev": 0,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r1.status, 200)
        d1 = await r1.json()
        self.assertEqual(d1, {"status": "ok", "dashboard_rev": 1})
        self.assertEqual(self.guild_settings_doc["banned_words"], ["badwordone", "badwordtwo"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 1)
        self.fake_ai_cog._invalidate_guild_cfg.assert_called_with(self.guild_id)
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(self.audit_rows[-1]["module"], "automod")
        self.assertEqual(self.audit_rows[-1]["action"], "update_banned_words")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"banned_words": {"old": ["oldword"], "new": ["badwordone", "badwordtwo"]}},
        )
        self.assertEqual(self.audit_rows[-1]["ip_norm"], "198.51.100.88")

        # 2. update_ai_model (expected_rev=1 -> new_rev=2)
        self.fake_ai_cog._invalidate_guild_cfg.reset_mock()
        r2 = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_ai_model",
                "model": "groq",
                "dashboard_rev": 1,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r2.status, 200)
        d2 = await r2.json()
        self.assertEqual(d2, {"status": "ok", "dashboard_rev": 2})
        self.assertEqual(self.guild_settings_doc["default_ai_model"], "groq")
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 2)
        self.fake_ai_cog._invalidate_guild_cfg.assert_called_once_with(self.guild_id)
        self.assertEqual(len(self.audit_rows), 2)
        self.assertEqual(self.audit_rows[-1]["module"], "ai")
        self.assertEqual(self.audit_rows[-1]["action"], "update_ai_model")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"default_ai_model": {"old": "auto", "new": "groq"}},
        )

        # 3. toggleMisc (expected_rev=2 -> new_rev=3)
        self.fake_ai_cog._invalidate_guild_cfg.reset_mock()
        r3 = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "toggle",
                "module": "toggleMisc",
                "enabled": False,
                "dashboard_rev": 2,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r3.status, 200)
        d3 = await r3.json()
        self.assertEqual(d3, {"status": "ok", "dashboard_rev": 3})
        self.assertFalse(self.guild_settings_doc["misc_enabled"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 3)
        self.fake_ai_cog._invalidate_guild_cfg.assert_called_once_with(self.guild_id)
        self.assertEqual(len(self.audit_rows), 3)
        self.assertEqual(self.audit_rows[-1]["module"], "misc")
        self.assertEqual(self.audit_rows[-1]["action"], "toggle_misc")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"misc_enabled": {"old": True, "new": False}},
        )

        # 4. Saving the exact same value ("groq") still produces an audit row with non-empty changes (3 -> 4)
        r4 = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_ai_model",
                "model": "groq",
                "dashboard_rev": 3,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r4.status, 200)
        self.assertEqual((await r4.json())["dashboard_rev"], 4)
        self.assertEqual(len(self.audit_rows), 4)
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"default_ai_model": {"old": "groq", "new": "groq"}},
        )

        # 5. Stale dashboard_rev (sending rev=2 when DB is at 4) -> 409 Conflict, no mutation, no audit row
        r_conflict = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_ai_model",
                "model": "gemini",
                "dashboard_rev": 2,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_conflict.status, 409)
        conflict_data = await r_conflict.json()
        self.assertIn("reload", conflict_data.get("error", "").lower())
        self.assertEqual(self.guild_settings_doc["default_ai_model"], "groq")
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 4)
        self.assertEqual(len(self.audit_rows), 4)

        # 6. toggle_lockdown -> 501 Not Implemented, no DB mutation, no audit row
        r_lock = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "toggle_lockdown"},
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_lock.status, 501)
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 4)
        self.assertEqual(len(self.audit_rows), 4)

    async def test_checkpoint_3c_batch1_settings_attack_vectors(self):
        """
        Attack & validation tests for POST /api/settings/{guild_id}:
        - Missing CSRF / wrong CSRF -> 403; unauthenticated -> 401
        - Outsider (not in guild) -> 403
        - Missing specific permission node (`automod.edit`, `ai.edit`, `settings.edit`, `lockdown.execute`) -> 403
        - Stored banned_words > 500 -> 400 (refuses truncation)
        - Wrong types, extra keys, control chars, oversized strings/lists/payloads -> 400
        """
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "banned_words": ["keepme"],
            "default_ai_model": "auto",
            "misc_enabled": True,
            "dashboard_rev": 0,
            "dashboard_permissions": {
                "role_mappings": {
                    # Role 555 has view permissions only!
                    "555": {
                        "allow": ["overview.view", "automod.view", "settings.view", "ai.view"],
                        "deny": [],
                    },
                }
            },
        }
        self.audit_rows.clear()

        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "AdminUser")
        mod_sid, mod_doc = await self._create_test_session(self.mod_user_id, "ViewerMod")
        outsider_sid, outsider_doc = await self._create_test_session(self.outsider_user_id, "Outsider")
        admin_csrf = admin_doc["csrf_token"]

        # 1. Unauthenticated -> 401
        r_unauth = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "update_ai_model", "model": "gemini", "dashboard_rev": 0},
        )
        self.assertEqual(r_unauth.status, 401)

        # 2. Missing or wrong CSRF token -> 403
        r_no_csrf = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "update_ai_model", "model": "gemini", "dashboard_rev": 0},
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_no_csrf.status, 403)

        r_bad_csrf = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "update_ai_model", "model": "gemini", "dashboard_rev": 0},
            headers={"X-CSRF-Token": "wrong_token"},
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_bad_csrf.status, 403)

        # 3. Outsider (non-member) -> 403
        r_outsider = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "update_ai_model", "model": "gemini", "dashboard_rev": 0},
            headers={"X-CSRF-Token": outsider_doc["csrf_token"]},
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_outsider.status, 403)

        # 4. Member with *.view only -> 403 on all 4 actions
        mod_headers = {"X-CSRF-Token": mod_doc["csrf_token"]}
        for payload in (
            {"type": "update_automod", "banned_words": ["test"], "dashboard_rev": 0},
            {"type": "update_ai_model", "model": "gemini", "dashboard_rev": 0},
            {"type": "toggle", "module": "toggleMisc", "enabled": False, "dashboard_rev": 0},
            {"type": "toggle_lockdown"},
        ):
            r_perm = await self.client.post(
                f"/api/settings/{self.guild_id}",
                json=payload,
                headers=mod_headers,
                cookies={SESSION_COOKIE_NAME: mod_sid},
            )
            self.assertEqual(r_perm.status, 403, f"Expected 403 for payload {payload}")

        # 5. Specifically verify that settings.edit WITHOUT ai.edit is denied on update_ai_model (403)
        self.guild_settings_doc["dashboard_permissions"]["role_mappings"]["555"]["allow"] = [
            "settings.view",
            "settings.edit",
            "automod.edit",
        ]
        r_no_ai_edit = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "update_ai_model", "model": "gemini", "dashboard_rev": 0},
            headers=mod_headers,
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_no_ai_edit.status, 403)

        # 6. Invalid types, extra keys, oversized entries, and hostile strings -> 400
        admin_headers = {"X-CSRF-Token": admin_csrf}
        bad_payloads = [
            # Invalid or missing type
            {},
            {"type": "unknown_action", "dashboard_rev": 0},
            {"type": 123, "dashboard_rev": 0},
            # Invalid dashboard_rev
            {"type": "update_ai_model", "model": "gemini", "dashboard_rev": True},
            {"type": "update_ai_model", "model": "gemini", "dashboard_rev": -1},
            {"type": "update_ai_model", "model": "gemini", "dashboard_rev": "0"},
            {"type": "update_ai_model", "model": "gemini"},
            # update_automod bad shapes & hostile inputs
            {"type": "update_automod", "banned_words": "not_a_list", "dashboard_rev": 0},
            {"type": "update_automod", "banned_words": [123], "dashboard_rev": 0},
            {"type": "update_automod", "banned_words": ["   "], "dashboard_rev": 0},
            {"type": "update_automod", "banned_words": ["a" * 65], "dashboard_rev": 0},
            {"type": "update_automod", "banned_words": [f"w{i}" for i in range(501)], "dashboard_rev": 0},
            {"type": "update_automod", "banned_words": ["bad\x00word"], "dashboard_rev": 0},
            {"type": "update_automod", "banned_words": ["bad\nword"], "dashboard_rev": 0},
            {
                "type": "update_automod",
                "banned_words": ["ok"],
                "automod_enabled": False,
                "dashboard_rev": 0,
            },
            # update_ai_model bad models & extra keys
            {"type": "update_ai_model", "model": "gpt-4", "dashboard_rev": 0},
            {"type": "update_ai_model", "model": "nexusify", "dashboard_rev": 0},
            {"type": "update_ai_model", "model": "AUTO", "dashboard_rev": 0},
            {"type": "update_ai_model", "model": 123, "dashboard_rev": 0},
            {
                "type": "update_ai_model",
                "model": "gemini",
                "ai_enabled": False,
                "dashboard_rev": 0,
            },
            # toggle bad module, non-bool enabled, & extra keys
            {"type": "toggle", "module": "automod_enabled", "enabled": True, "dashboard_rev": 0},
            {"type": "toggle", "module": "toggleMisc", "enabled": "true", "dashboard_rev": 0},
            {"type": "toggle", "module": "toggleMisc", "enabled": 1, "dashboard_rev": 0},
            {
                "type": "toggle",
                "module": "toggleMisc",
                "enabled": False,
                "welcome_enabled": False,
                "dashboard_rev": 0,
            },
        ]
        for bad in bad_payloads:
            self.limiter.reset()
            r_bad = await self.client.post(
                f"/api/settings/{self.guild_id}",
                json=bad,
                headers=admin_headers,
                cookies={SESSION_COOKIE_NAME: admin_sid},
            )
            self.assertEqual(r_bad.status, 400, f"Expected 400 for payload {bad}, got {r_bad.status}")

        # 7. Oversized raw body (> 65536 bytes) -> 400
        self.limiter.reset()
        oversized_raw = '{"type":"update_automod","dashboard_rev":0,"pad":"' + ("x" * 70000) + '"}'
        r_huge = await self.client.post(
            f"/api/settings/{self.guild_id}",
            data=oversized_raw.encode("utf-8"),
            headers={"Content-Type": "application/json", "X-CSRF-Token": admin_csrf},
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_huge.status, 400)

        # 8. Stored banned_words > 500 in DB -> POST update_automod is rejected with 400 so it cannot truncate
        self.limiter.reset()
        self.guild_settings_doc["banned_words"] = [f"stored_{i}" for i in range(501)]
        r_over_cap_post = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "update_automod", "banned_words": ["trimmed"], "dashboard_rev": 0},
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_over_cap_post.status, 400)
        self.assertEqual(len(self.guild_settings_doc["banned_words"]), 501)
        self.assertEqual(len(self.audit_rows), 0)

    async def test_checkpoint_3c_batch1_flaky_read_503_preserves_600_stored_words(self):
        """
        Regression test for the strict-read data-loss bug:
        - Stored 600 banned words (`dashboard_rev: 0`).
        - `db.guild_settings.find_one` fails once on the mutation read.
        - A POST save of 3 words with `dashboard_rev: 0` MUST return HTTP 503
          ("Database temporarily unavailable, please retry") BEFORE any write,
          leaving all 600 stored words untouched (never mistaking a failed read for
          `before_doc=None` / document missing).
        - Also verifies that when the document is genuinely missing (`guild_settings_doc = None`
          and `find_one` succeeds returning `None`), the `expected_rev == 0` upsert path works.
        """
        words_600 = [f"word_{i}" for i in range(600)]
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "banned_words": list(words_600),
            "dashboard_rev": 0,
        }
        self.audit_rows.clear()
        self.fake_db.guild_settings.update_one.reset_mock()

        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "AdminUser")
        headers = {"X-CSRF-Token": admin_doc["csrf_token"]}

        # Make find_one fail once, then succeed on subsequent calls
        fail_state = {"calls": 0}
        orig_find_one = self.fake_db.guild_settings.find_one.side_effect

        async def _flaky_find_one(query: dict):
            fail_state["calls"] += 1
            if fail_state["calls"] == 1:
                raise RuntimeError("Transient MongoDB read timeout")
            return orig_find_one(query)

        self.fake_db.guild_settings.find_one = AsyncMock(side_effect=_flaky_find_one)

        # 1. First POST hits the flaky find_one -> MUST return 503 and perform ZERO writes
        r_flaky = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_automod",
                "banned_words": ["one", "two", "three"],
                "dashboard_rev": 0,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_flaky.status, 503)
        flaky_json = await r_flaky.json()
        self.assertIn("temporarily unavailable", flaky_json.get("error", "").lower())
        self.fake_db.guild_settings.update_one.assert_not_awaited()
        self.assertEqual(len(self.guild_settings_doc["banned_words"]), 600)
        self.assertEqual(self.guild_settings_doc["banned_words"], words_600)
        self.assertEqual(len(self.audit_rows), 0)

        # 2. Second POST (find_one now succeeds) sees the 600 stored words and rejects with 400
        r_second = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_automod",
                "banned_words": ["one", "two", "three"],
                "dashboard_rev": 0,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_second.status, 400)
        self.fake_db.guild_settings.update_one.assert_not_awaited()
        self.assertEqual(len(self.guild_settings_doc["banned_words"]), 600)

        # 3. Genuinely missing document (find_one succeeds and returns None) -> upsert succeeds with 200
        self.guild_settings_doc = None
        r_upsert = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={
                "type": "update_automod",
                "banned_words": ["fresh1", "fresh2"],
                "dashboard_rev": 0,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_upsert.status, 200)
        self.assertEqual((await r_upsert.json())["dashboard_rev"], 1)
        self.assertIsNotNone(self.guild_settings_doc)
        self.assertEqual(self.guild_settings_doc["banned_words"], ["fresh1", "fresh2"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 1)

    async def test_checkpoint_3c_batch2_modules_page_scripts_and_permission_gates(self):
        """
        Verify Batch 3C-2 template & GET handler requirements on `/modules/{guild_id}`:
        - Renders `data-dashboard-rev` from a fresh MongoDB read (bypassing GuildConfigCache).
        - Includes `#saveModulesBtn` and `fetch("/api/modules/...")` inside `{% block scripts %}`
          for users with `modules.edit` or `commands.edit`.
        - Hides `#saveModulesBtn` and the save script when the user only has `modules.view`.
        - Contains zero inline event handlers (`on*=`).
        """
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        def _make_cog(cmds_spec: list[tuple[str, str, list[str]]]):
            cog = MagicMock()
            prefix_cmds = []
            for cname, cdesc, subs in cmds_spec:
                cmd_obj = MagicMock()
                cmd_obj.name = cname
                cmd_obj.help = cdesc
                cmd_obj.description = cdesc
                cmd_obj.commands = [MagicMock(name=s) for s in subs]
                for sub_obj, sname in zip(cmd_obj.commands, subs):
                    sub_obj.name = sname
                prefix_cmds.append(cmd_obj)
            cog.get_commands = MagicMock(return_value=prefix_cmds)
            cog.walk_app_commands = MagicMock(return_value=[])
            return cog

        self.fake_bot.cogs = {
            "Core": _make_cog([("help", "Help command", [])]),
            "Moderation": _make_cog([("ban", "Ban member", ["temp", "perm"])]),
            "Sports": _make_cog([("nba", "NBA scores", [])]),
        }

        ch_gen = MagicMock()
        ch_gen.id = 1001
        ch_gen.name = "general"
        self.fake_guild.text_channels = [ch_gen]

        r_everyone = MagicMock()
        r_everyone.id = self.guild_id
        r_everyone.name = "@everyone"
        r_mod = MagicMock()
        r_mod.id = 2001
        r_mod.name = "Moderator"
        self.fake_guild.roles = [r_everyone, r_mod]

        # Seed cache with rev=1, then set MongoDB doc to rev=9 to verify fresh read
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "disabled_cogs": ["Sports"],
            "dashboard_rev": 1,
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {
                        "allow": ["modules.view"],
                        "deny": [],
                    }
                }
            },
        }
        await self.app[GUILD_CONFIG_CACHE_KEY].get(self.guild_id)
        self.guild_settings_doc["dashboard_rev"] = 9

        admin_sid, _ = await self._create_test_session(self.admin_user_id, "AdminUser")
        mod_sid, _ = await self._create_test_session(self.mod_user_id, "ViewerMod")

        # 1. Admin (has modules.edit and commands.edit) sees save button, save script, and fresh rev=9
        r_admin = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_admin.status, 200)
        admin_page = await r_admin.text()
        self.assertIn('data-dashboard-rev="9"', admin_page)
        self.assertIn('id="saveModulesBtn"', admin_page)
        self.assertIn(f'fetch("/api/modules/{self.guild_id}"', admin_page)
        self.assertIn("getCsrfToken()", admin_page)
        self.assertIsNone(INLINE_HANDLER_RE.search(admin_page))

        # 2. ViewerMod (has modules.view only) does NOT see save button or save script
        r_viewer = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_viewer.status, 200)
        viewer_page = await r_viewer.text()
        self.assertIn('data-dashboard-rev="9"', viewer_page)
        self.assertNotIn('id="saveModulesBtn"', viewer_page)
        self.assertNotIn(f'fetch("/api/modules/{self.guild_id}"', viewer_page)
        self.assertIn("View-Only Access", viewer_page)
        self.assertIsNone(INLINE_HANDLER_RE.search(viewer_page))

    async def test_checkpoint_3c_batch2_modules_mutations_merge_concurrency_and_audit(self):
        """
        Verify Batch 3C-2 happy-path mutations on `POST /api/modules/{guild_id}`:
        - `disabled_cogs`: exact-case cog names; protected cogs can be re-enabled if already
          present in `disabled_cogs` and thereafter cannot be added again.
        - `command_settings`: per-command merge (never replaces the whole dict);
          commands absent from the payload (including protected-cog commands like `help`
          and stale/unloaded commands like `legacy_cmd`) keep their stored settings;
          an entry whose 5 lists are all empty removes ONLY that command;
          all channel and role IDs are stored as STRINGS (`cogs/core.py:761-764`).
        - Increments `dashboard_rev`, enforces 409 on stale revision, invalidates
          `GuildConfigCache` + `ai_cog._invalidate_guild_cfg`, and records non-empty
          `AuditLogger` rows for every mutation.
        """
        from webdash.router import GUILD_CONFIG_CACHE_KEY

        def _make_cog(cmds_spec: list[tuple[str, str, list[str]]]):
            cog = MagicMock()
            prefix_cmds = []
            for cname, cdesc, subs in cmds_spec:
                cmd_obj = MagicMock()
                cmd_obj.name = cname
                cmd_obj.help = cdesc
                cmd_obj.description = cdesc
                sub_objs = []
                for sname in subs:
                    sobj = MagicMock()
                    sobj.name = sname
                    sub_objs.append(sobj)
                cmd_obj.commands = sub_objs
                prefix_cmds.append(cmd_obj)
            cog.get_commands = MagicMock(return_value=prefix_cmds)
            cog.walk_app_commands = MagicMock(return_value=[])
            return cog

        self.fake_bot.cogs = {
            "Core": _make_cog([("help", "Help menu", [])]),
            "Database": _make_cog([]),
            "Dashboard": _make_cog([]),
            "Owner": _make_cog([("eval", "Owner eval", [])]),
            "Jishaku": _make_cog([]),
            "Moderation": _make_cog([("ban", "Ban member", ["temp", "perm"]), ("kick", "Kick member", [])]),
            "Sports": _make_cog([("nba", "NBA scores", ["live"])]),
        }

        ch_gen = MagicMock()
        ch_gen.id = 1001
        ch_gen.name = "general"
        ch_bot = MagicMock()
        ch_bot.id = 1002
        ch_bot.name = "bot-commands"
        self.fake_guild.text_channels = [ch_gen, ch_bot]

        r_everyone = MagicMock()
        r_everyone.id = self.guild_id
        r_everyone.name = "@everyone"
        r_mod = MagicMock()
        r_mod.id = 2001
        r_mod.name = "Moderator"
        r_muted = MagicMock()
        r_muted.id = 2002
        r_muted.name = "Muted"
        self.fake_guild.roles = [r_everyone, r_mod, r_muted]

        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "disabled_cogs": ["Core", "Sports"],
            "command_settings": {
                # Protected cog command (absent from UI payloads -> must be preserved!)
                "help": {
                    "disabled_subcommands": [],
                    "allowed_channels": ["1001"],
                    "ignored_channels": [],
                    "allowed_roles": [],
                    "ignored_roles": [],
                },
                # Stale/unloaded command (absent from UI payloads -> must be preserved!)
                "legacy_unloaded_cmd": {
                    "disabled_subcommands": ["oldsub"],
                    "allowed_channels": [],
                    "ignored_channels": ["1002"],
                    "allowed_roles": [],
                    "ignored_roles": ["2002"],
                },
                # Configurable commands
                "ban": {
                    "disabled_subcommands": ["temp"],
                    "allowed_channels": ["1001"],
                    "ignored_channels": [],
                    "allowed_roles": ["2001"],
                    "ignored_roles": [],
                },
                "nba": {
                    "disabled_subcommands": [],
                    "allowed_channels": ["1002"],
                    "ignored_channels": [],
                    "allowed_roles": [],
                    "ignored_roles": [],
                },
            },
            "dashboard_rev": 0,
        }
        self.audit_rows.clear()
        self.fake_ai_cog._invalidate_guild_cfg.reset_mock()

        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "AdminUser")
        headers = {
            "X-CSRF-Token": admin_doc["csrf_token"],
            "X-Forwarded-For": "198.51.100.99",
        }

        cfg_cache = self.app[GUILD_CONFIG_CACHE_KEY]
        await cfg_cache.get(self.guild_id)
        self.assertEqual(len(cfg_cache), 1)

        # 1. Re-enable protected "Core" and disable "Moderation" (rev 0 -> 1)
        r1 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "disabled_cogs": ["Sports", "Moderation"],
                "dashboard_rev": 0,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r1.status, 200)
        d1 = await r1.json()
        self.assertEqual(d1, {"status": "ok", "dashboard_rev": 1})
        self.assertEqual(self.guild_settings_doc["disabled_cogs"], ["Sports", "Moderation"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 1)
        self.assertEqual(len(cfg_cache), 0)
        self.fake_ai_cog._invalidate_guild_cfg.assert_called_once_with(self.guild_id)
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(self.audit_rows[-1]["module"], "modules")
        self.assertEqual(self.audit_rows[-1]["action"], "update_disabled_cogs")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"disabled_cogs": {"old": ["Core", "Sports"], "new": ["Sports", "Moderation"]}},
        )

        # 2. Now that "Core" is no longer in disabled_cogs, attempting to add "Core" back is rejected with 400
        r_readd_core = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "disabled_cogs": ["Core", "Sports"],
                "dashboard_rev": 1,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_readd_core.status, 400)
        self.assertEqual(self.guild_settings_doc["disabled_cogs"], ["Sports", "Moderation"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 1)

        # 3. Per-command merge of `command_settings`: update only "ban", omitting "help",
        #    "legacy_unloaded_cmd", and "nba" -> absent commands keep their stored settings!
        self.fake_ai_cog._invalidate_guild_cfg.reset_mock()
        r2 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "command_settings": {
                    "ban": {
                        "disabled_subcommands": ["perm"],
                        "allowed_channels": ["1001", "1002"],
                        "ignored_channels": [],
                        "allowed_roles": ["2001"],
                        "ignored_roles": ["2002"],
                    }
                },
                "dashboard_rev": 1,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r2.status, 200)
        self.assertEqual((await r2.json())["dashboard_rev"], 2)
        stored_cmds = self.guild_settings_doc["command_settings"]
        # Verify "help", "legacy_unloaded_cmd", and "nba" were preserved untouched
        self.assertIn("help", stored_cmds)
        self.assertEqual(stored_cmds["help"]["allowed_channels"], ["1001"])
        self.assertIn("legacy_unloaded_cmd", stored_cmds)
        self.assertEqual(stored_cmds["legacy_unloaded_cmd"]["ignored_roles"], ["2002"])
        self.assertIn("nba", stored_cmds)
        self.assertEqual(stored_cmds["nba"]["allowed_channels"], ["1002"])
        # Verify "ban" was updated and all channel/role IDs are stored as STRINGS
        self.assertEqual(
            stored_cmds["ban"],
            {
                "disabled_subcommands": ["perm"],
                "allowed_channels": ["1001", "1002"],
                "ignored_channels": [],
                "allowed_roles": ["2001"],
                "ignored_roles": ["2002"],
            },
        )
        for id_val in stored_cmds["ban"]["allowed_channels"] + stored_cmds["ban"]["allowed_roles"] + stored_cmds["ban"]["ignored_roles"]:
            self.assertIsInstance(id_val, str)
        self.fake_ai_cog._invalidate_guild_cfg.assert_called_once_with(self.guild_id)
        self.assertEqual(len(self.audit_rows), 2)
        self.assertEqual(self.audit_rows[-1]["action"], "update_command_settings")
        self.assertTrue(bool(self.audit_rows[-1]["changes"]))

        # 4. Empty-lists removal rule: sending "nba" with all 5 lists empty removes ONLY "nba"
        r3 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "command_settings": {
                    "nba": {
                        "disabled_subcommands": [],
                        "allowed_channels": [],
                        "ignored_channels": [],
                        "allowed_roles": [],
                        "ignored_roles": [],
                    }
                },
                "dashboard_rev": 2,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r3.status, 200)
        self.assertEqual((await r3.json())["dashboard_rev"], 3)
        stored_cmds_after = self.guild_settings_doc["command_settings"]
        self.assertNotIn("nba", stored_cmds_after)
        self.assertIn("ban", stored_cmds_after)
        self.assertIn("help", stored_cmds_after)
        self.assertIn("legacy_unloaded_cmd", stored_cmds_after)
        self.assertEqual(len(self.audit_rows), 3)

        # 5. Saving identical `disabled_cogs` still records an audit row with non-empty `changes` (rev 3 -> 4)
        r4 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "disabled_cogs": ["Sports", "Moderation"],
                "dashboard_rev": 3,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r4.status, 200)
        self.assertEqual((await r4.json())["dashboard_rev"], 4)
        self.assertEqual(len(self.audit_rows), 4)
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"disabled_cogs": {"old": ["Sports", "Moderation"], "new": ["Sports", "Moderation"]}},
        )

        # 6. Stale `dashboard_rev` (sending rev=2 when DB is at 4) -> 409 Conflict
        r_conflict = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "disabled_cogs": [],
                "dashboard_rev": 2,
            },
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_conflict.status, 409)
        self.assertIn("reload", (await r_conflict.json()).get("error", "").lower())
        self.assertEqual(self.guild_settings_doc["disabled_cogs"], ["Sports", "Moderation"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 4)
        self.assertEqual(len(self.audit_rows), 4)

    async def test_checkpoint_3c_batch2_modules_attack_vectors_and_flaky_read(self):
        """
        Attack & validation tests for `POST /api/modules/{guild_id}`:
        - Unauthenticated -> 401; missing/wrong CSRF -> 403; outsider -> 403
        - Granular RBAC split between `modules.edit` and `commands.edit`
        - Flaky `find_one` read failure -> 503 BEFORE any write, leaving stored doc unchanged
        - Protected cogs (`Core`, `core`, `Database`, `Dashboard`, `Owner`, `Jishaku`) rejected -> 400
        - Wrong-case cog names (`moderation`, `SPORTS`) or unknown cogs rejected -> 400
        - Non-dict `command_settings`, non-dict command entries, integer channel/role IDs,
          cross-guild channel/role IDs, `@everyone` role ID, unknown subcommands,
          protected-cog commands (`help`), and extra keys rejected -> 400
        """
        def _make_cog(cmds_spec: list[tuple[str, str, list[str]]]):
            cog = MagicMock()
            prefix_cmds = []
            for cname, cdesc, subs in cmds_spec:
                cmd_obj = MagicMock()
                cmd_obj.name = cname
                cmd_obj.help = cdesc
                cmd_obj.description = cdesc
                sub_objs = []
                for sname in subs:
                    sobj = MagicMock()
                    sobj.name = sname
                    sub_objs.append(sobj)
                cmd_obj.commands = sub_objs
                prefix_cmds.append(cmd_obj)
            cog.get_commands = MagicMock(return_value=prefix_cmds)
            cog.walk_app_commands = MagicMock(return_value=[])
            return cog

        self.fake_bot.cogs = {
            "Core": _make_cog([("help", "Help menu", [])]),
            "Owner": _make_cog([("eval", "Eval", [])]),
            "Moderation": _make_cog([("ban", "Ban member", ["temp", "perm"])]),
            "Sports": _make_cog([("nba", "NBA scores", [])]),
        }

        ch_gen = MagicMock()
        ch_gen.id = 1001
        ch_gen.name = "general"
        self.fake_guild.text_channels = [ch_gen]

        r_everyone = MagicMock()
        r_everyone.id = self.guild_id
        r_everyone.name = "@everyone"
        r_mod = MagicMock()
        r_mod.id = 2001
        r_mod.name = "Moderator"
        self.fake_guild.roles = [r_everyone, r_mod]

        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "disabled_cogs": ["Sports"],
            "command_settings": {
                "ban": {
                    "disabled_subcommands": ["temp"],
                    "allowed_channels": ["1001"],
                    "ignored_channels": [],
                    "allowed_roles": ["2001"],
                    "ignored_roles": [],
                }
            },
            "dashboard_rev": 0,
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {
                        "allow": ["modules.view", "modules.edit"],
                        "deny": [],
                    }
                }
            },
        }
        self.audit_rows.clear()

        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "AdminUser")
        mod_sid, mod_doc = await self._create_test_session(self.mod_user_id, "ModEditor")
        outsider_sid, outsider_doc = await self._create_test_session(self.outsider_user_id, "Outsider")
        admin_headers = {"X-CSRF-Token": admin_doc["csrf_token"]}
        mod_headers = {"X-CSRF-Token": mod_doc["csrf_token"]}

        # 1. Unauthenticated -> 401; missing CSRF -> 403; outsider -> 403
        r_unauth = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"disabled_cogs": [], "dashboard_rev": 0},
        )
        self.assertEqual(r_unauth.status, 401)

        r_no_csrf = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"disabled_cogs": [], "dashboard_rev": 0},
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_no_csrf.status, 403)

        r_outsider = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"disabled_cogs": [], "dashboard_rev": 0},
            headers={"X-CSRF-Token": outsider_doc["csrf_token"]},
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_outsider.status, 403)

        # 2. Granular RBAC split: Role 555 has `modules.edit` ONLY (lacks `commands.edit`)
        #    -> Sending `command_settings` is rejected with 403, while `disabled_cogs` is allowed (200)
        r_no_cmd_edit = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"command_settings": {"ban": {"allowed_channels": ["1001"]}}, "dashboard_rev": 0},
            headers=mod_headers,
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_no_cmd_edit.status, 403)

        # Flip Role 555 to `commands.edit` ONLY (lacks `modules.edit`)
        self.guild_settings_doc["dashboard_permissions"]["role_mappings"]["555"]["allow"] = [
            "modules.view",
            "commands.edit",
        ]
        r_no_mod_edit = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"disabled_cogs": ["Moderation"], "dashboard_rev": 0},
            headers=mod_headers,
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_no_mod_edit.status, 403)

        # 3. Flaky MongoDB read (`find_one` fails once) -> returns 503 BEFORE any write
        self.fake_db.guild_settings.update_one.reset_mock()
        orig_find_one = self.fake_db.guild_settings.find_one.side_effect
        flaky_counter = {"n": 0}

        async def _flaky_find(q: dict):
            flaky_counter["n"] += 1
            if flaky_counter["n"] == 1:
                raise RuntimeError("Simulated MongoDB outage")
            return orig_find_one(q)

        self.fake_db.guild_settings.find_one = AsyncMock(side_effect=_flaky_find)
        r_503 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"disabled_cogs": [], "dashboard_rev": 0},
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_503.status, 503)
        self.fake_db.guild_settings.update_one.assert_not_awaited()
        self.assertEqual(self.guild_settings_doc["disabled_cogs"], ["Sports"])

        # 4. Attack & validation payloads -> all rejected with 400 and zero DB mutations
        bad_payloads = [
            # Empty / missing required keys / extra keys
            {},
            {"dashboard_rev": 0},
            {"disabled_cogs": [], "extra_key": True, "dashboard_rev": 0},
            # Bad dashboard_rev
            {"disabled_cogs": [], "dashboard_rev": -1},
            {"disabled_cogs": [], "dashboard_rev": True},
            {"disabled_cogs": [], "dashboard_rev": "0"},
            # Protected cogs in disabled_cogs (exact case and lowercase)
            {"disabled_cogs": ["Core"], "dashboard_rev": 0},
            {"disabled_cogs": ["core"], "dashboard_rev": 0},
            {"disabled_cogs": ["Database"], "dashboard_rev": 0},
            {"disabled_cogs": ["Dashboard"], "dashboard_rev": 0},
            {"disabled_cogs": ["Owner"], "dashboard_rev": 0},
            {"disabled_cogs": ["Jishaku"], "dashboard_rev": 0},
            # Wrong-case cog names & unknown cogs
            {"disabled_cogs": ["moderation"], "dashboard_rev": 0},
            {"disabled_cogs": ["SPORTS"], "dashboard_rev": 0},
            {"disabled_cogs": ["NonExistentCog"], "dashboard_rev": 0},
            {"disabled_cogs": "Sports", "dashboard_rev": 0},
            {"disabled_cogs": [123], "dashboard_rev": 0},
            {"disabled_cogs": ["Moderation\x00"], "dashboard_rev": 0},
            # Non-dict command_settings & non-dict per-command values
            {"command_settings": ["ban"], "dashboard_rev": 0},
            {"command_settings": "ban", "dashboard_rev": 0},
            {"command_settings": {"ban": "not_a_dict"}, "dashboard_rev": 0},
            {"command_settings": {"ban": []}, "dashboard_rev": 0},
            {"command_settings": {"ban": {}}, "dashboard_rev": 0},
            # Protected cog command or unknown command
            {"command_settings": {"help": {"allowed_channels": ["1001"]}}, "dashboard_rev": 0},
            {"command_settings": {"eval": {"allowed_channels": ["1001"]}}, "dashboard_rev": 0},
            {"command_settings": {"unknown_cmd": {"allowed_channels": ["1001"]}}, "dashboard_rev": 0},
            # Unsupported per-command key (e.g. legacy "disabled": True)
            {"command_settings": {"ban": {"disabled": True}}, "dashboard_rev": 0},
            # Unknown subcommand
            {"command_settings": {"ban": {"disabled_subcommands": ["fake_sub"]}}, "dashboard_rev": 0},
            # Integer IDs instead of strings
            {"command_settings": {"ban": {"allowed_channels": [1001]}}, "dashboard_rev": 0},
            {"command_settings": {"ban": {"allowed_roles": [2001]}}, "dashboard_rev": 0},
            # Non-existent / cross-guild channel or role IDs
            {"command_settings": {"ban": {"allowed_channels": ["999999"]}}, "dashboard_rev": 0},
            {"command_settings": {"ban": {"ignored_channels": ["999999"]}}, "dashboard_rev": 0},
            {"command_settings": {"ban": {"allowed_roles": ["888888"]}}, "dashboard_rev": 0},
            {"command_settings": {"ban": {"ignored_roles": ["888888"]}}, "dashboard_rev": 0},
            # @everyone role ID
            {"command_settings": {"ban": {"allowed_roles": [str(self.guild_id)]}}, "dashboard_rev": 0},
            {"command_settings": {"ban": {"ignored_roles": [str(self.guild_id)]}}, "dashboard_rev": 0},
            # Control characters in list items
            {"command_settings": {"ban": {"allowed_channels": ["1001\n"]}}, "dashboard_rev": 0},
        ]

        for bad in bad_payloads:
            self.limiter.reset()
            r_bad = await self.client.post(
                f"/api/modules/{self.guild_id}",
                json=bad,
                headers=admin_headers,
                cookies={SESSION_COOKIE_NAME: admin_sid},
            )
            self.assertEqual(
                r_bad.status,
                400,
                f"Expected 400 for payload {bad}, got {r_bad.status}",
            )

        self.assertEqual(self.guild_settings_doc["disabled_cogs"], ["Sports"])
        self.assertEqual(self.guild_settings_doc["dashboard_rev"], 0)
        self.assertEqual(len(self.audit_rows), 0)

    async def test_checkpoint_3c_batch3_owner_panel_scripts_and_buttons(self):
        """
        Verify Batch 3C-3 `owner_panel.html` rendering:
        - Includes `getCsrfToken()`, `fetch("/api/owner/action"...)`, and `fetch("/api/owner/ip_action"...)`.
        - Disables reload buttons for `dashboard` and `database` while enabling normal extensions.
        - Disables ban button on the requester's own current IP (`127.0.0.1` in local test harness)
          and on `'invalid'` IPs.
        - Contains zero inline event handlers (`on*=`).
        """
        self.fake_bot.extensions = {
            "cogs.admin": MagicMock(),
            "cogs.dashboard": MagicMock(),
            "cogs.database": MagicMock(),
            "cogs.moderation": MagicMock(),
        }

        class _AsyncListCursor:
            def __init__(self, items: list[dict]):
                self._items = list(items)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, _n: int):
                return self

            def __aiter__(self):
                self._idx = 0
                return self

            async def __anext__(self):
                if self._idx >= len(self._items):
                    raise StopAsyncIteration
                val = self._items[self._idx]
                self._idx += 1
                return val

        self.fake_db.visit_logs.find = MagicMock(
            return_value=_AsyncListCursor(
                [
                    {"ip": "127.0.0.1", "last_user": "OwnerSelf", "hits": 5},
                    {"ip": "203.0.113.50", "last_user": "SomeVisitor", "hits": 2},
                ]
            )
        )
        self.ip_ban_docs = {"198.51.100.77": {"ip": "198.51.100.77"}}

        owner_sid, _ = await self._create_test_session(self.bot_owner_id, "BotOwner")
        resp = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(resp.status, 200)
        page_body = await resp.text()

        self.assertIn("function getCsrfToken()", page_body)
        self.assertIn('fetch("/api/owner/action"', page_body)
        self.assertIn('fetch("/api/owner/ip_action"', page_body)
        self.assertIn("Discord Only", page_body)
        self.assertIn("Your IP", page_body)
        self.assertIsNone(INLINE_HANDLER_RE.search(page_body))

    async def test_checkpoint_3c_batch3_owner_reload_cog_action_and_attacks(self):
        """
        Verify `POST /api/owner/action` (`reload_cog` only):
        - Unauthenticated -> 401; missing/wrong CSRF -> 403; non-owner guild Admin -> 403;
          outsider -> 403.
        - Rejects `dashboard` and `database` (with or without `cogs.` prefix) with 400 and
          instructs the user to use the Discord owner command.
        - Enforces `^[a-z0-9_]{1,32}$` regex and requires `"cogs.<name>" in bot.extensions`.
        - Calls `await bot.reload_extension("cogs.<name>")` and writes a non-empty audit row
          (`changes={"cog": {"old": ext_name, "new": ext_name}}`).
        """
        self.fake_bot.extensions = {
            "cogs.admin": MagicMock(),
            "cogs.moderation": MagicMock(),
            "cogs.dashboard": MagicMock(),
            "cogs.database": MagicMock(),
        }
        self.fake_bot.reload_extension = AsyncMock(return_value=None)
        self.audit_rows.clear()

        owner_sid, owner_doc = await self._create_test_session(self.bot_owner_id, "BotOwner")
        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "GuildAdmin")
        outsider_sid, outsider_doc = await self._create_test_session(
            self.outsider_user_id, "Outsider"
        )
        owner_csrf = owner_doc["csrf_token"]
        admin_csrf = admin_doc["csrf_token"]
        outsider_csrf = outsider_doc["csrf_token"]
        owner_headers = {"X-CSRF-Token": owner_csrf}

        # 1. Unauthenticated -> 401
        r_unauth = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "admin"},
        )
        self.assertEqual(r_unauth.status, 401)

        # 2. Missing / wrong CSRF -> 403
        r_no_csrf = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "admin"},
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_no_csrf.status, 403)

        r_bad_csrf = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "admin"},
            headers={"X-CSRF-Token": "wrong_csrf"},
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_bad_csrf.status, 403)

        # 3. Non-owner Guild Admin and Outsider -> 403
        r_admin = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "admin"},
            headers={"X-CSRF-Token": admin_csrf},
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_admin.status, 403)

        r_outsider = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "admin"},
            headers={"X-CSRF-Token": outsider_csrf},
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_outsider.status, 403)
        self.assertEqual(self.fake_bot.reload_extension.await_count, 0)

        # 4. Rejects `dashboard` and `database` (both short and `cogs.`-prefixed) and tells user to use Discord owner command
        for blocked_cog in ("dashboard", "cogs.dashboard", "database", "cogs.database"):
            self.limiter.reset()
            r_blocked = await self.client.post(
                "/api/owner/action",
                json={"action": "reload_cog", "cog": blocked_cog},
                headers=owner_headers,
                cookies={SESSION_COOKIE_NAME: owner_sid},
            )
            self.assertEqual(r_blocked.status, 400)
            err_msg = (await r_blocked.json()).get("error", "")
            self.assertIn("Discord owner command", err_msg)
        self.assertEqual(self.fake_bot.reload_extension.await_count, 0)

        # 5. Hostile / invalid cog names, unloaded extensions, and unsupported actions -> 400
        hostile_payloads = [
            [],
            "not_an_object",
            {},
            {"action": "eval", "cog": "admin"},
            {"action": "reload_cog"},
            {"action": "reload_cog", "cog": 123},
            {"action": "reload_cog", "cog": ""},
            {"action": "reload_cog", "cog": "../secrets"},
            {"action": "reload_cog", "cog": "cogs.__init__"},
            {"action": "reload_cog", "cog": "Admin"},  # uppercase rejected by ^[a-z0-9_]{1,32}$
            {"action": "reload_cog", "cog": "admin;rm -rf"},
            {"action": "reload_cog", "cog": "a" * 33},
            {"action": "reload_cog", "cog": "unloaded_cog"},
            {"action": "reload_cog", "cog": "admin", "extra": 1},
        ]
        for bad in hostile_payloads:
            self.limiter.reset()
            r_bad = await self.client.post(
                "/api/owner/action",
                json=bad,
                headers=owner_headers,
                cookies={SESSION_COOKIE_NAME: owner_sid},
            )
            self.assertEqual(r_bad.status, 400, f"Expected 400 for {bad}, got {r_bad.status}")
        self.assertEqual(self.fake_bot.reload_extension.await_count, 0)
        self.assertEqual(len(self.audit_rows), 0)

        # 6. Valid reload of `admin` and `cogs.moderation` -> 200 OK + non-empty audit row for each
        self.limiter.reset()
        r_ok1 = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "admin"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_ok1.status, 200)
        self.assertEqual(await r_ok1.json(), {"status": "ok", "reloaded": "cogs.admin"})
        self.fake_bot.reload_extension.assert_awaited_once_with("cogs.admin")
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(self.audit_rows[-1]["guild_id"], 0)
        self.assertEqual(self.audit_rows[-1]["module"], "owner")
        self.assertEqual(self.audit_rows[-1]["action"], "reload_cog")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"cog": {"old": "cogs.admin", "new": "cogs.admin"}},
        )

        r_ok2 = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "cogs.moderation"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_ok2.status, 200)
        self.assertEqual(await r_ok2.json(), {"status": "ok", "reloaded": "cogs.moderation"})
        self.assertEqual(len(self.audit_rows), 2)
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"cog": {"old": "cogs.moderation", "new": "cogs.moderation"}},
        )

    async def test_checkpoint_3c_batch3_owner_ip_ban_unban_strict_read_cache_and_attacks(self):
        """
        Verify `POST /api/owner/ip_action` + `IpBanCache` enforcement in `create_security_middleware`:
        - Only Bot Owner allowed (401 unauth, 403 CSRF, 403 non-owner admin/outsider).
        - Validates IPs via `normalize_ip` and rejects `'invalid'` / hostile strings with 400.
        - Self-ban protection: refuses banning the requester's own current IP (`request[CLIENT_IP_KEY]`).
        - Strict pre-write read on `db.ip_bans`: returns 503 BEFORE any write when `find_one` fails/times out.
        - Banning an IP immediately updates `IpBanCache` and blocks subsequent requests when the
          client IP is TRUSTED (`X-Recluse-Origin-Secret` + `X-Recluse-Client-IP`), while:
          * NEVER blocking `GET /health` or unauthenticated `GET /`
          * NEVER blocking when the client IP is UNTRUSTED (spoofed `X-Forwarded-For` without trusted hops/secret)
        - Unbanning immediately updates `IpBanCache` and restores access.
        - Both `ban` and `unban` write non-empty audit rows (`{"ip": {"old": None, "new": ip}}` and
          `{"ip": {"old": ip, "new": None}}`).
        """
        from webdash.router import IP_BAN_CACHE_KEY

        self.ip_ban_docs.clear()
        self.audit_rows.clear()
        ip_ban_cache = self.app[IP_BAN_CACHE_KEY]
        ip_ban_cache.clear()

        owner_sid, owner_doc = await self._create_test_session(self.bot_owner_id, "BotOwner")
        admin_sid, admin_doc = await self._create_test_session(self.admin_user_id, "GuildAdmin")
        outsider_sid, outsider_doc = await self._create_test_session(
            self.outsider_user_id, "Outsider"
        )
        owner_csrf = owner_doc["csrf_token"]
        admin_csrf = admin_doc["csrf_token"]
        outsider_csrf = outsider_doc["csrf_token"]
        owner_headers = {"X-CSRF-Token": owner_csrf}

        # 1. Auth & RBAC & CSRF checks
        r_unauth = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "203.0.113.55"},
        )
        self.assertEqual(r_unauth.status, 401)

        r_no_csrf = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "203.0.113.55"},
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_no_csrf.status, 403)

        r_admin = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "203.0.113.55"},
            headers={"X-CSRF-Token": admin_csrf},
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_admin.status, 403)

        r_outsider = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "203.0.113.55"},
            headers={"X-CSRF-Token": outsider_csrf},
            cookies={SESSION_COOKIE_NAME: outsider_sid},
        )
        self.assertEqual(r_outsider.status, 403)

        # 2. Self-ban refusal: in local AioHTTPTestCase, peername is 127.0.0.1 (or ::ffff:127.0.0.1 -> 127.0.0.1)
        for self_ip in ("127.0.0.1", "::ffff:127.0.0.1"):
            self.limiter.reset()
            r_self = await self.client.post(
                "/api/owner/ip_action",
                json={"action": "ban", "ip": self_ip},
                headers=owner_headers,
                cookies={SESSION_COOKIE_NAME: owner_sid},
            )
            self.assertEqual(r_self.status, 400)
            self.assertIn("own current IP", (await r_self.json()).get("error", ""))
        self.assertEqual(len(self.ip_ban_docs), 0)

        # 3. Hostile / invalid IP payloads -> 400
        hostile_ip_payloads = [
            [],
            {},
            {"action": "ban"},
            {"action": "freeze", "ip": "203.0.113.55"},
            {"action": "ban", "ip": 12345},
            {"action": "ban", "ip": ""},
            {"action": "ban", "ip": "invalid"},
            {"action": "ban", "ip": "999.999.999.999"},
            {"action": "ban", "ip": "203.0.113.55:8080"},
            {"action": "ban", "ip": '203.0.113.55" onclick="alert(1)'},
            {"action": "ban", "ip": "203.0.113.55\r\nSet-Cookie: evil=1"},
            {"action": "ban", "ip": "203.0.113.55", "extra": True},
        ]
        for bad in hostile_ip_payloads:
            self.limiter.reset()
            r_bad = await self.client.post(
                "/api/owner/ip_action",
                json=bad,
                headers=owner_headers,
                cookies={SESSION_COOKIE_NAME: owner_sid},
            )
            self.assertEqual(r_bad.status, 400, f"Expected 400 for {bad}, got {r_bad.status}")
        self.assertEqual(len(self.ip_ban_docs), 0)
        self.assertEqual(len(self.audit_rows), 0)

        # 4. Flapping DB strict read test: if find_one fails once, returns 503 BEFORE any write
        orig_find_one = self.fake_db.ip_bans.find_one
        flaky_calls = 0

        async def _flaky_ip_find_one(q: dict):
            nonlocal flaky_calls
            flaky_calls += 1
            if flaky_calls == 1:
                raise RuntimeError("Transient Mongo read error on ip_bans")
            return await orig_find_one(q)

        self.fake_db.ip_bans.find_one = AsyncMock(side_effect=_flaky_ip_find_one)
        self.fake_db.ip_bans.update_one.reset_mock()

        self.limiter.reset()
        r_503 = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "203.0.113.88"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_503.status, 503)
        self.assertIn("Database temporarily unavailable", (await r_503.json()).get("error", ""))
        self.assertEqual(self.fake_db.ip_bans.update_one.await_count, 0)
        self.assertNotIn("203.0.113.88", self.ip_ban_docs)
        self.assertEqual(len(self.audit_rows), 0)

        # 5. Next call succeeds -> bans ::ffff:203.0.113.88 (normalized to 203.0.113.88), refreshes IpBanCache immediately, and logs audit row
        r_ban = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "::ffff:203.0.113.88"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_ban.status, 200)
        self.assertEqual(
            await r_ban.json(),
            {"status": "ok", "action": "ban", "ip": "203.0.113.88"},
        )
        self.assertIn("203.0.113.88", self.ip_ban_docs)
        self.assertIn("203.0.113.88", ip_ban_cache.banned_ips)
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(self.audit_rows[-1]["guild_id"], 0)
        self.assertEqual(self.audit_rows[-1]["module"], "owner")
        self.assertEqual(self.audit_rows[-1]["action"], "ban_ip")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"ip": {"old": None, "new": "203.0.113.88"}},
        )

        # 6. Verify middleware enforcement of IpBanCache:
        #    (a) TRUSTED client IP (via X-Recluse-Origin-Secret == "test_worker_secret") matching 203.0.113.88 is blocked with 403 on normal routes
        trusted_banned_headers = {
            "X-Recluse-Origin-Secret": "test_worker_secret",
            "X-Recluse-Client-IP": "203.0.113.88",
        }
        r_blocked_page = await self.client.get(
            f"/manage/{self.guild_id}",
            headers=trusted_banned_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_blocked_page.status, 403)

        r_blocked_login = await self.client.get(
            "/login",
            headers=trusted_banned_headers,
            allow_redirects=False,
        )
        self.assertEqual(r_blocked_login.status, 403)

        #    (b) GET /health and unauthenticated GET / are NEVER blocked even for a banned trusted IP
        r_health = await self.client.get("/health", headers=trusted_banned_headers)
        self.assertEqual(r_health.status, 200)

        r_unauth_root = await self.client.get("/", headers=trusted_banned_headers)
        self.assertEqual(r_unauth_root.status, 200)

        #    (c) UNTRUSTED client IP (spoofed X-Forwarded-For without valid origin secret) is NOT blocked
        spoofed_untrusted_headers = {
            "X-Forwarded-For": "203.0.113.88",
        }
        r_untrusted_ok = await self.client.get(
            f"/manage/{self.guild_id}",
            headers=spoofed_untrusted_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_untrusted_ok.status, 200)

        # 7. Unban 203.0.113.88 -> immediately removes from DB & IpBanCache, restores trusted access, and logs audit row
        self.limiter.reset()
        r_unban = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "unban", "ip": "203.0.113.88"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_unban.status, 200)
        self.assertEqual(
            await r_unban.json(),
            {"status": "ok", "action": "unban", "ip": "203.0.113.88"},
        )
        self.assertNotIn("203.0.113.88", self.ip_ban_docs)
        self.assertNotIn("203.0.113.88", ip_ban_cache.banned_ips)
        self.assertEqual(len(self.audit_rows), 2)
        self.assertEqual(self.audit_rows[-1]["action"], "unban_ip")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"ip": {"old": "203.0.113.88", "new": None}},
        )

        r_restored = await self.client.get(
            f"/manage/{self.guild_id}",
            headers=trusted_banned_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_restored.status, 200)

    async def test_checkpoint_3c_batch3_slow_reload_not_cancelled_and_ip_noop_skips_audit(self):
        """
        Verify Batch 3C-3 follow-up fixes:
        1. `POST /api/owner/action` runs `bot.reload_extension(...)` as a task shielded via
           `asyncio.shield(task)`: on timeout, returns 202 `{"status": "still running"}`
           WITHOUT cancelling the reload task, which completes cleanly in the background.
        2. `POST /api/owner/ip_action` skips the audit row and returns a clear message
           (`status == "noop"`) when banning an already-banned IP or unbanning an IP that
           is not currently banned.
        """
        import webdash.router as router_mod

        self.fake_bot.extensions = {
            "cogs.admin": MagicMock(),
            "cogs.moderation": MagicMock(),
        }
        self.ip_ban_docs.clear()
        self.audit_rows.clear()

        owner_sid, owner_doc = await self._create_test_session(self.bot_owner_id, "BotOwner")
        owner_headers = {"X-CSRF-Token": owner_doc["csrf_token"]}

        # --- Fix 1: Slow reload is NOT cancelled on timeout ---
        reload_started = asyncio.Event()
        release_reload = asyncio.Event()
        reload_completed = asyncio.Event()
        reload_cancelled = False

        async def _slow_reload(ext_name: str):
            nonlocal reload_cancelled
            reload_started.set()
            try:
                await release_reload.wait()
                reload_completed.set()
            except asyncio.CancelledError:
                reload_cancelled = True
                raise

        self.fake_bot.reload_extension = AsyncMock(side_effect=_slow_reload)

        with patch.object(router_mod, "OWNER_RELOAD_TIMEOUT_SECONDS", 0.05):
            r_slow = await self.client.post(
                "/api/owner/action",
                json={"action": "reload_cog", "cog": "admin"},
                headers=owner_headers,
                cookies={SESSION_COOKIE_NAME: owner_sid},
            )

        self.assertEqual(r_slow.status, 202)
        slow_json = await r_slow.json()
        self.assertEqual(slow_json["status"], "still running")
        self.assertEqual(slow_json["reloaded"], "cogs.admin")
        self.assertTrue(reload_started.is_set())
        self.assertFalse(reload_cancelled, "Slow reload_extension must NOT be cancelled on timeout!")
        self.assertFalse(reload_completed.is_set())
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(self.audit_rows[-1]["action"], "reload_cog")

        # Let the background reload finish and confirm it completed without cancellation
        release_reload.set()
        await asyncio.wait_for(reload_completed.wait(), timeout=1.0)
        self.assertFalse(reload_cancelled)
        self.assertTrue(reload_completed.is_set())

        # --- Fix 2: Banning an already-banned IP or unbanning a non-banned IP skips audit row ---
        self.audit_rows.clear()
        self.limiter.reset()

        # (a) Unban an IP that is NOT banned -> 200 noop, clear message, 0 audit rows
        r_unban_noop1 = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "unban", "ip": "198.51.100.99"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_unban_noop1.status, 200)
        unban_noop1_json = await r_unban_noop1.json()
        self.assertEqual(unban_noop1_json["status"], "noop")
        self.assertIn("not currently banned", unban_noop1_json.get("message", ""))
        self.assertEqual(len(self.audit_rows), 0)

        # (b) First ban of 198.51.100.99 -> 200 ok, 1 audit row
        r_ban_first = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "198.51.100.99"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_ban_first.status, 200)
        self.assertEqual((await r_ban_first.json())["status"], "ok")
        self.assertEqual(len(self.audit_rows), 1)

        # (c) Second ban of already-banned 198.51.100.99 (even via IPv4-mapped form) -> 200 noop, clear message, still 1 audit row
        r_ban_noop = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "ban", "ip": "::ffff:198.51.100.99"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_ban_noop.status, 200)
        ban_noop_json = await r_ban_noop.json()
        self.assertEqual(ban_noop_json["status"], "noop")
        self.assertIn("already banned", ban_noop_json.get("message", ""))
        self.assertEqual(len(self.audit_rows), 1)

        # (d) First unban of 198.51.100.99 -> 200 ok, 2 audit rows total
        r_unban_first = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "unban", "ip": "198.51.100.99"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_unban_first.status, 200)
        self.assertEqual((await r_unban_first.json())["status"], "ok")
        self.assertEqual(len(self.audit_rows), 2)

        # (e) Second unban of now-unbanned 198.51.100.99 -> 200 noop, clear message, still 2 audit rows
        r_unban_noop2 = await self.client.post(
            "/api/owner/ip_action",
            json={"action": "unban", "ip": "198.51.100.99"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_unban_noop2.status, 200)
        unban_noop2_json = await r_unban_noop2.json()
        self.assertEqual(unban_noop2_json["status"], "noop")
        self.assertIn("not currently banned", unban_noop2_json.get("message", ""))
        self.assertEqual(len(self.audit_rows), 2)



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


