import os
import logging
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

raw_url = os.getenv("SUPABASE_URL", "")
SUPABASE_URL = raw_url.split("/rest/v1")[0].rstrip("/") if raw_url else ""
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

_client = None

def get_supabase_client():
    global _client
    if _client is not None:
        return _client
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    
    try:
        from supabase import create_client, Client
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return _client
    except Exception as e:
        logger.error(f"Error initializing Supabase client: {e}")
        return None

def is_supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY and get_supabase_client() is not None)

def upsert_user(user_id: int, telegram_nick: Optional[str] = None, full_name: Optional[str] = None, role: str = "student", class_name: str = "10-А") -> bool:
    client = get_supabase_client()
    if not client:
        return False
    
    try:
        data = {
            "id": user_id,
            "role": role,
            "class_name": class_name,
            "updated_at": "now()"
        }
        if telegram_nick:
            data["telegram_nick"] = telegram_nick
        if full_name:
            data["full_name"] = full_name

        client.table("users").upsert(data, on_conflict="id").execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase upsert_user: {e}")
        return False

def add_task(user_id: int, subject: str, description: str, due_date: str, class_name: str = "10-А", telegram_nick: Optional[str] = None, full_name: Optional[str] = None) -> Optional[int]:
    client = get_supabase_client()
    if not client:
        return None

    try:
        # 1. Ensure user exists
        if telegram_nick or full_name:
            upsert_user(user_id, telegram_nick=telegram_nick, full_name=full_name, class_name=class_name)
        else:
            upsert_user(user_id, class_name=class_name)

        # 2. Insert task
        task_data = {
            "user_id": user_id,
            "subject": subject,
            "description": description,
            "due_date": due_date,
            "class_name": class_name
        }
        res = client.table("tasks").insert(task_data).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["id"]
        return None
    except Exception as e:
        logger.error(f"Error in Supabase add_task: {e}")
        return None

def get_pending_tasks(user_id: int, class_name: str = "10-А") -> List[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return []

    try:
        # 1. Fetch completed task IDs for this user
        comp_res = client.table("user_completions").select("task_id").eq("user_id", user_id).execute()
        completed_ids = [row["task_id"] for row in (comp_res.data or [])]

        # 2. Query tasks with author info (select users)
        query = client.table("tasks").select("id, user_id, subject, description, due_date, class_name, created_at, users(telegram_nick, full_name, role)")
        if completed_ids:
            # Filter out completed tasks
            # PostgREST not.in syntax:
            query = query.not_.in_("id", completed_ids)

        res = query.order("due_date", desc=False).execute()

        tasks = []
        for row in (res.data or []):
            u_info = row.get("users") or {}
            author_nick = u_info.get("telegram_nick") or f"@ученик_{row['user_id']}"
            author_name = u_info.get("full_name") or ""
            
            task_dict = {
                "id": row["id"],
                "user_id": row["user_id"],
                "subject": row["subject"],
                "description": row["description"],
                "due_date": str(row["due_date"]),
                "author_nick": author_nick,
                "author_name": author_name,
                "role": u_info.get("role", "student")
            }
            tasks.append(task_dict)

        return tasks
    except Exception as e:
        logger.error(f"Error in Supabase get_pending_tasks: {e}")
        return []

def mark_task_done(task_id: int, user_id: int) -> bool:
    client = get_supabase_client()
    if not client:
        return False

    try:
        upsert_user(user_id)
        client.table("user_completions").upsert({
            "user_id": user_id,
            "task_id": task_id
        }, on_conflict="user_id,task_id").execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase mark_task_done: {e}")
        return False

def delete_task(task_id: int, user_id: int) -> bool:
    client = get_supabase_client()
    if not client:
        return False

    try:
        client.table("tasks").delete().eq("id", task_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase delete_task: {e}")
        return False

def add_material(task_id: int, file_id: str, caption: str, uploader_id: int) -> bool:
    client = get_supabase_client()
    if not client:
        return False

    try:
        upsert_user(uploader_id)
        client.table("task_materials").insert({
            "task_id": task_id,
            "file_id": file_id,
            "caption": caption,
            "uploader_id": uploader_id
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase add_material: {e}")
        return False

def get_materials_for_task(task_id: int) -> List[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return []

    try:
        res = client.table("task_materials").select("id, task_id, file_id, caption, uploader_id, users(telegram_nick, full_name)").eq("task_id", task_id).order("id").execute()
        materials = []
        for row in (res.data or []):
            u_info = row.get("users") or {}
            materials.append({
                "id": row["id"],
                "file_id": row["file_id"],
                "caption": row.get("caption") or "",
                "uploader_id": row["uploader_id"],
                "uploader_nick": u_info.get("telegram_nick") or f"@ученик_{row['uploader_id']}",
                "uploader_name": u_info.get("full_name") or ""
            })
        return materials
    except Exception as e:
        logger.error(f"Error in Supabase get_materials_for_task: {e}")
        return []
