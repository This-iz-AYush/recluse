"""
webdash/audit.py — Field-level configuration diffing and 30-day TTL audit trail
in db.dashboard_audit.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import logging
from typing import Any, Iterable

from webdash import get_ready_db
from webdash.security import normalize_ip

logger = logging.getLogger("recluse.webdash.audit")

AUDIT_TTL_SECONDS = 30 * 86400  # 30 days (2,592,000 seconds)
DEFAULT_DB_TIMEOUT_SECONDS = 2.0


def compute_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    keys: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Compute field-level changes between `before` and `after`.
    Returns {field_key: {"old": old_val, "new": new_val}} only for fields that changed.
    Ignores MongoDB '_id'.
    """
    old_map = before if isinstance(before, dict) else {}
    new_map = after if isinstance(after, dict) else {}

    if keys is not None:
        candidate_keys = [k for k in keys if k != "_id"]
    else:
        seen = set(old_map.keys()) | set(new_map.keys())
        seen.discard("_id")
        candidate_keys = sorted(seen)

    changes: dict[str, dict[str, Any]] = {}
    for key in candidate_keys:
        old_val = old_map.get(key)
        new_val = new_map.get(key)
        if old_val != new_val:
            changes[key] = {
                "old": copy.deepcopy(old_val),
                "new": copy.deepcopy(new_val),
            }
    return changes


class AuditLogger:
    """
    Records dashboard configuration mutations to db.dashboard_audit with a 30-day TTL.
    """

    def __init__(
        self,
        bot: Any,
        *,
        db_timeout: float = DEFAULT_DB_TIMEOUT_SECONDS,
    ) -> None:
        self.bot = bot
        self.db_timeout = max(0.5, float(db_timeout))

    def _collection(self) -> Any | None:
        db = get_ready_db(self.bot)
        if db is None:
            return None
        return db.dashboard_audit

    async def ensure_indexes(self) -> bool:
        """Create compound query index and 30-day TTL index on db.dashboard_audit."""
        coll = self._collection()
        if coll is None:
            return False
        try:
            await asyncio.wait_for(
                coll.create_index([("guild_id", 1), ("created_at", -1)]),
                timeout=self.db_timeout,
            )
            await asyncio.wait_for(
                coll.create_index("created_at", expireAfterSeconds=AUDIT_TTL_SECONDS),
                timeout=self.db_timeout,
            )
            return True
        except Exception:
            logger.exception("Failed to create indexes on db.dashboard_audit")
            return False

    async def record(
        self,
        *,
        guild_id: int,
        actor_id: int,
        actor_name: str,
        module: str,
        action: str,
        changes: dict[str, dict[str, Any]],
        before_snapshot: dict[str, Any] | None = None,
        client_ip: str | None = None,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any] | None:
        """
        Insert an audit entry if `changes` is non-empty (or if action is an explicit
        operation like 'restore' / 'lockdown').
        """
        if not changes and action not in ("restore", "lockdown_engage", "lockdown_lift"):
            return None

        coll = self._collection()
        if coll is None:
            return None

        current_time = now or datetime.datetime.now(datetime.timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=datetime.timezone.utc)

        clean_snapshot: dict[str, Any] = {}
        if isinstance(before_snapshot, dict):
            for k in changes:
                if k in before_snapshot and k != "_id":
                    clean_snapshot[k] = copy.deepcopy(before_snapshot[k])

        entry: dict[str, Any] = {
            "guild_id": int(guild_id),
            "actor_id": int(actor_id),
            "actor_name": str(actor_name)[:100],
            "module": str(module)[:64],
            "action": str(action)[:64],
            "changes": changes,
            "before_snapshot": clean_snapshot,
            "ip_norm": normalize_ip(client_ip),
            "created_at": current_time,
        }

        try:
            await asyncio.wait_for(coll.insert_one(entry), timeout=self.db_timeout)
            return entry
        except Exception:
            logger.exception("Failed to write audit log entry for guild_id=%s", guild_id)
            return None
