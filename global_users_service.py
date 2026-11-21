import os
import re
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any

logger = logging.getLogger("global_users")

GLOBAL_SUPABASE_URL = os.getenv("GLOBAL_SUPABASE_URL", "https://zgicgaticlyybpkrkahy.supabase.co")
GLOBAL_SUPABASE_KEY = os.getenv("GLOBAL_SUPABASE_KEY", "sb_publishable_nG3tmAktb8oinBWvWzApIQ_DRG3LOaB")
CURRENT_PROJECT_NAME = "HWManager_byTheKing"

_global_client = None
_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="global_users_worker")


def get_global_supabase_client():
    """Lazily initializes and returns the Supabase client for global_users."""
    global _global_client
    if _global_client is not None:
        return _global_client

    if not GLOBAL_SUPABASE_URL or not GLOBAL_SUPABASE_KEY:
        return None

    try:
        from supabase import create_client
        _global_client = create_client(GLOBAL_SUPABASE_URL, GLOBAL_SUPABASE_KEY)
        return _global_client
    except Exception as e:
        logger.warning(f"Could not initialize global_users Supabase client: {e}")
        return None


def sanitize_name(val: Optional[str]) -> Optional[str]:
    """
    Sanitizes first_name / last_name according to Postgres check constraints:
    valid_first_name / valid_last_name:
      ((name is null) or ((length(TRIM(both from name)) >= 1) and (name::text ~ '^[[:alpha:]\s''\-]+$')))
    Retains only Unicode alphabetic characters, spaces, single quotes, and hyphens.
    Returns None if empty or invalid so the database constraint is never violated.
    """
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None

    # Keep only Unicode letters, spaces, apostrophes, and hyphens
    cleaned = "".join(ch for ch in s if ch.isalpha() or ch in (" ", "'", "-")).strip()
    # Normalize multiple whitespace characters
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or len(cleaned) < 1:
        return None

    return cleaned[:60]


def parse_name_parts(full_name: Optional[str], first_name: Optional[str] = None, last_name: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Extracts and sanitizes first and last name from given components or full_name."""
    f_name = sanitize_name(first_name)
    l_name = sanitize_name(last_name)

    if (not f_name or not l_name) and full_name:
        parts = full_name.strip().split(maxsplit=1)
        if parts:
            if not f_name:
                f_name = sanitize_name(parts[0])
            if not l_name and len(parts) > 1:
                l_name = sanitize_name(parts[1])

    return f_name, l_name


def _sync_global_user_sync(
    telegram_id: int,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None
) -> bool:
    """
    Synchronous worker that inserts or updates a user in public.global_users table.
    Ensures safe handling of unique constraints, jsonb arrays, and check triggers.
    """
    try:
        tg_id = int(telegram_id)
        if tg_id <= 0:
            logger.debug(f"Skipping global user registration: invalid telegram_id {telegram_id}")
            return False
    except (ValueError, TypeError):
        return False

    client = get_global_supabase_client()
    if not client:
        return False

    f_name, l_name = parse_name_parts(full_name=full_name, first_name=first_name, last_name=last_name)

    insert_data = {
        "telegram_id": tg_id,
        "first_name": f_name,
        "last_name": l_name,
        "emails": [email] if email else [],
        "phones": [phone] if phone else [],
        "projects": [CURRENT_PROJECT_NAME]
    }

    try:
        # 1. Попытка прямой вставки нового пользователя (работает даже если у anon роли открыт только INSERT)
        client.table("global_users").insert(insert_data).execute()
        logger.info(f"🌐 global_users: Registered new user telegram_id={tg_id} ({f_name or ''} {l_name or ''})")
        return True
    except Exception as insert_err:
        err_msg = str(insert_err).lower()
        # Если пользователь уже существует (duplicate key / code 23505)
        if "23505" in err_msg or "duplicate key" in err_msg or "unique constraint" in err_msg:
            try:
                # Попытка обновить проект, если предоставлены права SELECT/UPDATE
                res = client.table("global_users").select("id, projects").eq("telegram_id", tg_id).limit(1).execute()
                if res.data and len(res.data) > 0:
                    existing = res.data[0]
                    existing_projects = existing.get("projects") or []
                    if CURRENT_PROJECT_NAME not in existing_projects and len(existing_projects) < 20:
                        client.table("global_users").update({
                            "projects": list(existing_projects) + [CURRENT_PROJECT_NAME]
                        }).eq("id", existing["id"]).execute()
                        logger.info(f"🌐 global_users: Appended project {CURRENT_PROJECT_NAME} for user {tg_id}")
                return True
            except Exception as upd_err:
                logger.debug(f"User {tg_id} already exists in global_users (update skipped: {upd_err})")
                return True
        else:
            logger.warning(f"Note syncing user {tg_id} to global_users: {insert_err}")
            return False


def sync_global_user(
    telegram_id: int,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None
):
    """
    Fire-and-forget background registration of user to global_users.
    Will never block the caller and never raise an exception.
    """
    try:
        # Check if an asyncio event loop is running in the current thread
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            _thread_pool,
            _sync_global_user_sync,
            telegram_id,
            first_name,
            last_name,
            full_name,
            email,
            phone
        )
    except RuntimeError:
        # No running event loop in thread; schedule directly in thread pool
        _thread_pool.submit(
            _sync_global_user_sync,
            telegram_id,
            first_name,
            last_name,
            full_name,
            email,
            phone
        )
