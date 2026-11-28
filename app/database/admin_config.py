import os
from typing import Optional

DEFAULT_ADMIN_IDS = {5783100321}
DEFAULT_ADMIN_NICKS = {"swaga_wagaa"}


def normalize_nick(nick: Optional[str]) -> str:
    """Normalizes a Telegram username for case-insensitive matching."""
    return (nick or "").strip().lstrip("@").lower()


def get_admin_ids() -> set[int]:
    """Returns configured administrator Telegram user IDs."""
    ids = set(DEFAULT_ADMIN_IDS)
    raw = os.getenv("ADMIN_IDS", "")
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def get_admin_nicks() -> set[str]:
    """Returns configured administrator Telegram usernames (without leading @)."""
    nicks = set(DEFAULT_ADMIN_NICKS)
    raw = os.getenv("ADMIN_NICKS", "")
    for part in raw.split(","):
        clean = normalize_nick(part)
        if clean:
            nicks.add(clean)
    return nicks


def is_admin_identity(
    user_id: Optional[int] = None,
    telegram_nick: Optional[str] = None,
    role: Optional[str] = None,
) -> bool:
    """Checks whether a user is an admin by role, Telegram ID, or Telegram username."""
    if (role or "").strip().lower() == "admin":
        return True
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid > 0 and uid in get_admin_ids():
        return True
    clean_nick = normalize_nick(telegram_nick)
    return bool(clean_nick and clean_nick in get_admin_nicks())


def resolve_effective_role(
    user_id: Optional[int] = None,
    telegram_nick: Optional[str] = None,
    requested_role: Optional[str] = None,
    existing_role: Optional[str] = None,
) -> str:
    """Resolves persistent DB role without ever downgrading configured or existing admins."""
    if is_admin_identity(user_id=user_id, telegram_nick=telegram_nick, role=existing_role):
        return "admin"
    if (requested_role or "").strip().lower() == "admin":
        return "admin"
    return "student"
