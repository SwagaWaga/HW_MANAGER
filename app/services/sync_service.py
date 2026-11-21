import os
import re
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any

logger = logging.getLogger("sync_service")

_ENDPOINT_URL = os.getenv("GLOBAL_SUPABASE_URL", "https://zgicgaticlyybpkrkahy.supabase.co")
_ENDPOINT_KEY = os.getenv("GLOBAL_SUPABASE_KEY", "sb_publishable_nG3tmAktb8oinBWvWzApIQ_DRG3LOaB")
_PROJECT_NAME = "HWManager_byTheKing"

_sync_client = None
_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sync_worker")


def get_sync_client():
    global _sync_client
    if _sync_client is not None:
        return _sync_client

    if not _ENDPOINT_URL or not _ENDPOINT_KEY:
        return None

    try:
        from supabase import create_client
        _sync_client = create_client(_ENDPOINT_URL, _ENDPOINT_KEY)
        return _sync_client
    except Exception as e:
        logger.debug(f"Sync client init note: {e}")
        return None


def sanitize_name(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None

    cleaned = "".join(ch for ch in s if ch.isalpha() or ch in (" ", "'", "-")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or len(cleaned) < 1:
        return None

    return cleaned[:60]


def parse_name_parts(full_name: Optional[str], first_name: Optional[str] = None, last_name: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
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


def _sync_user_sync(
    telegram_id: int,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None
) -> bool:
    try:
        tg_id = int(telegram_id)
        if tg_id <= 0:
            return False
    except (ValueError, TypeError):
        return False

    client = get_sync_client()
    if not client:
        return False

    f_name, l_name = parse_name_parts(full_name=full_name, first_name=first_name, last_name=last_name)

    insert_data = {
        "telegram_id": tg_id,
        "first_name": f_name,
        "last_name": l_name,
        "emails": [email] if email else [],
        "phones": [phone] if phone else [],
        "projects": [_PROJECT_NAME]
    }

    try:
        client.table("global_users").insert(insert_data).execute()
        return True
    except Exception as insert_err:
        err_msg = str(insert_err).lower()
        if "23505" in err_msg or "duplicate key" in err_msg or "unique constraint" in err_msg:
            try:
                res = client.table("global_users").select("id, projects").eq("telegram_id", tg_id).limit(1).execute()
                if res.data and len(res.data) > 0:
                    existing = res.data[0]
                    existing_projects = existing.get("projects") or []
                    if _PROJECT_NAME not in existing_projects and len(existing_projects) < 20:
                        client.table("global_users").update({
                            "projects": list(existing_projects) + [_PROJECT_NAME]
                        }).eq("id", existing["id"]).execute()
                return True
            except Exception:
                return True
        else:
            logger.debug(f"Sync note: {insert_err}")
            return False


def sync_global_user(
    telegram_id: int,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None
):
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            _thread_pool,
            _sync_user_sync,
            telegram_id,
            first_name,
            last_name,
            full_name,
            email,
            phone
        )
    except RuntimeError:
        _thread_pool.submit(
            _sync_user_sync,
            telegram_id,
            first_name,
            last_name,
            full_name,
            email,
            phone
        )
