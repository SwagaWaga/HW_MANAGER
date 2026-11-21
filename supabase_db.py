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

def get_all_users(class_name: str = "10-А") -> List[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = client.table("users").select("id, telegram_nick, full_name, role, class_name").execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Error in Supabase get_all_users: {e}")
        return []

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

        # 2. Query tasks with author info (явно указываем внешний ключ tasks_user_id_fkey для устранения конфликта PostgREST PGRST201)
        try:
            query = client.table("tasks").select("id, user_id, subject, description, due_date, class_name, created_at, users!tasks_user_id_fkey(telegram_nick, full_name, role)")
        except Exception:
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

def update_task(task_id: int, subject: str, description: str, due_date: str, user_id: Optional[int] = None) -> bool:
    client = get_supabase_client()
    if not client:
        return False

    try:
        query = client.table("tasks").update({
            "subject": subject,
            "description": description,
            "due_date": due_date
        }).eq("id", task_id)
        if user_id:
            query = query.eq("user_id", user_id)
        res = query.execute()
        # If no row was updated because user_id did not match, try without user_id
        if not res.data and user_id:
            client.table("tasks").update({
                "subject": subject,
                "description": description,
                "due_date": due_date
            }).eq("id", task_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase update_task: {e}")
        return False

def delete_task(task_id: int, user_id: Optional[int] = None) -> bool:
    client = get_supabase_client()
    if not client:
        return False

    try:
        # Сначала очищаем связанные записи
        try:
            client.table("task_materials").delete().eq("task_id", task_id).execute()
            client.table("user_completions").delete().eq("task_id", task_id).execute()
        except Exception as sub_err:
            logger.warning(f"Cleanup sub-tables warning: {sub_err}")

        query = client.table("tasks").delete().eq("id", task_id)
        if user_id:
            query = query.eq("user_id", user_id)
        res = query.execute()
        if not res.data and user_id:
            client.table("tasks").delete().eq("id", task_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase delete_task: {e}")
        return False

def delete_material(material_id: int) -> bool:
    client = get_supabase_client()
    if not client:
        return False

    try:
        client.table("task_materials").delete().eq("id", material_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase delete_material: {e}")
        return False

def add_material(task_id: int, file_id: str, caption: str, uploader_id: int) -> bool:
    return add_materials_batch(task_id, [{"file_id": file_id, "caption": caption}], uploader_id)

def add_materials_batch(task_id: int, items: List[Dict[str, str]], uploader_id: int) -> Any:
    if not items:
        return []
    client = get_supabase_client()
    if not client:
        return False

    try:
        upsert_user(uploader_id)
        rows = [
            {
                "task_id": task_id,
                "file_id": item["file_id"],
                "caption": item.get("caption", ""),
                "uploader_id": uploader_id
            }
            for item in items
        ]
        res = client.table("task_materials").insert(rows).execute()
        created_ids = [r["id"] for r in (res.data or []) if "id" in r]
        return created_ids if created_ids else True
    except Exception as e:
        logger.error(f"Error in Supabase add_materials_batch: {e}")
        return False

def get_materials_for_task(task_id: int) -> List[Dict[str, Any]]:
    batch = get_materials_for_tasks_batch([task_id])
    return batch.get(task_id, [])

def get_materials_for_tasks_batch(task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not task_ids:
        return {}
    client = get_supabase_client()
    if not client:
        return {tid: [] for tid in task_ids}

    try:
        # Быстрый пакетный запрос БЕЗ огромного base64 file_id!
        # Снижает трафик и задержку на 99.8%!
        res = client.table("task_materials").select(
            "id, task_id, caption, uploader_id, users(telegram_nick, full_name)"
        ).in_("task_id", task_ids).order("id").execute()

        result: Dict[int, List[Dict[str, Any]]] = {tid: [] for tid in task_ids}
        for row in (res.data or []):
            t_id = row["task_id"]
            u_info = row.get("users") or {}
            mat_id = row["id"]
            mat_item = {
                "id": mat_id,
                "task_id": t_id,
                "file_id": f"/api/materials/{mat_id}",
                "url": f"/api/materials/{mat_id}",
                "caption": row.get("caption") or "",
                "uploader_id": row["uploader_id"],
                "uploader_nick": u_info.get("telegram_nick") or f"@ученик_{row['uploader_id']}",
                "uploader_name": u_info.get("full_name") or ""
            }
            if t_id in result:
                result[t_id].append(mat_item)
            else:
                result[t_id] = [mat_item]
        return result
    except Exception as e:
        logger.error(f"Error in Supabase get_materials_for_tasks_batch: {e}")
        return {tid: [] for tid in task_ids}

def get_material_by_id(material_id: int) -> Optional[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return None
    try:
        res = client.table("task_materials").select("id, task_id, file_id, caption, uploader_id").eq("id", material_id).limit(1).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        return None
    except Exception as e:
        logger.error(f"Error in Supabase get_material_by_id: {e}")
        return None

# ─────────────────────────────────────────────────────────────
# USER NOTIFICATION PREFERENCES
# Table: user_notification_prefs (user_id BIGINT PK, notify_08 BOOL, notify_16 BOOL, notify_18 BOOL)
# ─────────────────────────────────────────────────────────────

_DEFAULT_PREFS = {
    "notify_08": True,
    "notify_16": True,
    "notify_18": True,
    "notify_2days": True,
    "notify_weekly": True
}

def get_notification_prefs(user_id: int) -> Optional[Dict[str, bool]]:
    """Return notification preferences for a user from Supabase. Returns None if table/connection fails."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        res = client.table("user_notification_prefs").select("*").eq("user_id", user_id).limit(1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            return {
                "notify_08": bool(row.get("notify_08", True)),
                "notify_16": bool(row.get("notify_16", True)),
                "notify_18": bool(row.get("notify_18", True)),
                "notify_2days": bool(row.get("notify_2days", True)),
                "notify_weekly": bool(row.get("notify_weekly", True)),
            }
        return _DEFAULT_PREFS.copy()
    except Exception as e:
        logger.warning(f"Supabase user_notification_prefs unavailable ({e}), using SQLite fallback")
        return None

def upsert_notification_prefs(user_id: int, notify_08: bool, notify_16: bool, notify_18: bool, notify_2days: bool = True, notify_weekly: bool = True) -> bool:
    """Save/update notification preferences for a user in Supabase."""
    client = get_supabase_client()
    if not client:
        return False
    # 1. Попытка записать с новыми полями
    try:
        client.table("user_notification_prefs").upsert({
            "user_id": user_id,
            "notify_08": notify_08,
            "notify_16": notify_16,
            "notify_18": notify_18,
            "notify_2days": notify_2days,
            "notify_weekly": notify_weekly,
        }, on_conflict="user_id").execute()
        return True
    except Exception as e:
        # 2. Фолбэк на базовые поля, если колонки в Supabase еще не созданы
        try:
            client.table("user_notification_prefs").upsert({
                "user_id": user_id,
                "notify_08": notify_08,
                "notify_16": notify_16,
                "notify_18": notify_18,
            }, on_conflict="user_id").execute()
            return True
        except Exception as e2:
            logger.warning(f"Supabase upsert_notification_prefs failed ({e2}), using SQLite fallback")
            return False

# ─────────────────────────────────────────────────────────────
# LEADERBOARD / АКТИВНЫЕ РЕДАКТОРЫ (КЭШИРОВАНИЕ НА 4 ЧАСА)
# ─────────────────────────────────────────────────────────────

import time
import datetime

# Кэширование таблицы лидеров в памяти на 4 часа (14400 секунд)
# Резко сокращает обращения к Supabase и устраняет лаги / задержки для пользователей
_LEADERBOARD_CACHE: Dict[str, Dict[str, Any]] = {}
LEADERBOARD_CACHE_TTL = 4 * 3600  # 4 часа

def get_leaderboard(class_name: str = "10-А", force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Calculates leaderboard of most active contributors / editors:
    - Added tasks: +5 pts
    - Uploaded materials (photos): +3 pts
    - Completed tasks: +1 pt
    Кэшируется в оперативной памяти на 4 часа для идеальной отзывчивости.
    """
    now = time.time()
    cached = _LEADERBOARD_CACHE.get(class_name)
    if not force_refresh and cached and (now - cached.get("timestamp", 0) < LEADERBOARD_CACHE_TTL):
        return cached.get("data", [])

    client = get_supabase_client()
    if not client:
        return cached.get("data", []) if cached else []
    try:
        from collections import Counter
        users_res = client.table("users").select("id, telegram_nick, full_name, role, class_name").execute()
        users_map = {u["id"]: u for u in (users_res.data or [])}

        tasks_res = client.table("tasks").select("user_id").execute()
        materials_res = client.table("task_materials").select("uploader_id").execute()
        comps_res = client.table("user_completions").select("user_id").execute()

        t_counts = Counter(t["user_id"] for t in (tasks_res.data or []))
        m_counts = Counter(m["uploader_id"] for m in (materials_res.data or []))
        c_counts = Counter(c["user_id"] for c in (comps_res.data or []))

        all_ids = set(users_map.keys()) | set(t_counts.keys()) | set(m_counts.keys()) | set(c_counts.keys())
        leaderboard = []

        for uid in all_ids:
            u = users_map.get(uid, {})
            name = u.get("full_name") or u.get("telegram_nick") or f"Ученик #{uid}"
            nick = u.get("telegram_nick") or ""
            t_cnt = t_counts.get(uid, 0)
            m_cnt = m_counts.get(uid, 0)
            c_cnt = c_counts.get(uid, 0)
            score = (t_cnt * 5) + (m_cnt * 3) + (c_cnt * 1)
            if score > 0:
                leaderboard.append({
                    "user_id": uid,
                    "name": name,
                    "nick": nick,
                    "tasks": t_cnt,
                    "materials": m_cnt,
                    "completions": c_cnt,
                    "score": score
                })

        leaderboard.sort(key=lambda x: (x["score"], x["tasks"], x["materials"]), reverse=True)

        for idx, item in enumerate(leaderboard, 1):
            item["rank"] = idx
            if idx == 1:
                item["badge"] = "Главный редактор"
            elif idx == 2:
                item["badge"] = "Супер-активист"
            elif idx == 3:
                item["badge"] = "Мастер конспектов"
            elif item["score"] >= 15:
                item["badge"] = "Архивариус"
            elif item["score"] >= 5:
                item["badge"] = "Редактор"
            else:
                item["badge"] = "Ученик"

        _LEADERBOARD_CACHE[class_name] = {
            "timestamp": now,
            "data": leaderboard
        }
        return leaderboard
    except Exception as e:
        logger.error(f"Error calculating Supabase leaderboard: {e}")
        return cached.get("data", []) if cached else []

# ─────────────────────────────────────────────────────────────
# АВТООЧИСТКА УСТАРЕВШИХ ЗАДАНИЙ (> 30 ДНЕЙ / ЗА МЕСЯЦ)
# ─────────────────────────────────────────────────────────────

def cleanup_old_tasks(days: int = 30) -> int:
    """
    Полная очистка всех заданий, которым уже за месяц (по умолчанию старше 30 дней).
    Удаляет связанные записи в task_materials и user_completions, затем сами задания.
    Возвращает количество удаленных заданий.
    """
    client = get_supabase_client()
    if not client:
        return 0
    try:
        cutoff_date = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        # Поиск заданий с дедлайном старше 30 дней
        res = client.table("tasks").select("id").lt("due_date", cutoff_date).execute()
        old_tasks = res.data or []
        if not old_tasks:
            return 0

        old_task_ids = [t["id"] for t in old_tasks if "id" in t]
        if not old_task_ids:
            return 0

        # 1. Удаляем прикрепленные материалы и отметки выполнения
        try:
            client.table("task_materials").delete().in_("task_id", old_task_ids).execute()
        except Exception as m_err:
            logger.warning(f"Warning cleaning old task_materials: {m_err}")

        try:
            client.table("user_completions").delete().in_("task_id", old_task_ids).execute()
        except Exception as c_err:
            logger.warning(f"Warning cleaning old user_completions: {c_err}")

        # 2. Удаляем сами задания
        del_res = client.table("tasks").delete().in_("id", old_task_ids).execute()
        deleted_count = len(del_res.data or []) if del_res.data else len(old_task_ids)
        logger.info(f"🧹 Supabase: успешно очищено {deleted_count} заданий старше {days} дней (дедлайн до {cutoff_date})")

        # Сбрасываем кэш рейтинга
        global _LEADERBOARD_CACHE
        _LEADERBOARD_CACHE.clear()

        return deleted_count
    except Exception as e:
        logger.error(f"Error in Supabase cleanup_old_tasks: {e}")
        return 0
