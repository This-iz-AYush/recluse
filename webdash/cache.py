"""
webdash/cache.py — Bounded in-memory LRU + TTL caches with short timeouts
and fail-open behavior on database errors.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import logging
import time
from typing import Any, Awaitable, Callable, Generic, TypeVar

from webdash import get_ready_db

logger = logging.getLogger("recluse.webdash.cache")

K = TypeVar("K")
V = TypeVar("V")


class ShortTTLCache(Generic[K, V]):
    """
    Generic bounded in-memory LRU + TTL cache using OrderedDict.
    """

    def __init__(
        self,
        *,
        max_size: int = 500,
        ttl_seconds: float = 60.0,
    ) -> None:
        self.max_size = max(1, int(max_size))
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self._store: collections.OrderedDict[K, tuple[V, float]] = collections.OrderedDict()

    def get_sync(self, key: K, *, now: float | None = None) -> tuple[bool, V | None]:
        current = time.monotonic() if now is None else now
        entry = self._store.get(key)
        if entry is None:
            return False, None
        val, expires_at = entry
        if current >= expires_at:
            self._store.pop(key, None)
            return False, None
        self._store.move_to_end(key)
        return True, val

    def set_sync(self, key: K, value: V, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if key in self._store:
            self._store.move_to_end(key)
        elif len(self._store) >= self.max_size:
            self._store.popitem(last=False)
        self._store[key] = (value, current + self.ttl_seconds)

    def invalidate(self, key: K) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


class GuildConfigCache:
    """
    Bounded in-memory LRU + TTL cache for db.guild_settings per guild_id.
    - Max 200 guilds, 120s TTL by default.
    - Uses getattr(bot, "db_ready", False) via get_ready_db(bot).
    - Enforces short asyncio.wait_for timeout (default 2.0s) and fails OPEN
      (returns {} or last stale snapshot) if MongoDB is unreachable or errors.
    """

    def __init__(
        self,
        bot: Any,
        *,
        max_size: int = 200,
        ttl_seconds: float = 120.0,
        db_timeout: float = 2.0,
    ) -> None:
        self.bot = bot
        self.max_size = max(1, int(max_size))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.db_timeout = max(0.2, float(db_timeout))
        self._cache: collections.OrderedDict[int, tuple[dict[str, Any], float]] = (
            collections.OrderedDict()
        )

    async def get(
        self,
        guild_id: int,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """
        Return guild_settings dict for guild_id.
        Fails open to {} (or stale cached dict if present) on DB timeout/error.
        """
        gid = int(guild_id)
        current = time.monotonic() if now is None else now

        entry = self._cache.get(gid)
        stale_doc: dict[str, Any] | None = None
        if entry is not None:
            doc, expires_at = entry
            if current < expires_at:
                self._cache.move_to_end(gid)
                return copy.deepcopy(doc)
            stale_doc = doc

        db = get_ready_db(self.bot)
        if db is None:
            return copy.deepcopy(stale_doc) if stale_doc is not None else {}

        try:
            fetched = await asyncio.wait_for(
                db.guild_settings.find_one({"guild_id": gid}),
                timeout=self.db_timeout,
            )
            clean_doc: dict[str, Any] = dict(fetched) if isinstance(fetched, dict) else {"guild_id": gid}
            clean_doc.pop("_id", None)
            self.set(gid, clean_doc, now=current)
            return copy.deepcopy(clean_doc)
        except Exception as exc:
            logger.warning(
                "GuildConfigCache fetch failed/timed out for guild_id=%s (%s); failing open",
                gid,
                type(exc).__name__,
            )
            return copy.deepcopy(stale_doc) if stale_doc is not None else {}

    def set(
        self,
        guild_id: int,
        config: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        gid = int(guild_id)
        current = time.monotonic() if now is None else now
        stored = copy.deepcopy(config)
        stored.pop("_id", None)
        if gid in self._cache:
            self._cache.move_to_end(gid)
        elif len(self._cache) >= self.max_size:
            self._cache.popitem(last=False)
        self._cache[gid] = (stored, current + self.ttl_seconds)

    def invalidate(self, guild_id: int) -> None:
        self._cache.pop(int(guild_id), None)
        # Also keep AI cog's local cache in sync if loaded
        if self.bot is not None and hasattr(self.bot, "get_cog"):
            try:
                ai_cog = self.bot.get_cog("AI")
                if ai_cog and hasattr(ai_cog, "_invalidate_guild_cfg"):
                    ai_cog._invalidate_guild_cfg(int(guild_id))
            except Exception:
                pass

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


class IpBanCache:
    """
    Bounded in-memory set of normalized banned IPs backed by `db.ip_bans`.
    - Refreshes at most once every `ttl_seconds` (default 60s).
    - Single-flight refresh (`asyncio.Lock`) with short `db_timeout` (default 2.0s).
    - Serves the previous set while a refresh is in flight so concurrent requests never block.
    - Fails open (preserves previous set, or empty set on cold start) if MongoDB is unavailable
      or errors, advancing expiry by `failure_backoff_seconds` so a flapping DB is not hammered.
    """

    def __init__(
        self,
        bot: Any,
        *,
        max_size: int = 2000,
        ttl_seconds: float = 60.0,
        db_timeout: float = 2.0,
        failure_backoff_seconds: float = 10.0,
    ) -> None:
        self.bot = bot
        self.max_size = max(1, int(max_size))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.db_timeout = max(0.2, float(db_timeout))
        self.failure_backoff_seconds = max(0.5, min(float(failure_backoff_seconds), self.ttl_seconds))
        self._banned_ips: frozenset[str] = frozenset()
        self._expires_at: float = 0.0
        self._has_loaded: bool = False
        self._refresh_lock = asyncio.Lock()

    @property
    def banned_ips(self) -> frozenset[str]:
        return self._banned_ips

    def add_local(self, ip: str) -> None:
        """Immediately add a normalized IP to the in-memory set."""
        from webdash.security import normalize_ip

        norm = normalize_ip(ip)
        if norm != "invalid" and len(self._banned_ips) < self.max_size:
            self._banned_ips = self._banned_ips | {norm}

    def remove_local(self, ip: str) -> None:
        """Immediately remove a normalized IP from the in-memory set."""
        from webdash.security import normalize_ip

        norm = normalize_ip(ip)
        if norm != "invalid" and norm in self._banned_ips:
            self._banned_ips = self._banned_ips - {norm}

    async def refresh(
        self,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> frozenset[str]:
        """
        Perform a single-flight refresh from `db.ip_bans`.
        If another coroutine is already refreshing and `force=False`, returns the
        current in-memory set immediately without waiting.
        """
        from webdash.security import normalize_ip

        current = time.monotonic() if now is None else now
        if not force and current < self._expires_at:
            return self._banned_ips

        if not force and self._has_loaded and self._refresh_lock.locked():
            return self._banned_ips

        async with self._refresh_lock:
            now_locked = time.monotonic() if now is None else now
            if not force and now_locked < self._expires_at:
                return self._banned_ips

            db = get_ready_db(self.bot)
            if db is None or getattr(db, "ip_bans", None) is None:
                self._expires_at = now_locked + self.failure_backoff_seconds
                self._has_loaded = True
                return self._banned_ips

            try:
                cursor = db.ip_bans.find().limit(self.max_size)

                async def _drain() -> list[dict[str, Any]]:
                    docs: list[dict[str, Any]] = []
                    async for doc in cursor:
                        if isinstance(doc, dict):
                            docs.append(doc)
                            if len(docs) >= self.max_size:
                                break
                    return docs

                raw_docs = await asyncio.wait_for(_drain(), timeout=self.db_timeout)
                loaded: set[str] = set()
                for doc in raw_docs:
                    norm = normalize_ip(str(doc.get("ip", "")))
                    if norm != "invalid":
                        loaded.add(norm)
                        if len(loaded) >= self.max_size:
                            break
                self._banned_ips = frozenset(loaded)
                finish_ts = time.monotonic() if now is None else now
                self._expires_at = finish_ts + self.ttl_seconds
                self._has_loaded = True
            except Exception as exc:
                logger.warning(
                    "IpBanCache refresh failed/timed out (%s); failing open with %d cached IPs",
                    type(exc).__name__,
                    len(self._banned_ips),
                )
                finish_ts = time.monotonic() if now is None else now
                self._expires_at = finish_ts + self.failure_backoff_seconds
                self._has_loaded = True

            return self._banned_ips

    async def is_banned(
        self,
        ip: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        """
        Check whether `ip` is in the cached ban set.
        - Serves the previous set immediately if a refresh is currently in flight.
        - Fails open on any DB error.
        """
        from webdash.security import normalize_ip

        norm = normalize_ip(ip)
        if norm == "invalid":
            return False

        current = time.monotonic() if now is None else now
        if current < self._expires_at:
            return norm in self._banned_ips

        if self._has_loaded and self._refresh_lock.locked():
            return norm in self._banned_ips

        current_set = await self.refresh(force=False, now=now)
        return norm in current_set

    def clear(self) -> None:
        self._banned_ips = frozenset()
        self._expires_at = 0.0
        self._has_loaded = False

    def __len__(self) -> int:
        return len(self._banned_ips)


async def fetch_with_fail_open(
    coro_factory: Callable[[], Awaitable[V]],
    default: V,
    *,
    timeout: float = 2.0,
) -> V:
    """Execute an async DB operation with a short timeout, returning `default` on failure."""
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout)
    except Exception:
        return default
