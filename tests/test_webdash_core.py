"""
tests/test_webdash_core.py — Unit tests for Phase A1 Piece 1:
- normalize_ip & right-most / ORIGIN_SHARED_SECRET client IP resolution
- InMemoryRateLimiter & resolve_rate_limit_key fallback behavior
- CSRF verification & security headers middleware
- OAuth2 state & SessionManager (SHA-256 hashing, throttled last_seen, UTC datetime TTL)
- Permission engine (non-lockable-out Owners & Discord Administrators, user_denies, presets)
- GuildConfigCache (TTL, LRU eviction, fail-open on timeout / db_ready=False)
- AuditLogger & compute_diff
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import secrets
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, make_mocked_request

from webdash import get_ready_db, is_webdash_enabled
from webdash.audit import AUDIT_TTL_SECONDS, AuditLogger, compute_diff
from webdash.auth import (
    SessionManager,
    filter_session_guilds,
    generate_oauth_state,
    hash_session_token,
    verify_oauth_state,
)
from webdash.cache import GuildConfigCache, ShortTTLCache
from webdash.permissions import (
    DISCORD_PERM_ADMINISTRATOR,
    DISCORD_PERM_MANAGE_GUILD,
    PERMISSION_CATALOG,
    PERMISSION_SET,
    evaluate_permission,
    expand_entries,
    resolve_user_permissions,
)
from webdash.security import (
    CLIENT_IP_KEY,
    CLIENT_IP_TRUSTED_KEY,
    InMemoryRateLimiter,
    create_security_middleware,
    generate_csrf_token,
    get_client_ip,
    is_plausible_session_token,
    normalize_ip,
    resolve_client_ip_details,
    resolve_rate_limit_key,
    safe_compare,
    verify_csrf_token,
)


class TestIpAndSecurityHelpers(unittest.TestCase):
    def test_normalize_ip(self):
        cases = [
            ("1.2.3.4", "1.2.3.4"),
            ("2001:db8::1", "2001:db8::1"),
            ("::1", "::1"),
            ("::ffff:203.0.113.5", "203.0.113.5"),
            ("fe80::1%eth0", "invalid"),
            ("1.2.3.4 ", "invalid"),
            (" 1.2.3.4", "invalid"),
            ("not-an-ip", "invalid"),
            ("", "invalid"),
            (None, "invalid"),
            ("1.2.3.4\n", "invalid"),
            ("fe80::1%<script>", "invalid"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_ip(raw), expected)

    def test_safe_compare_and_token_verifiers_with_non_ascii_surrogates_and_extremes(self):
        # 1. Non-ASCII str, lone surrogate (from HTTP header surrogateescape), empty, and huge strings
        tricky_values = [
            "é",
            "café-token-🔥",
            "\udcff",
            "prefix-\udc80-\udcff-suffix",
            "",
            "x" * 100_000,
            None,
        ]
        for val in tricky_values:
            with self.subTest(val=repr(val)[:40]):
                self.assertFalse(safe_compare("valid_ascii_secret_123456", val))
                self.assertFalse(safe_compare(val, "valid_ascii_secret_123456"))
                self.assertFalse(verify_csrf_token("valid_ascii_secret_123456", val))
                self.assertFalse(verify_csrf_token(val, "valid_ascii_secret_123456"))
                self.assertFalse(verify_oauth_state("valid_ascii_state_123456", val))
                self.assertFalse(verify_oauth_state(val, "valid_ascii_state_123456"))

                # Origin secret check with non-ASCII / surrogate header value must not raise
                if isinstance(val, str):
                    req = MagicMock()
                    req.headers = {
                        "X-Recluse-Origin-Secret": val,
                        "X-Recluse-Client-IP": "198.51.100.1",
                        "X-Forwarded-For": "203.0.113.99",
                    }
                    req.remote = "10.0.0.1"
                    ip_addr, trusted, _ = resolve_client_ip_details(
                        req, trusted_hops=1, origin_secret="real_ascii_origin_secret"
                    )
                    self.assertEqual(ip_addr, "203.0.113.99")
                    self.assertFalse(trusted)

        # Empty / None must never match even against itself in safe_compare / verifiers
        self.assertFalse(safe_compare("", ""))
        self.assertFalse(safe_compare(None, None))
        self.assertFalse(verify_csrf_token("", ""))
        self.assertFalse(verify_oauth_state("", ""))

        # Matching valid strings (including long and surrogate-escaped strings) work in safe_compare
        long_str = "a" * 100_000
        self.assertTrue(safe_compare(long_str, long_str))
        self.assertTrue(safe_compare("é", "é"))
        self.assertTrue(safe_compare("\udcff", "\udcff"))

    def test_get_client_ip_default_one_hop_rightmost(self):
        # Default TRUSTED_PROXY_HOPS = 1 -> right-most XFF entry
        req = make_mocked_request(
            "GET",
            "/",
            headers={"X-Forwarded-For": "198.51.100.99, 203.0.113.10"},
        )
        self.assertEqual(get_client_ip(req, origin_secret=""), "203.0.113.10")

    def test_get_client_ip_two_hops_from_right_ignores_spoofed_left(self):
        # Spoofed left-most IP must be ignored when TRUSTED_PROXY_HOPS=2
        req = make_mocked_request(
            "GET",
            "/",
            headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.42, 172.64.0.10"},
        )
        self.assertEqual(
            get_client_ip(req, trusted_hops=2, origin_secret=""),
            "203.0.113.42",
        )

    def test_get_client_ip_fewer_hops_than_configured_falls_back_to_rightmost_never_leftmost(self):
        # If trusted_hops=3 but only 2 hops exist, it MUST pick hops[-1] (right-most),
        # NEVER hops[0] (which is spoofable by the client).
        req = make_mocked_request(
            "GET",
            "/",
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.88"},
        )
        self.assertEqual(
            get_client_ip(req, trusted_hops=3, origin_secret=""),
            "203.0.113.88",
        )

    def test_get_client_ip_origin_shared_secret_valid(self):
        secret = "top-secret-worker-key"
        req = make_mocked_request(
            "GET",
            "/",
            headers={
                "X-Recluse-Origin-Secret": secret,
                "X-Recluse-Client-IP": "198.51.100.77",
                "X-Forwarded-For": "9.9.9.9, 10.0.0.1",
            },
        )
        ip_addr, trusted, src = resolve_client_ip_details(
            req, trusted_hops=2, origin_secret=secret
        )
        self.assertEqual(ip_addr, "198.51.100.77")
        self.assertTrue(trusted)
        self.assertEqual(src, "origin_header")

    def test_get_client_ip_direct_origin_spoofing_attempts_rejected(self):
        secret = "top-secret-worker-key"

        # 1. Attacker hits Render directly without X-Recluse-Origin-Secret,
        #    spoofing both X-Recluse-Client-IP and left-most X-Forwarded-For:
        req_missing = make_mocked_request(
            "GET",
            "/",
            headers={
                "X-Recluse-Client-IP": "1.2.3.4",
                "X-Forwarded-For": "5.5.5.5, 203.0.113.200",
            },
        )
        ip_addr, trusted, src = resolve_client_ip_details(
            req_missing, trusted_hops=2, origin_secret=secret
        )
        # X-Recluse-Client-IP is ignored, hops clamped to 1 (right-most Render edge IP),
        # and ip_trusted is False because the Worker secret was bypassed!
        self.assertEqual(ip_addr, "203.0.113.200")
        self.assertFalse(trusted)
        self.assertEqual(src, "xff_right")

        # 2. Attacker sends wrong X-Recluse-Origin-Secret:
        req_wrong = make_mocked_request(
            "GET",
            "/",
            headers={
                "X-Recluse-Origin-Secret": "wrong-secret",
                "X-Recluse-Client-IP": "1.2.3.4",
                "X-Forwarded-For": "5.5.5.5, 203.0.113.201",
            },
        )
        ip_addr2, trusted2, _ = resolve_client_ip_details(
            req_wrong, trusted_hops=2, origin_secret=secret
        )
        self.assertEqual(ip_addr2, "203.0.113.201")
        self.assertFalse(trusted2)

        # 3. When ORIGIN_SHARED_SECRET is unset, X-Recluse-Client-IP is still ignored:
        req_unset = make_mocked_request(
            "GET",
            "/",
            headers={
                "X-Recluse-Origin-Secret": "anything",
                "X-Recluse-Client-IP": "1.2.3.4",
                "X-Forwarded-For": "203.0.113.202",
            },
        )
        self.assertEqual(
            get_client_ip(req_unset, trusted_hops=1, origin_secret=""),
            "203.0.113.202",
        )


class TestRateLimiterAndKeyResolution(unittest.TestCase):
    def test_sliding_window_and_lru_eviction(self):
        limiter = InMemoryRateLimiter(
            max_keys=100,
            limits={"auth": (2, 10.0), "page": (5, 10.0), "unidentified": (50, 10.0)},
        )
        ok1, retry1 = limiter.check("auth", "ip:203.0.113.1", now=100.0)
        ok2, retry2 = limiter.check("auth", "ip:203.0.113.1", now=101.0)
        ok3, retry3 = limiter.check("auth", "ip:203.0.113.1", now=102.0)
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertFalse(ok3)
        self.assertGreaterEqual(retry3, 8)

        # After window expires (t=110.5), allowed again
        ok4, _ = limiter.check("auth", "ip:203.0.113.1", now=110.5)
        self.assertTrue(ok4)

        # Test LRU max_keys eviction in anonymous pool
        for i in range(150):
            limiter.check("page", f"ip:10.0.0.{i}", now=200.0)
        self.assertEqual(len(limiter), 100)

    def test_unvalidated_cookie_never_used_for_rate_limit_key(self):
        # Rotating unauthenticated cookies MUST NOT produce distinct sid: buckets.
        req_with_unvalidated_cookie = make_mocked_request(
            "GET",
            "/login",
            headers={"Cookie": "recluse_sid=attacker_random_cookie_999"},
        )
        # 1. With trusted IP -> keyed by IP, ignoring unvalidated cookie
        scope1, key1 = resolve_rate_limit_key(
            req_with_unvalidated_cookie,
            "auth",
            client_ip="203.0.113.5",
            ip_trusted=True,
            session_doc=None,
        )
        self.assertEqual(scope1, "auth")
        self.assertEqual(key1, "ip:203.0.113.5")

        # 2. With untrusted/invalid IP and unvalidated cookie -> shared unidentified bucket, NEVER sid:
        scope2, key2 = resolve_rate_limit_key(
            req_with_unvalidated_cookie,
            "auth",
            client_ip="203.0.113.5",
            ip_trusted=False,
            session_doc=None,
        )
        self.assertEqual(scope2, "unidentified")
        self.assertEqual(key2, "shared:auth")

        # 3. Validated session_doc with discord_id -> user:<id>
        scope3, key3 = resolve_rate_limit_key(
            req_with_unvalidated_cookie,
            "page",
            client_ip="invalid",
            ip_trusted=False,
            session_doc={"discord_id": 424242, "session_id_hash": "abc123def456"},
        )
        self.assertEqual(scope3, "page")
        self.assertEqual(key3, "user:424242")

        # 4. Validated session_doc without discord_id -> sid:<hash>
        scope4, key4 = resolve_rate_limit_key(
            req_with_unvalidated_cookie,
            "page",
            client_ip="invalid",
            ip_trusted=False,
            session_doc={"session_id_hash": "deadbeef0123456789abcdef"},
        )
        self.assertEqual(scope4, "page")
        self.assertEqual(key4, "sid:deadbeef01234567")

    def test_junk_cookie_or_anon_flood_never_evicts_authenticated_user_bucket(self):
        limiter = InMemoryRateLimiter(
            max_keys=5000,
            limits={"mutation": (5, 60.0), "page": (60, 60.0), "unidentified": (300, 60.0)},
        )
        # Authenticated user hits their mutation limit (5/5)
        for i in range(5):
            ok, _ = limiter.check("mutation", "user:777888999", now=1000.0 + i)
            self.assertTrue(ok)
        ok_sixth, _ = limiter.check("mutation", "user:777888999", now=1005.0)
        self.assertFalse(ok_sixth)
        self.assertTrue(limiter.has_bucket("mutation", "user:777888999"))

        # Flood 5,200 distinct unauthenticated buckets (exceeding max_keys=5000)
        for i in range(5200):
            limiter.check("page", f"ip:198.51.{i // 256}.{i % 256}", now=1010.0)

        # Authenticated user's bucket MUST still exist and remain rate-limited!
        self.assertTrue(limiter.has_bucket("mutation", "user:777888999"))
        ok_after_flood, _ = limiter.check("mutation", "user:777888999", now=1015.0)
        self.assertFalse(ok_after_flood)


class TestPermissionEngine(unittest.TestCase):
    def test_catalog_contains_amended_permissions(self):
        required_nodes = {
            "autoresponder.view",
            "autoresponder.edit",
            "birthdays.view",
            "birthdays.edit",
            "reminders.view",
            "reminders.edit",
            "settings.edit",
            "settings.export",
            "settings.import",
            "welcome.view",
            "reactionroles.view",
            "logging.view",
            "audit.view",
            "audit.restore",
            "permissions.manage",
        }
        self.assertTrue(required_nodes.issubset(PERMISSION_SET))

    def test_owners_and_discord_administrators_cannot_be_locked_out(self):
        # Even if user_denies and role_mappings explicitly deny "*",
        # Bot Owner, Server Owner, and Discord Administrator MUST always be allowed.
        hostile_acl = {
            "user_denies": {
                "100": ["*"],
                "200": ["*"],
                "300": ["*", "permissions.manage", "antinuke.edit"],
            },
            "role_mappings": {
                "999": {"allow": [], "deny": ["*"]},
            },
        }

        # 1. Bot Owner (even if not in guild)
        for perm in PERMISSION_CATALOG:
            self.assertTrue(
                evaluate_permission(
                    perm,
                    user_id=100,
                    guild_owner_id=200,
                    bot_owner_id=100,
                    is_member=False,
                    acl_config=hostile_acl,
                )
            )

        # 2. Server Owner
        for perm in PERMISSION_CATALOG:
            self.assertTrue(
                evaluate_permission(
                    perm,
                    user_id=200,
                    guild_owner_id=200,
                    bot_owner_id=100,
                    is_member=True,
                    member_role_ids=[999],
                    acl_config=hostile_acl,
                )
            )

        # 3. Discord Administrator (0x8)
        for perm in PERMISSION_CATALOG:
            self.assertTrue(
                evaluate_permission(
                    perm,
                    user_id=300,
                    guild_owner_id=200,
                    bot_owner_id=100,
                    is_member=True,
                    discord_permissions=DISCORD_PERM_ADMINISTRATOR,
                    member_role_ids=[999],
                    acl_config=hostile_acl,
                )
            )

    def test_user_denies_and_role_precedence_for_non_admins(self):
        acl = {
            "user_denies": {
                "400": ["automod.edit", "lockdown.execute"],
            },
            "user_overrides": {
                "400": ["automod.*"],
                "401": ["tickets.edit"],
            },
            "role_mappings": {
                "10": {"allow": ["ModManager"], "deny": ["giveaways.reroll"]},
                "20": {"allow": ["giveaways.reroll"], "deny": []},
            },
        }

        # User 400 has user_overrides for automod.*, but user_denies blocks automod.edit
        self.assertTrue(
            evaluate_permission(
                "automod.view",
                user_id=400,
                is_member=True,
                acl_config=acl,
            )
        )
        self.assertFalse(
            evaluate_permission(
                "automod.edit",
                user_id=400,
                is_member=True,
                acl_config=acl,
            )
        )

        # User 402 has roles 10 and 20: role 10 grants ModManager (which includes giveaways.*)
        # and denies giveaways.reroll; role 20 allows giveaways.reroll. Role deny wins!
        self.assertTrue(
            evaluate_permission(
                "giveaways.edit",
                user_id=402,
                is_member=True,
                member_role_ids=[10, 20],
                acl_config=acl,
            )
        )
        self.assertFalse(
            evaluate_permission(
                "giveaways.reroll",
                user_id=402,
                is_member=True,
                member_role_ids=[10, 20],
                acl_config=acl,
            )
        )

        # Manage Server (0x20) grants standard nodes by default, but NOT antinuke.edit
        # or permissions.manage, and CAN be restricted by user_denies:
        self.assertTrue(
            evaluate_permission(
                "welcome.edit",
                user_id=500,
                is_member=True,
                discord_permissions=DISCORD_PERM_MANAGE_GUILD,
                acl_config=acl,
            )
        )
        self.assertFalse(
            evaluate_permission(
                "permissions.manage",
                user_id=500,
                is_member=True,
                discord_permissions=DISCORD_PERM_MANAGE_GUILD,
                acl_config=acl,
            )
        )
        self.assertFalse(
            evaluate_permission(
                "lockdown.execute",
                user_id=400,
                is_member=True,
                discord_permissions=DISCORD_PERM_MANAGE_GUILD,
                acl_config=acl,
            )
        )

    def test_expand_entries_bare_string_and_non_str_items(self):
        # Bare string must be treated as [entries], NOT iterated char-by-char
        self.assertEqual(expand_entries("automod.edit"), ["automod.edit"])
        self.assertEqual(expand_entries("automod.*"), ["automod.*"])
        self.assertEqual(expand_entries("Viewer"), ["*.view"])

        # Non-str items inside an iterable must be skipped safely
        self.assertEqual(
            expand_entries([123, None, {"bad": "item"}, "automod.edit", ""]),
            ["automod.edit"],
        )

        # Malformed ACL in MongoDB where user_denies["400"] is a bare string "automod.edit"
        malformed_acl = {
            "user_denies": {
                "400": "automod.edit",
            },
            "user_overrides": {
                "400": "automod.*",
            },
        }
        self.assertFalse(
            evaluate_permission(
                "automod.edit",
                user_id=400,
                is_member=True,
                discord_permissions=DISCORD_PERM_MANAGE_GUILD,
                acl_config=malformed_acl,
            )
        )
        self.assertTrue(
            evaluate_permission(
                "automod.view",
                user_id=400,
                is_member=True,
                acl_config=malformed_acl,
            )
        )


class TestSessionsCacheAndAudit(unittest.IsolatedAsyncioTestCase):
    async def test_session_manager_hashing_and_throttled_last_seen(self):
        bot = MagicMock()
        bot.db_ready = True
        stored_docs: dict[str, dict] = {}

        async def fake_insert_one(doc):
            stored_docs[doc["session_id_hash"]] = dict(doc)
            return MagicMock(inserted_id="id1")

        async def fake_find_one(query):
            h = query.get("session_id_hash")
            return dict(stored_docs[h]) if h in stored_docs else None

        update_mock = AsyncMock()

        bot.db.webdash_sessions.insert_one = AsyncMock(side_effect=fake_insert_one)
        bot.db.webdash_sessions.find_one = AsyncMock(side_effect=fake_find_one)
        bot.db.webdash_sessions.update_one = update_mock

        mgr = SessionManager(bot, ttl_seconds=3600, last_seen_throttle_seconds=300)
        t0 = datetime.datetime(2026, 10, 25, 10, 0, 0, tzinfo=datetime.timezone.utc)

        created = await mgr.create_session(
            discord_id=123456,
            username="Ayush",
            guilds=[{"id": "999", "name": "Recluse HQ", "permissions": 8, "extra": "dropped"}],
            client_ip="203.0.113.5",
            user_agent="TestBrowser/1.0",
            now=t0,
        )
        self.assertIsNotNone(created)
        raw_token, doc = created
        expected_hash = hash_session_token(raw_token)
        self.assertEqual(doc["session_id_hash"], expected_hash)
        self.assertNotIn(raw_token, stored_docs)
        self.assertEqual(doc["expires_at"], t0 + datetime.timedelta(seconds=3600))
        self.assertNotIn("extra", doc["guilds"][0])

        # Lookup 60s later -> should NOT trigger update_one (throttled at 300s)
        t1 = t0 + datetime.timedelta(seconds=60)
        fetched1 = await mgr.get_session(raw_token, now=t1)
        self.assertIsNotNone(fetched1)
        update_mock.assert_not_awaited()

        # Lookup 305s later -> SHOULD trigger update_one
        t2 = t0 + datetime.timedelta(seconds=305)
        fetched2 = await mgr.get_session(raw_token, now=t2)
        self.assertIsNotNone(fetched2)
        update_mock.assert_awaited_once()

        # Lookup after expiry -> returns None
        t_expired = t0 + datetime.timedelta(seconds=3601)
        self.assertIsNone(await mgr.get_session(raw_token, now=t_expired))

        # When db_ready is False -> returns None safely
        bot.db_ready = False
        self.assertIsNone(await mgr.get_session(raw_token, now=t1))

    async def test_guild_config_cache_ttl_invalidation_and_fail_open(self):
        bot = MagicMock()
        bot.db_ready = True
        ai_cog = MagicMock()
        bot.get_cog.return_value = ai_cog

        bot.db.guild_settings.find_one = AsyncMock(
            return_value={"_id": "mongo_id", "guild_id": 555, "automod_enabled": True}
        )

        cache = GuildConfigCache(bot, max_size=2, ttl_seconds=60.0, db_timeout=0.5)
        cfg1 = await cache.get(555, now=10.0)
        self.assertEqual(cfg1, {"guild_id": 555, "automod_enabled": True})
        self.assertEqual(bot.db.guild_settings.find_one.await_count, 1)

        # Second call within TTL uses cache without hitting MongoDB
        cfg2 = await cache.get(555, now=30.0)
        self.assertEqual(cfg2["automod_enabled"], True)
        self.assertEqual(bot.db.guild_settings.find_one.await_count, 1)

        # If DB times out after TTL expiry, cache fails OPEN (returns stale snapshot or {})
        async def slow_find_one(_q):
            await asyncio.sleep(5.0)
            return {}

        bot.db.guild_settings.find_one = AsyncMock(side_effect=slow_find_one)
        cfg_stale = await cache.get(555, now=100.0)
        self.assertEqual(cfg_stale, {"guild_id": 555, "automod_enabled": True})

        # Uncached guild when DB errors -> fails open to {}
        cfg_empty = await cache.get(777, now=100.0)
        self.assertEqual(cfg_empty, {})

        # Invalidate also notifies AI cog
        cache.invalidate(555)
        ai_cog._invalidate_guild_cfg.assert_called_once_with(555)

    async def test_audit_logger_and_compute_diff(self):
        before = {"welcome_enabled": False, "welcome_channel": 111, "unchanged": "ok"}
        after = {"welcome_enabled": True, "welcome_channel": 222, "unchanged": "ok"}
        diff = compute_diff(before, after)
        self.assertEqual(
            diff,
            {
                "welcome_channel": {"old": 111, "new": 222},
                "welcome_enabled": {"old": False, "new": True},
            },
        )

        bot = MagicMock()
        bot.db_ready = True
        bot.db.dashboard_audit.insert_one = AsyncMock()
        bot.db.dashboard_audit.create_index = AsyncMock()

        audit = AuditLogger(bot)
        self.assertTrue(await audit.ensure_indexes())
        entry = await audit.record(
            guild_id=999,
            actor_id=123,
            actor_name="AdminUser",
            module="welcome",
            action="update",
            changes=diff,
            before_snapshot=before,
            client_ip="203.0.113.1",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["before_snapshot"], {"welcome_channel": 111, "welcome_enabled": False})
        self.assertEqual(AUDIT_TTL_SECONDS, 2592000)


class TestSecurityMiddlewareIntegration(AioHTTPTestCase):
    async def get_application(self):
        self.valid_sid = "V" * 43  # Matches ^[A-Za-z0-9_-]{43}$
        self.limiter = InMemoryRateLimiter(
            limits={
                "auth": (3, 60.0),
                "mutation": (5, 60.0),
                "page": (10, 60.0),
                "pre_session": (20, 60.0),
                "unidentified": (50, 60.0),
            }
        )
        self.session_loader_calls = 0
        self.active_session = {
            "discord_id": 123456,
            "username": "Tester",
            "csrf_token": "valid_csrf_token_abc",
        }

        async def fake_session_loader(request: web.Request):
            self.session_loader_calls += 1
            if request.cookies.get("recluse_sid") == self.valid_sid:
                return self.active_session
            return None

        middleware = create_security_middleware(
            self.limiter,
            session_loader=fake_session_loader,
            trusted_hops=1,
            origin_secret="worker_shared_secret_ascii",
        )
        app = web.Application(middlewares=[middleware])

        async def health(req: web.Request):
            return web.json_response({"ok": True, "trusted": req[CLIENT_IP_TRUSTED_KEY]})

        async def home(req: web.Request):
            return web.json_response({"ip": req[CLIENT_IP_KEY], "trusted": req[CLIENT_IP_TRUSTED_KEY]})

        async def login(_req: web.Request):
            return web.json_response({"login": True})

        async def page_route(_req: web.Request):
            return web.json_response({"page": True})

        async def mutate(_req: web.Request):
            return web.json_response({"saved": True})

        app.router.add_get("/health", health)
        app.router.add_get("/", home)
        app.router.add_get("/login", login)
        app.router.add_get("/manage/123", page_route)
        app.router.add_post("/api/test", mutate)
        return app

    async def test_is_plausible_session_token_regex(self):
        self.assertTrue(is_plausible_session_token(secrets.token_urlsafe(32)))
        self.assertTrue(is_plausible_session_token("A" * 43))
        self.assertFalse(is_plausible_session_token("short_token"))
        self.assertFalse(is_plausible_session_token("A" * 42))
        self.assertFalse(is_plausible_session_token("A" * 44))
        self.assertFalse(is_plausible_session_token("é" * 43))
        self.assertFalse(is_plausible_session_token("A" * 42 + "!"))
        self.assertFalse(is_plausible_session_token(""))
        self.assertFalse(is_plausible_session_token(None))

    async def test_fast_path_health_and_unauthenticated_home(self):
        resp_h = await self.client.get(
            "/health",
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(resp_h.status, 200)
        self.assertEqual(self.session_loader_calls, 0)
        self.assertEqual(resp_h.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp_h.headers.get("X-Content-Type-Options"), "nosniff")

        resp_home = await self.client.get(
            "/",
            headers={
                "X-Recluse-Origin-Secret": "worker_shared_secret_ascii",
                "X-Recluse-Client-IP": "203.0.113.55",
                "X-Forwarded-For": "1.1.1.1, 203.0.113.55",
            },
        )
        self.assertEqual(resp_home.status, 200)
        self.assertEqual(self.session_loader_calls, 0)
        data = await resp_home.json()
        self.assertEqual(data["ip"], "203.0.113.55")
        self.assertTrue(data["trusted"])

    async def test_non_ascii_headers_never_500_on_aiohttp_client(self):
        # 1. Non-ASCII X-CSRF-Token -> 403 (never 500)
        r_csrf = await self.client.post(
            "/api/test",
            json={"a": 1},
            headers={"X-CSRF-Token": "é"},
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(r_csrf.status, 403)

        # 2. Non-ASCII X-Recluse-Origin-Secret on /health and / -> 200 without trust (never 500)
        r_health = await self.client.get(
            "/health",
            headers={
                "X-Recluse-Origin-Secret": "é",
                "X-Recluse-Client-IP": "1.2.3.4",
            },
        )
        self.assertEqual(r_health.status, 200)
        h_data = await r_health.json()
        self.assertFalse(h_data["trusted"])

        r_home = await self.client.get(
            "/",
            headers={
                "X-Recluse-Origin-Secret": "é",
                "X-Recluse-Client-IP": "1.2.3.4",
                "X-Forwarded-For": "203.0.113.77",
            },
        )
        self.assertEqual(r_home.status, 200)
        home_data = await r_home.json()
        self.assertEqual(home_data["ip"], "203.0.113.77")
        self.assertFalse(home_data["trusted"])

        # 3. Raw non-ASCII bytes (0xff / UTF-8 'é') sent over raw socket to the running aiohttp test server
        reader, writer = await asyncio.open_connection(self.server.host, self.server.port)
        raw_req = (
            f"POST /api/test HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Cookie: recluse_sid={self.valid_sid}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 7\r\n"
            f"Connection: close\r\n"
        ).encode("ascii") + (
            b"X-CSRF-Token: \xc3\xa9\xff\r\n"
            b"X-Recluse-Origin-Secret: \xc3\xa9\xff\r\n\r\n"
            b'{"a":1}'
        )
        writer.write(raw_req)
        await writer.drain()
        status_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b"403", status_line)
        self.assertNotIn(b"500", status_line)

    async def test_rotating_junk_cookies_still_hit_429_and_bound_session_loader(self):
        # 1. 150 non-plausible junk cookies on /manage/123 -> 0 session_loader calls!
        self.session_loader_calls = 0
        statuses_short = []
        for i in range(150):
            resp = await self.client.get(
                "/manage/123",
                headers={
                    "X-Recluse-Origin-Secret": "worker_shared_secret_ascii",
                    "X-Recluse-Client-IP": "198.51.100.88",
                },
                cookies={"recluse_sid": f"junk_cookie_{i}"},
            )
            statuses_short.append(resp.status)
        self.assertEqual(self.session_loader_calls, 0)
        self.assertEqual(statuses_short[:10], [200] * 10)
        self.assertEqual(statuses_short[10:], [429] * 140)

        # 2. 150 plausible 43-char random cookies (secrets.token_urlsafe(32)) from IP 198.51.100.89:
        #    pre_session limit is 20 -> session_loader is called at most 20 times (never 150)!
        self.session_loader_calls = 0
        statuses_plausible = []
        for _ in range(150):
            resp = await self.client.get(
                "/manage/123",
                headers={
                    "X-Recluse-Origin-Secret": "worker_shared_secret_ascii",
                    "X-Recluse-Client-IP": "198.51.100.89",
                },
                cookies={"recluse_sid": secrets.token_urlsafe(32)},
            )
            statuses_plausible.append(resp.status)
        self.assertEqual(self.session_loader_calls, 20)
        self.assertEqual(statuses_plausible[:10], [200] * 10)
        self.assertEqual(statuses_plausible[10:], [429] * 140)

        # 3. Legitimate user from another IP (198.51.100.90) with valid_sid still works!
        resp_legit = await self.client.get(
            "/manage/123",
            headers={
                "X-Recluse-Origin-Secret": "worker_shared_secret_ascii",
                "X-Recluse-Client-IP": "198.51.100.90",
            },
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(resp_legit.status, 200)

    async def test_csrf_enforcement_on_post(self):
        # 1. No session -> 401
        r1 = await self.client.post("/api/test", json={"a": 1})
        self.assertEqual(r1.status, 401)

        # 2. Valid session but missing CSRF token -> 403
        r2 = await self.client.post(
            "/api/test",
            json={"a": 1},
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(r2.status, 403)

        # 3. Valid session + wrong CSRF token -> 403
        r3 = await self.client.post(
            "/api/test",
            json={"a": 1},
            headers={"X-CSRF-Token": "wrong_token"},
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(r3.status, 403)

        # 4. Valid session + matching X-CSRF-Token header -> 200
        r4 = await self.client.post(
            "/api/test",
            json={"a": 1},
            headers={"X-CSRF-Token": "valid_csrf_token_abc"},
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(r4.status, 200)

        # 5. Valid session + matching JSON body csrf_token -> 200
        r5 = await self.client.post(
            "/api/test",
            json={"a": 1, "csrf_token": "valid_csrf_token_abc"},
            cookies={"recluse_sid": self.valid_sid},
        )
        self.assertEqual(r5.status, 200)

    async def test_security_headers_applied_on_http_exceptions_404_and_405(self):
        # 1. GET /nope -> 404 HTTPNotFound raised by aiohttp router must still carry security headers
        r_404 = await self.client.get("/nope")
        self.assertEqual(r_404.status, 404)
        self.assertEqual(r_404.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r_404.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            r_404.headers.get("Referrer-Policy"),
            "strict-origin-when-cross-origin",
        )
        self.assertIn("default-src 'self'", r_404.headers.get("Content-Security-Policy", ""))

        # 2. GET /api/test (registered only as POST) -> 405 HTTPMethodNotAllowed must carry security headers
        r_405 = await self.client.get("/api/test")
        self.assertEqual(r_405.status, 405)
        self.assertEqual(r_405.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r_405.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            r_405.headers.get("Referrer-Policy"),
            "strict-origin-when-cross-origin",
        )
        self.assertIn("default-src 'self'", r_405.headers.get("Content-Security-Policy", ""))

    async def test_one_time_warnings_for_missing_origin_secret_and_db_not_ready(self):
        import webdash
        import webdash.security as sec_mod

        # 1. Missing ORIGIN_SHARED_SECRET logs warning once
        sec_mod._MISSING_ORIGIN_SECRET_WARNED = False
        with self.assertLogs("recluse.webdash.security", level="WARNING") as cm_sec:
            create_security_middleware(self.limiter, origin_secret="")
            create_security_middleware(self.limiter, origin_secret="")
        self.assertEqual(len(cm_sec.output), 1)
        self.assertIn("ORIGIN_SHARED_SECRET is unset", cm_sec.output[0])

        # 2. REQUIRE_ORIGIN_SHARED_SECRET=True with empty ORIGIN_SHARED_SECRET logs one ERROR at startup
        sec_mod._REQUIRE_WITHOUT_ORIGIN_SECRET_ERRORED = False
        with self.assertLogs("recluse.webdash.security", level="ERROR") as cm_err:
            create_security_middleware(self.limiter, origin_secret="", require_origin_secret=True)
            create_security_middleware(self.limiter, origin_secret="", require_origin_secret=True)
        self.assertEqual(len(cm_err.output), 1)
        self.assertIn(
            "REQUIRE_ORIGIN_SHARED_SECRET is enabled while ORIGIN_SHARED_SECRET is empty",
            cm_err.output[0],
        )

        # 3. bot.db_ready is False logs "webdash: bot.db_ready is False, sessions disabled" once
        webdash._DB_NOT_READY_WARNED = False
        fake_bot = MagicMock()
        fake_bot.db_ready = False
        fake_bot.db = MagicMock()
        with self.assertLogs("recluse.webdash", level="WARNING") as cm_db:
            self.assertIsNone(get_ready_db(fake_bot))
            self.assertIsNone(get_ready_db(fake_bot))
        self.assertEqual(len(cm_db.output), 1)
        self.assertIn("webdash: bot.db_ready is False, sessions disabled", cm_db.output[0])


if __name__ == "__main__":
    unittest.main()



