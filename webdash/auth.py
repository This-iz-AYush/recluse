"""
webdash/auth.py — Discord OAuth2 state verification and hashed multi-session
storage in db.webdash_sessions (leaving legacy db.sessions untouched).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import secrets
from typing import Any

from webdash import get_ready_db
from webdash.security import (
    generate_csrf_token,
    is_plausible_session_token,
    normalize_ip,
    safe_compare,
)

logger = logging.getLogger("recluse.webdash.auth")

SESSION_COOKIE_NAME = "recluse_sid"
OAUTH_STATE_COOKIE_NAME = "recluse_oauth_state"

DEFAULT_SESSION_TTL_SECONDS = 86400          # 24 hours
DEFAULT_LAST_SEEN_THROTTLE_SECONDS = 300     # 5 minutes
DEFAULT_DB_TIMEOUT_SECONDS = 2.0


def hash_session_token(raw_token: str) -> str:
    """Return the 64-character SHA-256 hex digest of a raw session token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def hash_user_agent(user_agent: str | None) -> str:
    """Return a 16-character SHA-256 hex prefix of the User-Agent header."""
    if not user_agent or not isinstance(user_agent, str):
        return "none"
    return hashlib.sha256(user_agent.encode("utf-8", errors="ignore")).hexdigest()[:16]


def generate_oauth_state() -> str:
    """Generate a cryptographically random OAuth2 state parameter."""
    return secrets.token_urlsafe(24)


def verify_oauth_state(cookie_state: str | None, query_state: str | None) -> bool:
    """Verify OAuth2 state parameter against cookie state in constant time."""
    return safe_compare(cookie_state, query_state)


def filter_session_guilds(raw_guilds: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """
    Keep only minimal guild metadata in the session document to stay well within
    Render Free Tier memory and MongoDB document limits.
    """
    if not isinstance(raw_guilds, list):
        return []
    filtered: list[dict[str, Any]] = []
    for g in raw_guilds[:200]:
        if not isinstance(g, dict) or "id" not in g:
            continue
        try:
            perms = int(g.get("permissions", 0))
        except (ValueError, TypeError):
            perms = 0
        filtered.append(
            {
                "id": str(g["id"]),
                "name": str(g.get("name", "Server"))[:100],
                "icon": str(g["icon"]) if g.get("icon") else None,
                "owner": bool(g.get("owner", False)),
                "permissions": perms,
            }
        )
    return filtered


def _ensure_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


class SessionManager:
    """
    Manages dashboard user sessions in db.webdash_sessions.
    - Stores only SHA-256(session_id) in session_id_hash.
    - Stores expires_at as a timezone-aware UTC datetime with a TTL index (expireAfterSeconds=0).
    - Throttles last_seen updates to once every 5 minutes (300s).
    - Leaves legacy db.sessions untouched for instant WEBDASH_ENABLED=0 rollback.
    """

    def __init__(
        self,
        bot: Any,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        last_seen_throttle_seconds: int = DEFAULT_LAST_SEEN_THROTTLE_SECONDS,
        db_timeout: float = DEFAULT_DB_TIMEOUT_SECONDS,
    ) -> None:
        self.bot = bot
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.last_seen_throttle_seconds = max(0, int(last_seen_throttle_seconds))
        self.db_timeout = max(0.5, float(db_timeout))

    def _collection(self) -> Any | None:
        db = get_ready_db(self.bot)
        if db is None:
            return None
        return db.webdash_sessions

    async def ensure_indexes(self) -> bool:
        """Create indexes on db.webdash_sessions idempotently."""
        coll = self._collection()
        if coll is None:
            return False
        try:
            await asyncio.wait_for(
                coll.create_index("session_id_hash", unique=True),
                timeout=self.db_timeout,
            )
            await asyncio.wait_for(
                coll.create_index("expires_at", expireAfterSeconds=0),
                timeout=self.db_timeout,
            )
            await asyncio.wait_for(
                coll.create_index("discord_id"),
                timeout=self.db_timeout,
            )
            return True
        except Exception:
            logger.exception("Failed to create indexes on db.webdash_sessions")
            return False

    async def create_session(
        self,
        *,
        discord_id: int,
        username: str,
        avatar: str | None = None,
        guilds: list[dict[str, Any]] | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
        now: datetime.datetime | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """
        Create a new session in db.webdash_sessions.
        Returns (raw_token, session_doc) or None if the database is unavailable.
        """
        coll = self._collection()
        if coll is None:
            return None

        current_time = _ensure_utc(now) or datetime.datetime.now(datetime.timezone.utc)
        expires_at = current_time + datetime.timedelta(seconds=self.ttl_seconds)

        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_session_token(raw_token)
        csrf_token = generate_csrf_token()

        doc: dict[str, Any] = {
            "session_id_hash": token_hash,
            "discord_id": int(discord_id),
            "username": str(username)[:100],
            "avatar": str(avatar) if avatar else None,
            "csrf_token": csrf_token,
            "guilds": filter_session_guilds(guilds),
            "created_at": current_time,
            "last_seen": current_time,
            "expires_at": expires_at,
            "ip_norm": normalize_ip(client_ip),
            "ua_hash": hash_user_agent(user_agent),
        }

        try:
            await asyncio.wait_for(coll.insert_one(doc), timeout=self.db_timeout)
            return raw_token, doc
        except Exception:
            logger.exception("Failed to insert session into db.webdash_sessions")
            return None

    async def get_session(
        self,
        raw_token: str | None,
        *,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any] | None:
        """
        Look up a session by raw cookie token.
        Verifies expiration and updates last_seen only if >= last_seen_throttle_seconds
        have elapsed.
        """
        if not is_plausible_session_token(raw_token):
            return None

        coll = self._collection()
        if coll is None:
            return None

        token_hash = hash_session_token(raw_token)
        current_time = _ensure_utc(now) or datetime.datetime.now(datetime.timezone.utc)

        try:
            doc = await asyncio.wait_for(
                coll.find_one({"session_id_hash": token_hash}),
                timeout=self.db_timeout,
            )
        except Exception:
            logger.exception("Failed to query db.webdash_sessions")
            return None

        if not doc:
            return None

        expires_at = _ensure_utc(doc.get("expires_at"))
        if expires_at is None or expires_at <= current_time:
            return None

        last_seen = _ensure_utc(doc.get("last_seen"))
        should_touch = (
            last_seen is None
            or (current_time - last_seen).total_seconds() >= self.last_seen_throttle_seconds
        )
        if should_touch:
            doc["last_seen"] = current_time
            try:
                await asyncio.wait_for(
                    coll.update_one(
                        {"session_id_hash": token_hash},
                        {"$set": {"last_seen": current_time}},
                    ),
                    timeout=self.db_timeout,
                )
            except Exception:
                logger.debug("Throttled last_seen update failed (non-fatal)", exc_info=True)

        return doc

    async def revoke_session(self, raw_token: str | None) -> bool:
        """Delete a single session by its raw cookie token."""
        if not is_plausible_session_token(raw_token):
            return False
        coll = self._collection()
        if coll is None:
            return False
        token_hash = hash_session_token(raw_token)
        try:
            res = await asyncio.wait_for(
                coll.delete_one({"session_id_hash": token_hash}),
                timeout=self.db_timeout,
            )
            return bool(getattr(res, "deleted_count", 0))
        except Exception:
            logger.exception("Failed to revoke session in db.webdash_sessions")
            return False

    async def revoke_all_user_sessions(self, discord_id: int) -> int:
        """Delete all active sessions for a Discord user ID."""
        coll = self._collection()
        if coll is None:
            return 0
        try:
            res = await asyncio.wait_for(
                coll.delete_many({"discord_id": int(discord_id)}),
                timeout=self.db_timeout,
            )
            return int(getattr(res, "deleted_count", 0))
        except Exception:
            logger.exception("Failed to revoke user sessions in db.webdash_sessions")
            return 0
