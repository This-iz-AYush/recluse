"""
tests/test_webdash_real_bot.py — Regression tests for:
- FIX 1: Real `discord.ext.commands.Bot` container types (`mappingproxy` for `bot.cogs` /
  `bot.extensions`, `SequenceProxy` for `bot.guilds`) across `/modules/{guild_id}`,
  `/owner_panel`, `POST /api/owner/action`, and `POST /api/modules/{guild_id}`.
- FIX 2: Throttled fire-and-forget `db.visit_logs` writer (`VisitTracker`), 30-day TTL
  index on `last_visit_at`, trusted-IP-only recording, fast-path exemptions, and
  `"Your current IP: <ip>"` + `"Your IP"` row marker on `/owner_panel`.
- FIX 3: Removable legacy `disabled_cmds` via `reenable_cmds` on `POST /api/modules/{guild_id}`
  and per-entry remove controls on `/modules/{guild_id}`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import copy
import datetime
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import AioHTTPTestCase
import discord
from discord.ext import commands

from webdash.app import create_webdash_app
from webdash.auth import SESSION_COOKIE_NAME, SessionManager
from webdash.cache import (
    VISIT_LOGS_TTL_SECONDS,
    GuildConfigCache,
    IpBanCache,
    ShortTTLCache,
    VisitTracker,
)
from webdash.permissions import DISCORD_PERM_ADMINISTRATOR
from webdash.security import InMemoryRateLimiter


class _FakeChannel:
    def __init__(self, cid: int, name: str) -> None:
        self.id = cid
        self.name = name


class _FakeRole:
    def __init__(self, rid: int, name: str) -> None:
        self.id = rid
        self.name = name


class _FakePermissions:
    def __init__(self, value: int, *, administrator: bool = False, manage_guild: bool = False) -> None:
        self.value = value
        self.administrator = administrator
        self.manage_guild = manage_guild


class _FakeMember:
    def __init__(
        self,
        uid: int,
        *,
        administrator: bool = False,
        manage_guild: bool = False,
        role_ids: list[int] | None = None,
    ) -> None:
        self.id = uid
        bits = DISCORD_PERM_ADMINISTRATOR if administrator else 0
        self.guild_permissions = _FakePermissions(
            bits,
            administrator=administrator,
            manage_guild=manage_guild,
        )
        self.roles = [_FakeRole(rid, f"role-{rid}") for rid in (role_ids or [])]


class _FakeGuild:
    def __init__(self, gid: int, name: str, owner_id: int) -> None:
        self.id = gid
        self.name = name
        self.owner_id = owner_id
        self.icon = None
        self.member_count = 25
        self._members: dict[int, _FakeMember] = {}
        self.text_channels = [
            _FakeChannel(1001, "general"),
            _FakeChannel(1002, "mod-log"),
        ]
        self.channels = list(self.text_channels)
        self.roles = [
            _FakeRole(gid, "@everyone"),
            _FakeRole(2001, "Moderator"),
            _FakeRole(2002, "Muted"),
        ]

    def get_member(self, uid: int) -> _FakeMember | None:
        return self._members.get(int(uid))


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = list(docs)

    def sort(self, field: str, direction: int = -1) -> "_FakeCursor":
        self._docs.sort(key=lambda d: d.get(field, 0), reverse=(direction < 0))
        return self

    def limit(self, count: int) -> "_FakeCursor":
        self._docs = self._docs[:count]
        return self

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return copy.deepcopy(next(self._iter))
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _register_real_cog_extensions(temp_dir_path: str) -> tuple[str, str]:
    """
    Write two real `.py` extension modules into `temp_dir_path` and append `temp_dir_path`
    to `cogs.__path__` so `await bot.load_extension("cogs.real_mod_test")` and
    `await bot.reload_extension("cogs.real_mod_test")` use Python's native file loader
    without modifying any file inside `/cogs/`.
    """
    import pathlib
    import textwrap
    import cogs

    if temp_dir_path not in cogs.__path__:
        cogs.__path__.append(temp_dir_path)

    base = pathlib.Path(temp_dir_path)
    (base / "real_mod_test.py").write_text(
        textwrap.dedent(
            """
            import discord
            from discord import app_commands
            from discord.ext import commands

            LOAD_COUNT = 0

            class Moderation(commands.Cog):
                def __init__(self, bot: commands.Bot) -> None:
                    self.bot = bot

                lookup = app_commands.Group(
                    name="lookup",
                    description="Information lookup commands",
                )

                @lookup.command(name="wiki", description="Fetch a Wikipedia article summary")
                async def lookup_wiki(self, interaction: discord.Interaction, query: str) -> None:
                    pass

                @lookup.command(name="ipinfo", description="Look up geolocation info for an IP")
                async def lookup_ipinfo(self, interaction: discord.Interaction, ip: str) -> None:
                    pass

                @app_commands.command(name="slash_only", description="Pure slash command")
                async def slash_only_cmd(self, interaction: discord.Interaction) -> None:
                    pass

                @commands.hybrid_command(name="warn", description="Warn a server member")
                async def warn_cmd(self, ctx: commands.Context) -> None:
                    pass

                @commands.hybrid_group(name="filter", description="Manage word filter")
                async def filter_grp(self, ctx: commands.Context) -> None:
                    pass

                @filter_grp.command(name="add", description="Add a filtered word")
                async def filter_add(self, ctx: commands.Context, word: str) -> None:
                    pass

                @filter_grp.command(name="remove", description="Remove a filtered word")
                async def filter_remove(self, ctx: commands.Context, word: str) -> None:
                    pass

                @commands.command(name="purge", help="Bulk delete messages")
                async def purge_cmd(self, ctx: commands.Context, count: int = 10) -> None:
                    pass

            async def setup(bot: commands.Bot) -> None:
                global LOAD_COUNT
                LOAD_COUNT += 1
                await bot.add_cog(Moderation(bot))
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    (base / "real_core_test.py").write_text(
        textwrap.dedent(
            """
            from discord.ext import commands

            class Core(commands.Cog):
                def __init__(self, bot: commands.Bot) -> None:
                    self.bot = bot

                @commands.command(name="ping", help="Check latency")
                async def ping_cmd(self, ctx: commands.Context) -> None:
                    pass

            async def setup(bot: commands.Bot) -> None:
                await bot.add_cog(Core(bot))
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    return "cogs.real_mod_test", "cogs.real_core_test"


class TestRealDiscordBotAndTelemetryFixes(AioHTTPTestCase):
    async def get_application(self):
        import tempfile

        self.owner_user_id = 900001
        self.admin_user_id = 900002
        self.mod_only_user_id = 900003
        self.guild_id = 123456789012345678

        self._ext_temp_dir = tempfile.TemporaryDirectory()
        self.mod_ext_name, self.core_ext_name = _register_real_cog_extensions(
            self._ext_temp_dir.name
        )

        intents = discord.Intents.default()
        self.real_bot = commands.Bot(command_prefix="!", intents=intents, owner_id=self.owner_user_id)
        await self.real_bot.load_extension(self.mod_ext_name)
        await self.real_bot.load_extension(self.core_ext_name)

        # Attach fake DB & guild lookup onto the real commands.Bot instance
        self.fake_guild = _FakeGuild(self.guild_id, "RealBot Test Server", owner_id=self.admin_user_id)
        self.fake_guild._members[self.admin_user_id] = _FakeMember(
            self.admin_user_id, administrator=True
        )
        self.fake_guild._members[self.mod_only_user_id] = _FakeMember(
            self.mod_only_user_id, administrator=False, role_ids=[555]
        )
        self.real_bot.get_guild = lambda gid: (  # type: ignore[method-assign]
            self.fake_guild if int(gid) == self.guild_id else None
        )

        # Setup in-memory MongoDB collections on real_bot.db
        self.session_store: dict[str, dict] = {}
        self.guild_settings_doc: dict = {
            "guild_id": self.guild_id,
            "dashboard_rev": 0,
            "disabled_cogs": [],
            "disabled_cmds": ["legacy_ban", "legacy_kick", "old_mute"],
            "command_settings": {
                "ping": {"allowed_channels": ["1001"]},
            },
            "dashboard_permissions": {
                "role_mappings": {
                    "555": {
                        "allow": ["modules.view", "modules.edit"],
                        "deny": [],
                    }
                }
            },
        }
        self.visit_docs: dict[str, dict] = {}
        self.ip_ban_docs: list[dict] = []
        self.audit_rows: list[dict] = []

        fake_db = MagicMock()
        self.fake_db = fake_db

        # webdash_sessions
        async def _sess_insert(doc: dict):
            self.session_store[doc["session_id_hash"]] = copy.deepcopy(doc)

        async def _sess_find_one(flt: dict):
            h = flt.get("session_id_hash")
            doc = self.session_store.get(h)
            return copy.deepcopy(doc) if doc else None

        async def _sess_update_one(flt: dict, update: dict, **_kw):
            h = flt.get("session_id_hash")
            doc = self.session_store.get(h)
            if doc and "$set" in update:
                doc.update(copy.deepcopy(update["$set"]))

        async def _sess_find_one_and_update(flt: dict, update: dict, **_kw):
            h = flt.get("session_id_hash")
            doc = self.session_store.get(h)
            if not doc:
                return None
            if "$set" in update:
                doc.update(copy.deepcopy(update["$set"]))
            return copy.deepcopy(doc)

        async def _sess_delete_one(flt: dict):
            self.session_store.pop(flt.get("session_id_hash"), None)

        fake_db.webdash_sessions.insert_one = AsyncMock(side_effect=_sess_insert)
        fake_db.webdash_sessions.find_one = AsyncMock(side_effect=_sess_find_one)
        fake_db.webdash_sessions.update_one = AsyncMock(side_effect=_sess_update_one)
        fake_db.webdash_sessions.find_one_and_update = AsyncMock(side_effect=_sess_find_one_and_update)
        fake_db.webdash_sessions.delete_one = AsyncMock(side_effect=_sess_delete_one)
        fake_db.webdash_sessions.create_index = AsyncMock()

        # guild_settings
        async def _gs_find_one(flt: dict):
            if int(flt.get("guild_id", 0)) == self.guild_id:
                return copy.deepcopy(self.guild_settings_doc)
            return None

        async def _gs_update_one(flt: dict, update: dict, upsert: bool = False):
            res = MagicMock()
            if int(flt.get("guild_id", 0)) != self.guild_id:
                res.matched_count = 0
                res.upserted_id = None
                return res
            current_rev = int(self.guild_settings_doc.get("dashboard_rev", 0))
            if "$or" in flt:
                expected_rev = 0
            else:
                expected_rev = int(flt.get("dashboard_rev", -1))
            if current_rev != expected_rev:
                res.matched_count = 0
                res.upserted_id = None
                return res
            if "$set" in update:
                self.guild_settings_doc.update(copy.deepcopy(update["$set"]))
            if "$inc" in update:
                for k, v in update["$inc"].items():
                    self.guild_settings_doc[k] = int(self.guild_settings_doc.get(k, 0)) + int(v)
            res.matched_count = 1
            res.upserted_id = None
            return res

        fake_db.guild_settings.find_one = AsyncMock(side_effect=_gs_find_one)
        fake_db.guild_settings.update_one = AsyncMock(side_effect=_gs_update_one)

        # visit_logs
        async def _visit_update_one(flt: dict, update: dict, upsert: bool = False):
            ip_val = str(flt.get("ip", ""))
            doc = self.visit_docs.get(ip_val, {"ip": ip_val, "hits": 0})
            if "$set" in update:
                doc.update(copy.deepcopy(update["$set"]))
            if "$inc" in update:
                for k, v in update["$inc"].items():
                    doc[k] = int(doc.get(k, 0)) + int(v)
            self.visit_docs[ip_val] = doc
            res = MagicMock()
            res.matched_count = 1
            return res

        fake_db.visit_logs.update_one = AsyncMock(side_effect=_visit_update_one)
        fake_db.visit_logs.create_index = AsyncMock()
        fake_db.visit_logs.find = MagicMock(
            side_effect=lambda *a, **kw: _FakeCursor(list(self.visit_docs.values()))
        )

        # ip_bans
        fake_db.ip_bans.find = MagicMock(
            side_effect=lambda *a, **kw: _FakeCursor(list(self.ip_ban_docs))
        )

        # dashboard_audit
        async def _audit_insert(doc: dict):
            self.audit_rows.append(copy.deepcopy(doc))

        fake_db.dashboard_audit.insert_one = AsyncMock(side_effect=_audit_insert)
        fake_db.dashboard_audit.create_index = AsyncMock()
        fake_db.global_blacklist.find_one = AsyncMock(return_value=None)

        self.real_bot.db = fake_db  # type: ignore[attr-defined]
        self.real_bot.db_ready = True  # type: ignore[attr-defined]

        self.limiter = InMemoryRateLimiter()
        self.session_mgr = SessionManager(self.real_bot)
        self.cfg_cache = GuildConfigCache(self.real_bot)
        self.ip_ban_cache = IpBanCache(self.real_bot)
        self.visit_tracker = VisitTracker(self.real_bot, throttle_seconds=300.0, db_timeout=2.0)

        return create_webdash_app(
            self.real_bot,
            rate_limiter=self.limiter,
            session_manager=self.session_mgr,
            guild_config_cache=self.cfg_cache,
            ip_ban_cache=self.ip_ban_cache,
            visit_tracker=self.visit_tracker,
            member_cache=ShortTTLCache(max_size=100, ttl_seconds=60.0),
            owner_id_cache=ShortTTLCache(max_size=10, ttl_seconds=600.0),
        )

    async def tearDownAsync(self) -> None:
        import cogs

        await self.visit_tracker.flush()
        await self.real_bot.close()
        sys.modules.pop(self.mod_ext_name, None)
        sys.modules.pop(self.core_ext_name, None)
        raw_path_list = getattr(cogs.__path__, "_path", None)
        if isinstance(raw_path_list, list) and self._ext_temp_dir.name in raw_path_list:
            raw_path_list.remove(self._ext_temp_dir.name)
        self._ext_temp_dir.cleanup()
        await super().tearDownAsync()

    async def _make_session(self, user_id: int, username: str) -> tuple[str, dict]:
        created = await self.session_mgr.create_session(
            discord_id=user_id,
            username=username,
            avatar=None,
            guilds=[{"id": str(self.guild_id), "name": "RealBot Test Server", "owner": True, "permissions": "8"}],
            client_ip="127.0.0.1",
            user_agent="RealBotTest/1.0",
        )
        assert created is not None
        return created

    async def test_fix1_real_commands_bot_containers_modules_owner_reload_and_save(self):
        """
        FIX 1 Regression Test with a REAL `discord.ext.commands.Bot`:
        - Confirms `bot.cogs` and `bot.extensions` are `mappingproxy` (Mapping, NOT dict)
          and `bot.guilds` is `SequenceProxy` (Sequence, NOT list/tuple/set).
        - Confirms `/modules/{guild_id}` lists the non-protected `Moderation` cog with its
          hybrid command (`warn`), hybrid group (`filter`) + subcommands (`add`, `remove`),
          and plain prefix command (`purge`), while hiding enabled protected `Core`.
        - Confirms `/owner_panel` shows `Cogs: 2` and one reload button per loaded extension.
        - Confirms `POST /api/owner/action` (`reload_cog`) reloads the real extension `cogs.real_mod_test`.
        - Confirms `POST /api/modules/{guild_id}` succeeds across the `dashboard_rev` sequence
          for `disabled_cogs` and `command_settings` (including a subcommand) with string IDs saved,
          while preserving protected `Core` command settings (`ping`).
        """
        # 1. Verify real discord.py container types
        self.assertIsInstance(self.real_bot.cogs, Mapping)
        self.assertNotIsInstance(self.real_bot.cogs, dict)
        self.assertIsInstance(self.real_bot.extensions, Mapping)
        self.assertNotIsInstance(self.real_bot.extensions, dict)
        self.assertIsInstance(self.real_bot.guilds, Sequence)
        self.assertNotIsInstance(self.real_bot.guilds, (list, tuple, set))

        owner_sid, owner_doc = await self._make_session(self.owner_user_id, "BotOwner")
        admin_sid, admin_doc = await self._make_session(self.admin_user_id, "ServerAdmin")
        owner_headers = {"X-CSRF-Token": owner_doc["csrf_token"]}
        admin_headers = {"X-CSRF-Token": admin_doc["csrf_token"]}

        # 2. GET /modules/{guild_id} lists Moderation commands & subcommands, hides enabled Core
        r_mod_page = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_mod_page.status, 200)
        mod_body = await r_mod_page.text()
        self.assertNotIn("No configurable modules found", mod_body)
        self.assertIn('data-cog-card="Moderation"', mod_body)
        self.assertNotIn('data-cog-card="Core"', mod_body)
        self.assertIn('data-cmd-block="warn"', mod_body)
        self.assertIn('data-cmd-block="filter"', mod_body)
        self.assertIn('data-subcommand="add"', mod_body)
        self.assertIn('data-subcommand="remove"', mod_body)
        self.assertIn('data-cmd-block="purge"', mod_body)
        self.assertIn('data-cmd-block="slash_only"', mod_body)
        self.assertIn('data-cmd-block="lookup"', mod_body)
        self.assertIn('data-subcommand="wiki"', mod_body)
        self.assertIn('data-subcommand="ipinfo"', mod_body)

        # 3. GET /owner_panel shows Cogs: 2 and one reload button per extension
        r_owner_page = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner_page.status, 200)
        owner_body = await r_owner_page.text()
        self.assertIn("Guilds: 0 | Cogs: 2", owner_body)
        self.assertNotIn("No extensions loaded.", owner_body)
        self.assertIn('data-action="reload-cog"', owner_body)
        self.assertIn('data-cog="real_mod_test"', owner_body)
        self.assertIn('data-cog="real_core_test"', owner_body)
        self.assertEqual(owner_body.count('data-action="reload-cog"'), 2)

        # 4. POST /api/owner/action reloads the real extension `cogs.real_mod_test`
        mod_cog_before = self.real_bot.get_cog("Moderation")
        self.assertIsNotNone(mod_cog_before)
        r_reload = await self.client.post(
            "/api/owner/action",
            json={"action": "reload_cog", "cog": "real_mod_test"},
            headers=owner_headers,
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_reload.status, 200)
        reload_json = await r_reload.json()
        self.assertEqual(reload_json, {"status": "ok", "reloaded": "cogs.real_mod_test"})
        mod_cog_after = self.real_bot.get_cog("Moderation")
        self.assertIsNotNone(mod_cog_after)
        self.assertIsNot(mod_cog_after, mod_cog_before)

        # 5. POST /api/modules/{guild_id} with rev 0 -> 1 for disabled_cogs
        r_save_cogs = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"disabled_cogs": ["Moderation"], "dashboard_rev": 0},
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_save_cogs.status, 200)
        save_cogs_json = await r_save_cogs.json()
        self.assertEqual(save_cogs_json["dashboard_rev"], 1)
        self.assertEqual(self.guild_settings_doc["disabled_cogs"], ["Moderation"])

        # 6. POST /api/modules/{guild_id} with rev 1 -> 2 for command_settings across all command types
        r_save_cmds = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "command_settings": {
                    "filter": {
                        "disabled_subcommands": ["add"],
                        "allowed_channels": ["1001"],
                        "ignored_channels": ["1002"],
                        "allowed_roles": ["2001"],
                        "ignored_roles": ["2002"],
                    },
                    "warn": {
                        "allowed_roles": ["2001"],
                    },
                    "purge": {
                        "ignored_channels": ["1002"],
                    },
                    "slash_only": {
                        "allowed_channels": ["1001"],
                    },
                    "lookup": {
                        "disabled_subcommands": ["wiki"],
                        "allowed_roles": ["2001"],
                    },
                },
                "dashboard_rev": 1,
            },
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_save_cmds.status, 200)
        save_cmds_json = await r_save_cmds.json()
        self.assertEqual(save_cmds_json["dashboard_rev"], 2)

        saved_cs = self.guild_settings_doc["command_settings"]
        # Protected Core command `ping` preserved!
        self.assertEqual(saved_cs["ping"], {"allowed_channels": ["1001"]})
        self.assertEqual(
            saved_cs["filter"],
            {
                "disabled_subcommands": ["add"],
                "allowed_channels": ["1001"],
                "ignored_channels": ["1002"],
                "allowed_roles": ["2001"],
                "ignored_roles": ["2002"],
            },
        )
        self.assertEqual(
            saved_cs["lookup"],
            {
                "disabled_subcommands": ["wiki"],
                "allowed_channels": [],
                "ignored_channels": [],
                "allowed_roles": ["2001"],
                "ignored_roles": [],
            },
        )
        self.assertEqual(saved_cs["slash_only"]["allowed_channels"], ["1001"])
        self.assertEqual(saved_cs["purge"]["ignored_channels"], ["1002"])
        self.assertTrue(all(isinstance(cid, str) for cid in saved_cs["filter"]["allowed_channels"]))
        self.assertTrue(all(isinstance(rid, str) for rid in saved_cs["filter"]["allowed_roles"]))

    async def test_fix2_visit_logs_recording_throttling_ttl_and_owner_panel_your_ip(self):
        """
        FIX 2 Regression Test:
        - Fast-path `GET /health` and unauthenticated `GET /` NEVER write to `db.visit_logs`.
        - Non-fast-path unauthenticated request (`GET /servers`) records `last_user="Guest"`,
          `hits=1`, float `last_visit`, and UTC `datetime` `last_visit_at`, plus creates the
          30-day TTL index (`expireAfterSeconds=2592000`) on `last_visit_at`.
        - Repeat requests from the same IP within 5 minutes are throttled in memory.
        - After throttle window (or for a fresh IP / cleared throttle), authenticated request
          records `last_user=<Discord username>` and increments `hits`.
        - Untrusted IP requests (`trusted_hops=1` with spoofed invalid XFF and no origin secret)
          or DB failures/timeouts never block or fail the HTTP response.
        - `/owner_panel` displays `"Your current IP: 127.0.0.1"` at the top of Ingress Telemetry
          and marks the matching row `"Your IP"` (`data-your-ip="true"`) with its Ban button disabled.
        """
        self.visit_docs.clear()
        self.visit_tracker.clear()
        self.fake_db.visit_logs.update_one.reset_mock()
        self.fake_db.visit_logs.create_index.reset_mock()

        # 1. Fast-path GET /health and unauthenticated GET / must NOT record visits
        r_health = await self.client.get("/health")
        self.assertEqual(r_health.status, 200)
        r_home = await self.client.get("/")
        self.assertEqual(r_home.status, 200)
        await self.visit_tracker.flush()
        self.assertEqual(len(self.visit_docs), 0)

        # 2. Unauthenticated non-fast-path GET /servers records last_user="Guest" and creates 30d TTL index
        r_servers_guest = await self.client.get("/servers", allow_redirects=False)
        self.assertEqual(r_servers_guest.status, 302)
        await self.visit_tracker.flush()

        self.assertIn("127.0.0.1", self.visit_docs)
        doc1 = self.visit_docs["127.0.0.1"]
        self.assertEqual(doc1["ip"], "127.0.0.1")
        self.assertEqual(doc1["last_user"], "Guest")
        self.assertEqual(doc1["hits"], 1)
        self.assertIsInstance(doc1["last_visit"], float)
        self.assertIsInstance(doc1["last_visit_at"], datetime.datetime)
        self.fake_db.visit_logs.create_index.assert_awaited_with(
            "last_visit_at",
            expireAfterSeconds=VISIT_LOGS_TTL_SECONDS,
        )

        # 3. Second request within 5 minutes from the same IP is throttled (hits stays 1)
        r_servers_guest_2 = await self.client.get("/servers", allow_redirects=False)
        self.assertEqual(r_servers_guest_2.status, 302)
        await self.visit_tracker.flush()
        self.assertEqual(self.visit_docs["127.0.0.1"]["hits"], 1)

        # 4. Simulate 5 minutes elapsed -> authenticated request updates last_user to Discord username and increments hits
        self.visit_tracker._last_recorded["127.0.0.1"] -= 301.0
        owner_sid, _ = await self._make_session(self.owner_user_id, "RecluseOwner")

        r_owner = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner.status, 200)
        await self.visit_tracker.flush()

        self.assertEqual(self.visit_docs["127.0.0.1"]["last_user"], "RecluseOwner")
        self.assertEqual(self.visit_docs["127.0.0.1"]["hits"], 2)

        # 5. Fetch /owner_panel again and verify "Your current IP: 127.0.0.1" and "Your IP" row + disabled Ban button
        r_owner_2 = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner_2.status, 200)
        panel_body = await r_owner_2.text()
        self.assertIn("Your current IP: 127.0.0.1", panel_body)
        self.assertIn('data-ip="127.0.0.1" data-your-ip="true"', panel_body)
        self.assertRegex(
            panel_body,
            r'data-action="ban"\s+data-ip="127\.0\.0\.1"[^>]*disabled[\s\S]*?Your IP',
        )

        # 6. Untrusted IP must NEVER be recorded in db.visit_logs
        untrusted_task = self.visit_tracker.record_visit(
            "203.0.113.99",
            ip_trusted=False,
            session_doc={"username": "Spoofer"},
        )
        self.assertIsNone(untrusted_task)
        self.assertNotIn("203.0.113.99", self.visit_docs)

        # 7. Fire-and-forget resilience: even if db.visit_logs.update_one raises, request succeeds
        self.visit_tracker._last_recorded["127.0.0.1"] -= 301.0
        self.fake_db.visit_logs.update_one = AsyncMock(side_effect=RuntimeError("Mongo write error"))
        r_resilient = await self.client.get(
            "/servers",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_resilient.status, 200)
        await self.visit_tracker.flush()

    async def test_fix3_legacy_disabled_cmds_reenable_api_and_ui(self):
        """
        FIX 3 Regression Test:
        - `/modules/{guild_id}` renders per-entry remove buttons (`data-action="reenable-legacy-cmd"`)
          for users with `commands.edit`, and omits remove buttons for users with only `modules.edit`.
        - `POST /api/modules/{guild_id}` with `reenable_cmds`:
          * Rejects users lacking `commands.edit` with 403.
          * Rejects command names not currently in `disabled_cmds` with 400 (re-enable only).
          * Rejects non-list or control-char payloads with 400.
          * Returns 503 on a flaky pre-write MongoDB read without modifying `disabled_cmds`.
          * Returns 409 on stale `dashboard_rev`.
          * Removes valid entries from `disabled_cmds`, increments `dashboard_rev`, invalidates
            `GuildConfigCache`, and records a non-empty audit log entry.
        """
        admin_sid, admin_doc = await self._make_session(self.admin_user_id, "ServerAdmin")
        mod_sid, mod_doc = await self._make_session(self.mod_only_user_id, "ModulesOnlyEditor")
        admin_headers = {"X-CSRF-Token": admin_doc["csrf_token"]}
        mod_headers = {"X-CSRF-Token": mod_doc["csrf_token"]}

        # 1. UI check: Admin (has commands.edit) sees remove buttons; ModulesOnlyEditor (lacks commands.edit) does not
        r_admin_page = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_admin_page.status, 200)
        admin_page_body = await r_admin_page.text()
        self.assertIn('data-reenable-cmd="legacy_ban"', admin_page_body)
        self.assertIn('data-reenable-cmd="legacy_kick"', admin_page_body)
        self.assertIn('data-reenable-cmd="old_mute"', admin_page_body)

        r_mod_page = await self.client.get(
            f"/modules/{self.guild_id}",
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_mod_page.status, 200)
        mod_page_body = await r_mod_page.text()
        self.assertIn("/legacy_ban", mod_page_body)
        self.assertNotIn('data-action="reenable-legacy-cmd"', mod_page_body)

        # 2. User lacking `commands.edit` cannot call POST /api/modules with reenable_cmds -> 403
        r_403 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"reenable_cmds": ["legacy_ban"], "dashboard_rev": 0},
            headers=mod_headers,
            cookies={SESSION_COOKIE_NAME: mod_sid},
        )
        self.assertEqual(r_403.status, 403)

        # 3. Cannot pass a command name not currently in disabled_cmds (re-enable only) -> 400
        for bad_reenable in (
            {"reenable_cmds": ["warn"], "dashboard_rev": 0},
            {"reenable_cmds": ["not_in_list"], "dashboard_rev": 0},
            {"reenable_cmds": "legacy_ban", "dashboard_rev": 0},
            {"reenable_cmds": [123], "dashboard_rev": 0},
            {"reenable_cmds": ["legacy_ban\x00"], "dashboard_rev": 0},
        ):
            self.limiter.reset()
            r_bad = await self.client.post(
                f"/api/modules/{self.guild_id}",
                json=bad_reenable,
                headers=admin_headers,
                cookies={SESSION_COOKIE_NAME: admin_sid},
            )
            self.assertEqual(r_bad.status, 400, f"Expected 400 for {bad_reenable}")

        # 4. Flaky MongoDB read -> 503 before any write
        orig_find = self.fake_db.guild_settings.find_one.side_effect
        calls = {"n": 0}

        async def _flaky_find(flt: dict):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Transient DB failure")
            return await orig_find(flt)

        self.fake_db.guild_settings.find_one = AsyncMock(side_effect=_flaky_find)
        r_503 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"reenable_cmds": ["legacy_ban"], "dashboard_rev": 0},
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_503.status, 503)
        self.assertEqual(
            self.guild_settings_doc["disabled_cmds"],
            ["legacy_ban", "legacy_kick", "old_mute"],
        )

        # 5. Valid reenable_cmds removes "legacy_ban", increments dashboard_rev to 1, and records audit diff
        await self.cfg_cache.get(self.guild_id)
        self.assertEqual(len(self.cfg_cache), 1)

        r_ok = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"reenable_cmds": ["legacy_ban"], "dashboard_rev": 0},
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_ok.status, 200)
        ok_json = await r_ok.json()
        self.assertEqual(ok_json["dashboard_rev"], 1)
        self.assertEqual(
            self.guild_settings_doc["disabled_cmds"],
            ["legacy_kick", "old_mute"],
        )
        self.assertEqual(len(self.cfg_cache), 0)
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(self.audit_rows[-1]["action"], "reenable_disabled_cmds")
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {
                "disabled_cmds": {
                    "old": ["legacy_ban", "legacy_kick", "old_mute"],
                    "new": ["legacy_kick", "old_mute"],
                }
            },
        )

        # 6. Stale rev 0 now returns 409 Conflict
        r_409 = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={"reenable_cmds": ["legacy_kick"], "dashboard_rev": 0},
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_409.status, 409)

    async def test_key_consistency_and_core_enforcement_all_four_command_types(self):
        """
        Verifies side-by-side key consistency between `webdash/router.py` (`_extract_cog_commands`
        and `POST /api/modules/{guild_id}`) and `cogs/core.py` (`Core.bot_check` at lines 737-777
        and `Core.global_slash_check` at lines 796-829), and confirms restrictions saved from
        the page are actually enforced by `Core.bot_check` / `Core.global_slash_check` for:
          (a) prefix commands (`purge`)
          (b) hybrid commands (`warn`) and hybrid group subcommands (`filter add`)
          (c) pure slash commands (`slash_only`)
          (d) slash group subcommands (`lookup wiki`)
        """
        from discord import app_commands
        from cogs.core import Core
        from webdash.router import _extract_cog_commands

        mod_cog = self.real_bot.get_cog("Moderation")
        self.assertIsNotNone(mod_cog)
        extracted = _extract_cog_commands(mod_cog)

        # 1. Verify exact keys extracted by router.py match core.py's root_parent.name / name derivation
        # (a) Prefix command `purge`
        pref_cmd = self.real_bot.get_command("purge")
        self.assertIsNotNone(pref_cmd)
        core_key_a = pref_cmd.root_parent.name if pref_cmd.root_parent else pref_cmd.name
        core_sub_a = pref_cmd.name if pref_cmd.parent else None
        self.assertEqual((core_key_a, core_sub_a), ("purge", None))
        self.assertIn("purge", extracted)

        # (b) Hybrid command `warn` and hybrid group subcommand `filter add`
        hyb_cmd = self.real_bot.get_command("warn")
        hyb_app = self.real_bot.tree.get_command("warn")
        self.assertEqual(
            (hyb_cmd.root_parent.name if hyb_cmd.root_parent else hyb_cmd.name),
            "warn",
        )
        self.assertEqual(
            (hyb_app.root_parent.name if getattr(hyb_app, "root_parent", None) else hyb_app.name),
            "warn",
        )
        self.assertIn("warn", extracted)

        hyb_sub_ctx = self.real_bot.get_command("filter add")
        hyb_grp_app = self.real_bot.tree.get_command("filter")
        hyb_sub_app = hyb_grp_app.get_command("add")
        self.assertEqual(
            (
                hyb_sub_ctx.root_parent.name if hyb_sub_ctx.root_parent else hyb_sub_ctx.name,
                hyb_sub_ctx.name if hyb_sub_ctx.parent else None,
            ),
            ("filter", "add"),
        )
        self.assertEqual(
            (
                hyb_sub_app.root_parent.name if getattr(hyb_sub_app, "root_parent", None) else hyb_sub_app.name,
                hyb_sub_app.name if getattr(hyb_sub_app, "parent", None) else None,
            ),
            ("filter", "add"),
        )
        self.assertIn("filter", extracted)
        self.assertIn("add", extracted["filter"]["subs"])

        # (c) Pure slash command `slash_only`
        slash_cmd = self.real_bot.tree.get_command("slash_only")
        self.assertIsNotNone(slash_cmd)
        core_key_c = slash_cmd.root_parent.name if getattr(slash_cmd, "root_parent", None) else slash_cmd.name
        core_sub_c = slash_cmd.name if getattr(slash_cmd, "parent", None) else None
        self.assertEqual((core_key_c, core_sub_c), ("slash_only", None))
        self.assertIn("slash_only", extracted)

        # (d) Slash group subcommand `lookup wiki`
        lookup_grp = self.real_bot.tree.get_command("lookup")
        self.assertIsNotNone(lookup_grp)
        wiki_sub = lookup_grp.get_command("wiki")
        self.assertIsNotNone(wiki_sub)
        core_key_d = wiki_sub.root_parent.name if getattr(wiki_sub, "root_parent", None) else wiki_sub.name
        core_sub_d = wiki_sub.name if getattr(wiki_sub, "parent", None) else None
        self.assertEqual((core_key_d, core_sub_d), ("lookup", "wiki"))
        self.assertIn("lookup", extracted)
        self.assertIn("wiki", extracted["lookup"]["subs"])

        # 2. Save restrictions via POST /api/modules/{guild_id} and verify runtime enforcement by Core
        admin_sid, admin_doc = await self._make_session(self.admin_user_id, "ServerAdmin")
        admin_headers = {"X-CSRF-Token": admin_doc["csrf_token"]}
        r_save = await self.client.post(
            f"/api/modules/{self.guild_id}",
            json={
                "command_settings": {
                    "purge": {"ignored_channels": ["1002"]},
                    "warn": {"allowed_roles": ["2001"]},
                    "filter": {"disabled_subcommands": ["add"]},
                    "slash_only": {"allowed_channels": ["1001"]},
                    "lookup": {"disabled_subcommands": ["wiki"], "allowed_roles": ["2001"]},
                },
                "dashboard_rev": 0,
            },
            headers=admin_headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_save.status, 200)

        core_stub = types.SimpleNamespace(bot=self.real_bot, _original_interaction_check=None)

        class _RealDiscordMemberStub(discord.Member):
            def __init__(self, uid: int, *, admin: bool = False, role_ids: list[int] | None = None):
                self._uid = uid
                self._admin = admin
                self._role_objs = [_FakeRole(rid, f"r-{rid}") for rid in (role_ids or [])]

            @property
            def id(self) -> int:
                return self._uid

            @property
            def guild_permissions(self):
                return _FakePermissions(
                    DISCORD_PERM_ADMINISTRATOR if self._admin else 0,
                    administrator=self._admin,
                )

            @property
            def roles(self):
                return self._role_objs

        non_admin_no_role = _RealDiscordMemberStub(777001, admin=False, role_ids=[2002])
        non_admin_with_mod_role = _RealDiscordMemberStub(777002, admin=False, role_ids=[2001])

        # Enforce (a): prefix command `purge` blocked in ignored channel 1002, allowed in 1001
        ctx_purge_blocked = types.SimpleNamespace(
            guild=self.fake_guild,
            command=pref_cmd,
            author=non_admin_no_role,
            channel=_FakeChannel(1002, "mod-log"),
        )
        with self.assertRaises(commands.CheckFailure):
            await Core.bot_check(core_stub, ctx_purge_blocked)  # type: ignore[arg-type]

        ctx_purge_ok = types.SimpleNamespace(
            guild=self.fake_guild,
            command=pref_cmd,
            author=non_admin_no_role,
            channel=_FakeChannel(1001, "general"),
        )
        self.assertTrue(await Core.bot_check(core_stub, ctx_purge_ok))  # type: ignore[arg-type]

        # Enforce (b): hybrid command `warn` role restriction & hybrid subcommand `filter add` disabled_subcommands
        ctx_warn_blocked = types.SimpleNamespace(
            guild=self.fake_guild,
            command=hyb_cmd,
            author=non_admin_no_role,
            channel=_FakeChannel(1001, "general"),
        )
        with self.assertRaises(commands.CheckFailure):
            await Core.bot_check(core_stub, ctx_warn_blocked)  # type: ignore[arg-type]

        inter_warn_blocked = types.SimpleNamespace(
            guild=self.fake_guild,
            command=hyb_app,
            user=non_admin_no_role,
            channel_id=1001,
        )
        with self.assertRaises(app_commands.CheckFailure):
            await Core.global_slash_check(core_stub, inter_warn_blocked)  # type: ignore[arg-type]

        ctx_filter_add = types.SimpleNamespace(
            guild=self.fake_guild,
            command=hyb_sub_ctx,
            author=non_admin_with_mod_role,
            channel=_FakeChannel(1001, "general"),
        )
        with self.assertRaises(commands.CheckFailure):
            await Core.bot_check(core_stub, ctx_filter_add)  # type: ignore[arg-type]

        inter_filter_add = types.SimpleNamespace(
            guild=self.fake_guild,
            command=hyb_sub_app,
            user=non_admin_with_mod_role,
            channel_id=1001,
        )
        with self.assertRaises(app_commands.CheckFailure):
            await Core.global_slash_check(core_stub, inter_filter_add)  # type: ignore[arg-type]

        # Enforce (c): pure slash command `slash_only` allowed only in channel 1001
        inter_slash_blocked = types.SimpleNamespace(
            guild=self.fake_guild,
            command=slash_cmd,
            user=non_admin_no_role,
            channel_id=1002,
        )
        with self.assertRaises(app_commands.CheckFailure):
            await Core.global_slash_check(core_stub, inter_slash_blocked)  # type: ignore[arg-type]

        inter_slash_ok = types.SimpleNamespace(
            guild=self.fake_guild,
            command=slash_cmd,
            user=non_admin_no_role,
            channel_id=1001,
        )
        self.assertTrue(await Core.global_slash_check(core_stub, inter_slash_ok))  # type: ignore[arg-type]

        # Enforce (d): slash group subcommand `lookup wiki` blocked by disabled_subcommands,
        # and sibling `lookup ipinfo` blocked for user without allowed_roles=["2001"]
        inter_lookup_wiki = types.SimpleNamespace(
            guild=self.fake_guild,
            command=wiki_sub,
            user=non_admin_with_mod_role,
            channel_id=1001,
        )
        with self.assertRaises(app_commands.CheckFailure):
            await Core.global_slash_check(core_stub, inter_lookup_wiki)  # type: ignore[arg-type]

        ipinfo_sub = lookup_grp.get_command("ipinfo")
        inter_lookup_ipinfo_blocked = types.SimpleNamespace(
            guild=self.fake_guild,
            command=ipinfo_sub,
            user=non_admin_no_role,
            channel_id=1001,
        )
        with self.assertRaises(app_commands.CheckFailure):
            await Core.global_slash_check(core_stub, inter_lookup_ipinfo_blocked)  # type: ignore[arg-type]

        inter_lookup_ipinfo_ok = types.SimpleNamespace(
            guild=self.fake_guild,
            command=ipinfo_sub,
            user=non_admin_with_mod_role,
            channel_id=1001,
        )
        self.assertTrue(await Core.global_slash_check(core_stub, inter_lookup_ipinfo_ok))  # type: ignore[arg-type]

    async def test_owner_panel_guilds_sequence_proxy_count_two(self):
        """
        Requirement 5:
        When `bot.guilds` is a real `discord.utils.SequenceProxy` over two fake guilds,
        `GET /owner_panel` displays `Guilds: 2`.
        """
        second_guild = _FakeGuild(987654321098765432, "Second Test Server", owner_id=self.admin_user_id)
        self.real_bot._connection._guilds[self.fake_guild.id] = self.fake_guild  # type: ignore[index]
        self.real_bot._connection._guilds[second_guild.id] = second_guild  # type: ignore[index]

        self.assertIsInstance(self.real_bot.guilds, discord.utils.SequenceProxy)
        self.assertEqual(len(self.real_bot.guilds), 2)

        owner_sid, _ = await self._make_session(self.owner_user_id, "BotOwner")
        r_owner = await self.client.get(
            "/owner_panel",
            cookies={SESSION_COOKIE_NAME: owner_sid},
        )
        self.assertEqual(r_owner.status, 200)
        owner_body = await r_owner.text()
        self.assertIn("Guilds: 2 | Cogs: 2", owner_body)

    async def test_real_bot_toggle_ai_chat_and_automod_with_ai_cog_cache_invalidation(self):
        """
        Verify `toggleAiChat` and `toggleAutomod` against a REAL `commands.Bot` with a
        real `commands.Cog` named `AI` loaded into `bot.cogs` (`mappingproxy`) that exposes
        `_invalidate_guild_cfg(guild_id)` with a call counter and an in-memory `_guild_cfg` dict:
        - Absent `ai_enabled` (effective True) + `enabled: True` -> 200 `{"status": "noop"}`,
          zero DB writes, zero rev bump, zero audit rows, `_invalidate_guild_cfg` not called.
        - Absent `ai_enabled` (effective True) + `enabled: False` -> 200 `{"status": "ok", "dashboard_rev": 1}`,
          writes only `{"ai_enabled": False}`, pops `guild_id` from `ai_cog._guild_cfg` and increments
          `ai_cog.invalidate_calls`, records audit row with `old: True, new: False`.
        - Absent `automod_enabled` (effective False) + `enabled: False` -> 200 `{"status": "noop", "dashboard_rev": 1}`.
        - Absent `automod_enabled` (effective False) + `enabled: True` -> 200 `{"status": "ok", "dashboard_rev": 2}`,
          writes only `{"automod_enabled": True}`, calls `ai_cog._invalidate_guild_cfg`, records audit row
          with `old: False, new: True`.
        """
        class StubAICog(commands.Cog, name="AI"):
            def __init__(self, bot: commands.Bot):
                self.bot = bot
                self._guild_cfg: dict[int, dict[str, Any]] = {}
                self.invalidate_calls: list[int] = []

            def _invalidate_guild_cfg(self, guild_id: int) -> None:
                self.invalidate_calls.append(guild_id)
                self._guild_cfg.pop(guild_id, None)

        ai_cog = StubAICog(self.real_bot)
        await self.real_bot.add_cog(ai_cog)
        self.assertIs(self.real_bot.get_cog("AI"), ai_cog)

        # Start with a guild_settings doc where ai_enabled and automod_enabled are absent
        self.guild_settings_doc = {
            "guild_id": self.guild_id,
            "default_ai_model": "auto",
            "banned_words": ["badsub"],
        }
        ai_cog._guild_cfg[self.guild_id] = dict(self.guild_settings_doc)
        self.audit_rows.clear()

        admin_sid, admin_doc = await self._make_session(self.admin_user_id, "RealBotAdmin")
        headers = {"X-CSRF-Token": admin_doc["csrf_token"]}

        # 1. No-op when ai_enabled is absent (effective True) and enabled=True is requested
        r_noop_ai = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "toggle", "module": "toggleAiChat", "enabled": True, "dashboard_rev": 0},
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_noop_ai.status, 200)
        self.assertEqual(await r_noop_ai.json(), {"status": "noop", "dashboard_rev": 0})
        self.assertEqual(ai_cog.invalidate_calls, [])
        self.assertIn(self.guild_id, ai_cog._guild_cfg)
        self.assertEqual(len(self.audit_rows), 0)

        # 2. Real change: toggleAiChat -> False (rev 0 -> 1)
        r_ai_off = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "toggle", "module": "toggleAiChat", "enabled": False, "dashboard_rev": 0},
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_ai_off.status, 200)
        self.assertEqual(await r_ai_off.json(), {"status": "ok", "dashboard_rev": 1})
        self.assertIs(self.guild_settings_doc.get("ai_enabled"), False)
        self.assertEqual(self.guild_settings_doc.get("dashboard_rev"), 1)
        self.assertEqual(ai_cog.invalidate_calls, [self.guild_id])
        self.assertNotIn(self.guild_id, ai_cog._guild_cfg)
        self.assertEqual(len(self.audit_rows), 1)
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"ai_enabled": {"old": True, "new": False}},
        )

        # 3. No-op when automod_enabled is absent (effective False) and enabled=False is requested
        r_noop_auto = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "toggle", "module": "toggleAutomod", "enabled": False, "dashboard_rev": 1},
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_noop_auto.status, 200)
        self.assertEqual(await r_noop_auto.json(), {"status": "noop", "dashboard_rev": 1})
        self.assertEqual(len(ai_cog.invalidate_calls), 1)
        self.assertEqual(len(self.audit_rows), 1)

        # 4. Real change: toggleAutomod -> True (rev 1 -> 2)
        ai_cog._guild_cfg[self.guild_id] = dict(self.guild_settings_doc)
        r_auto_on = await self.client.post(
            f"/api/settings/{self.guild_id}",
            json={"type": "toggle", "module": "toggleAutomod", "enabled": True, "dashboard_rev": 1},
            headers=headers,
            cookies={SESSION_COOKIE_NAME: admin_sid},
        )
        self.assertEqual(r_auto_on.status, 200)
        self.assertEqual(await r_auto_on.json(), {"status": "ok", "dashboard_rev": 2})
        self.assertIs(self.guild_settings_doc.get("automod_enabled"), True)
        self.assertEqual(self.guild_settings_doc.get("dashboard_rev"), 2)
        self.assertEqual(ai_cog.invalidate_calls, [self.guild_id, self.guild_id])
        self.assertNotIn(self.guild_id, ai_cog._guild_cfg)
        self.assertEqual(len(self.audit_rows), 2)
        self.assertEqual(
            self.audit_rows[-1]["changes"],
            {"automod_enabled": {"old": False, "new": True}},
        )


if __name__ == "__main__":
    unittest.main()
