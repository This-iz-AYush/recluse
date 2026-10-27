"""
webdash/permissions.py — Granular permission catalog, preset expansion, and
deterministic fail-closed RBAC evaluation engine.
"""

from __future__ import annotations

from typing import Any, Iterable

DISCORD_PERM_ADMINISTRATOR = 0x8
DISCORD_PERM_MANAGE_GUILD = 0x20

PERMISSION_CATALOG: tuple[str, ...] = (
    # Overview & General Settings
    "overview.view",
    "settings.view",
    "settings.edit",
    "settings.export",
    "settings.import",
    # Modules & Commands
    "modules.view",
    "modules.edit",
    "commands.view",
    "commands.edit",
    # Welcome & Reaction Roles
    "welcome.view",
    "welcome.edit",
    "reactionroles.view",
    "reactionroles.edit",
    # Moderation & AutoMod
    "moderation.view",
    "moderation.edit",
    "automod.view",
    "automod.edit",
    # Security & Lockdown
    "antinuke.view",
    "antinuke.edit",
    "lockdown.execute",
    # Auto-Responder
    "autoresponder.view",
    "autoresponder.edit",
    # Leveling & XP
    "leveling.view",
    "leveling.edit",
    # AI Chat
    "ai.view",
    "ai.edit",
    # Tickets
    "tickets.view",
    "tickets.edit",
    # Birthdays
    "birthdays.view",
    "birthdays.edit",
    # Giveaways
    "giveaways.view",
    "giveaways.edit",
    "giveaways.reroll",
    # Reminders
    "reminders.view",
    "reminders.edit",
    # Logs & Audit
    "logging.view",
    "audit.view",
    "audit.restore",
    # Access Control
    "permissions.manage",
)

PERMISSION_SET: frozenset[str] = frozenset(PERMISSION_CATALOG)

# Manage Server default preset: grants everything except antinuke.edit and permissions.manage
MANAGE_GUILD_DEFAULT_NODES: tuple[str, ...] = tuple(
    p for p in PERMISSION_CATALOG if p not in ("antinuke.edit", "permissions.manage")
)

PRESETS: dict[str, tuple[str, ...]] = {
    "Admin": ("*",),
    "ModManager": (
        "overview.view",
        "moderation.*",
        "automod.*",
        "autoresponder.*",
        "tickets.*",
        "giveaways.*",
        "birthdays.*",
        "reminders.*",
        "logging.view",
        "audit.view",
    ),
    "Support": (
        "overview.view",
        "tickets.*",
        "moderation.view",
        "logging.view",
    ),
    "Viewer": ("*.view",),
    "ManageGuildDefault": MANAGE_GUILD_DEFAULT_NODES,
}


def matches_pattern(permission: str, pattern: str) -> bool:
    """
    Check if a concrete permission node (e.g., 'automod.edit') matches a pattern
    ('*', 'automod.*', '*.view', or exact match).
    """
    if pattern == "*":
        return True
    if pattern == permission:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return permission.startswith(f"{prefix}.")
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return permission.endswith(f".{suffix}")
    return False


def expand_entries(entries: str | Iterable[Any] | None) -> list[str]:
    """
    Expand any preset names (e.g. 'ModManager', 'Viewer') into patterns.
    Treats a bare string as a single entry and skips any non-str items.
    """
    if not entries:
        return []
    if isinstance(entries, str):
        iterable: Iterable[Any] = (entries,)
    elif isinstance(entries, (bytes, bytearray, dict)) or not isinstance(entries, Iterable):
        return []
    else:
        iterable = entries

    expanded: list[str] = []
    for item in iterable:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token:
            continue
        if token in PRESETS:
            expanded.extend(PRESETS[token])
        else:
            expanded.append(token)
    return expanded


def permission_in_entries(permission: str, entries: str | Iterable[Any] | None) -> bool:
    """Return True if `permission` is matched by any pattern or preset in `entries`."""
    for pat in expand_entries(entries):
        if matches_pattern(permission, pat):
            return True
    return False


def evaluate_permission(
    permission: str,
    *,
    user_id: int,
    guild_owner_id: int | None = None,
    bot_owner_id: int | None = None,
    is_member: bool = True,
    discord_permissions: int = 0,
    is_administrator: bool = False,
    has_manage_guild: bool = False,
    member_role_ids: Iterable[int | str] = (),
    acl_config: dict[str, Any] | None = None,
) -> bool:
    """
    Deterministic, fail-closed permission evaluator.

    Precedence Order:
    1. Guild membership check: non-members (except Bot Owner) are denied.
    2. Non-lockable-out Root Principals:
       - Bot Owner (user_id == bot_owner_id)
       - Discord Server Owner (user_id == guild_owner_id)
       - Discord Administrator (discord_permissions & 0x8 or is_administrator=True)
       -> Always ALLOW. Cannot be locked out by user_denies or role_mappings denies.
    3. Explicit User Denies (acl_config["user_denies"][str(user_id)]):
       -> If matched, DENY.
    4. Explicit User Overrides / Allows (acl_config["user_overrides"][str(user_id)]):
       -> If matched, ALLOW.
    5. Discord Role Mappings (acl_config["role_mappings"][str(role_id)]):
       -> Across all of the member's roles: if any role denies `permission`, DENY.
       -> Otherwise if any role allows `permission`, ALLOW.
    6. Discord Manage Server (discord_permissions & 0x20 or has_manage_guild=True):
       -> Evaluated against acl_config.get("manage_guild_permissions", PRESETS["ManageGuildDefault"]).
    7. Default: DENY.
    """
    if permission not in PERMISSION_SET:
        return False

    # Step 2 (checked alongside membership for Bot Owner):
    if bot_owner_id is not None and int(user_id) == int(bot_owner_id):
        return True

    # Step 1: Must be a guild member
    if not is_member:
        return False

    # Step 2: Server Owner and Discord Administrator cannot be locked out
    if guild_owner_id is not None and int(user_id) == int(guild_owner_id):
        return True

    admin_bit = bool(int(discord_permissions) & DISCORD_PERM_ADMINISTRATOR) or bool(is_administrator)
    if admin_bit:
        return True

    acl = acl_config if isinstance(acl_config, dict) else {}
    uid_key = str(user_id)

    # Step 3: Explicit user_denies
    user_denies_map = acl.get("user_denies")
    if isinstance(user_denies_map, dict):
        denied_entries = user_denies_map.get(uid_key)
        if permission_in_entries(permission, denied_entries):
            return False

    # Step 4: Explicit user_overrides (allows)
    user_overrides_map = acl.get("user_overrides")
    if isinstance(user_overrides_map, dict):
        allowed_entries = user_overrides_map.get(uid_key)
        if permission_in_entries(permission, allowed_entries):
            return True

    # Step 5: Discord Role Mappings (role deny beats role allow)
    role_mappings = acl.get("role_mappings")
    role_id_strs = [str(rid) for rid in member_role_ids]
    if isinstance(role_mappings, dict) and role_id_strs:
        role_allows: list[str] = []
        for rid_str in role_id_strs:
            mapping = role_mappings.get(rid_str)
            if not isinstance(mapping, dict):
                continue
            if permission_in_entries(permission, mapping.get("deny")):
                return False
            role_allows.extend(expand_entries(mapping.get("allow")))
        if permission_in_entries(permission, role_allows):
            return True

    # Step 6: Manage Server preset (editable in acl_config["manage_guild_permissions"])
    manage_guild_bit = bool(int(discord_permissions) & DISCORD_PERM_MANAGE_GUILD) or bool(has_manage_guild)
    if manage_guild_bit:
        mg_entries = acl.get("manage_guild_permissions", PRESETS["ManageGuildDefault"])
        if permission_in_entries(permission, mg_entries):
            return True

    # Step 7: Default fail-closed
    return False


def resolve_user_permissions(
    *,
    user_id: int,
    guild_owner_id: int | None = None,
    bot_owner_id: int | None = None,
    is_member: bool = True,
    discord_permissions: int = 0,
    is_administrator: bool = False,
    has_manage_guild: bool = False,
    member_role_ids: Iterable[int | str] = (),
    acl_config: dict[str, Any] | None = None,
) -> set[str]:
    """Return the set of all catalog permissions granted to the user."""
    roles_tuple = tuple(member_role_ids)
    return {
        perm
        for perm in PERMISSION_CATALOG
        if evaluate_permission(
            perm,
            user_id=user_id,
            guild_owner_id=guild_owner_id,
            bot_owner_id=bot_owner_id,
            is_member=is_member,
            discord_permissions=discord_permissions,
            is_administrator=is_administrator,
            has_manage_guild=has_manage_guild,
            member_role_ids=roles_tuple,
            acl_config=acl_config,
        )
    }
