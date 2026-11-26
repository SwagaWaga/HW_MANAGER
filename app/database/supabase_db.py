import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone
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
    try:
        from app.services.sync_service import sync_global_user
        sync_global_user(user_id, telegram_nick=telegram_nick, full_name=full_name, role=role, class_name=class_name, source="supabase")
    except Exception:
        pass

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

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return None
    try:
        res = client.table("users").select("id, telegram_nick, full_name, role, class_name").eq("id", user_id).limit(1).execute()
        return res.data[0] if (res.data and len(res.data) > 0) else None
    except Exception as e:
        logger.error(f"Error in Supabase get_user: {e}")
        return None

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

def get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return None
    try:
        # First attempt: with author relation
        res = client.table("tasks").select("*, users!tasks_user_id_fkey(telegram_nick, full_name, role)").eq("id", task_id).limit(1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            u_info = row.get("users") or {}
            row["author_nick"] = u_info.get("telegram_nick") or f"@ученик_{row.get('user_id')}"
            row["author_name"] = u_info.get("full_name") or ""
            row["role"] = u_info.get("role", "student")
            return row
    except Exception as e:
        logger.warning(f"Note: get_task_by_id with relation skipped: {e}")

    # Fallback: direct select without embedding
    try:
        res = client.table("tasks").select("*").eq("id", task_id).limit(1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            uid = row.get("user_id")
            row["author_nick"] = f"@ученик_{uid}"
            row["author_name"] = ""
            row["role"] = "student"
            if uid:
                try:
                    u_res = client.table("users").select("telegram_nick, full_name, role").eq("id", uid).limit(1).execute()
                    if u_res.data:
                        u = u_res.data[0]
                        row["author_nick"] = u.get("telegram_nick") or row["author_nick"]
                        row["author_name"] = u.get("full_name") or ""
                        row["role"] = u.get("role", "student")
                except Exception:
                    pass
            return row
    except Exception as e:
        logger.error(f"Error in Supabase get_task_by_id fallback: {e}")

    return None

def get_pending_tasks(user_id: int, class_name: str = "10-А") -> List[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return []

    def _fetch_completions():
        comp_res = client.table("user_completions").select("task_id").eq("user_id", user_id).execute()
        return {row["task_id"] for row in (comp_res.data or [])}

    def _fetch_tasks():
        try:
            res = client.table("tasks").select(
                "*, users!tasks_user_id_fkey(telegram_nick, full_name, role)"
            ).order("due_date", desc=False).execute()
            return res.data or []
        except Exception as e:
            logger.warning(f"Note: get_pending_tasks with relation skipped ({e}), falling back to direct select")
            res = client.table("tasks").select("*").order("due_date", desc=False).execute()
            return res.data or []

    try:
        # Выполняем оба независимых запроса параллельно (~2x быстрее)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_comp = pool.submit(_fetch_completions)
            f_tasks = pool.submit(_fetch_tasks)
            completed_ids = f_comp.result()
            res_data = f_tasks.result()

        tasks = []
        for row in res_data:
            if row["id"] in completed_ids:
                continue
            u_info = row.get("users") or {}
            author_nick = u_info.get("telegram_nick") or f"@ученик_{row.get('user_id')}"
            author_name = u_info.get("full_name") or ""

            task_dict = {
                "id": row["id"],
                "user_id": row.get("user_id"),
                "subject": row.get("subject"),
                "description": row.get("description"),
                "due_date": str(row.get("due_date")),
                "last_edited_by": row.get("last_edited_by"),
                "last_edited_by_name": row.get("last_edited_by_name"),
                "last_edited_at": row.get("last_edited_at"),
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

def update_task(task_id: int, subject: str, description: str, due_date: str, user_id: Optional[int] = None, editor_name: Optional[str] = None) -> tuple[bool, str]:
    client = get_supabase_client()
    if not client:
        return False, "Database client unavailable"

    task = get_task_by_id(task_id)
    if not task:
        return False, "Задание не найдено"

    # Проверка авторства: только автор может изменять задание
    task_author = task.get("user_id")
    if user_id and task_author and int(task_author) != int(user_id):
        author_nick = task.get("author_nick") or f"ID {task_author}"
        return False, f"Изменять задание может только автор, который его опубликовал ({author_nick})"

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    clean_editor_name = editor_name or task.get("author_name") or task.get("author_nick") or "Редактор"

    try:
        payload = {
            "subject": subject,
            "description": description,
            "due_date": due_date,
            "last_edited_by": user_id,
            "last_edited_by_name": clean_editor_name,
            "last_edited_at": now_str
        }
        try:
            client.table("tasks").update(payload).eq("id", task_id).execute()
        except Exception:
            # Fallback if audit columns aren't in Supabase schema cache yet
            client.table("tasks").update({
                "subject": subject,
                "description": description,
                "due_date": due_date
            }).eq("id", task_id).execute()
        return True, "Задание успешно обновлено"
    except Exception as e:
        logger.error(f"Error in Supabase update_task: {e}")
        return False, str(e)

def delete_task(task_id: int, user_id: Optional[int] = None) -> tuple[bool, str]:
    client = get_supabase_client()
    if not client:
        return False, "Database client unavailable"

    task = get_task_by_id(task_id)
    if not task:
        return True, "Задание уже удалено"

    # Проверка авторства: только автор может удалять задание
    task_author = task.get("user_id")
    if user_id and task_author and int(task_author) != int(user_id):
        author_nick = task.get("author_nick") or f"ID {task_author}"
        return False, f"Удалять задание может только автор, который его опубликовал ({author_nick})"

    try:
        # Сначала очищаем связанные записи
        try:
            client.table("task_materials").delete().eq("task_id", task_id).execute()
            client.table("user_completions").delete().eq("task_id", task_id).execute()
        except Exception as sub_err:
            logger.warning(f"Cleanup sub-tables warning: {sub_err}")

        client.table("tasks").delete().eq("id", task_id).execute()
        return True, "Задание удалено"
    except Exception as e:
        logger.error(f"Error in Supabase delete_task: {e}")
        return False, str(e)

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

def get_materials_and_comments_batch(task_ids: List[int]) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[int, int]]:
    """Одним запросом получает материалы и счётчики комментариев для списка заданий."""
    if not task_ids:
        return {}, {}
    client = get_supabase_client()
    if not client:
        return {tid: [] for tid in task_ids}, {tid: 0 for tid in task_ids}

    try:
        # Быстрый пакетный запрос БЕЗ огромного base64 file_id!
        res = client.table("task_materials").select(
            "id, task_id, caption, uploader_id, users(telegram_nick, full_name)"
        ).in_("task_id", task_ids).order("id").execute()

        materials: Dict[int, List[Dict[str, Any]]] = {tid: [] for tid in task_ids}
        comment_counts: Dict[int, int] = {tid: 0 for tid in task_ids}

        for row in (res.data or []):
            t_id = row["task_id"]
            caption = row.get("caption") or ""

            if caption.startswith("[COMMENT]"):
                comment_counts[t_id] = comment_counts.get(t_id, 0) + 1
                continue

            u_info = row.get("users") or {}
            mat_id = row["id"]

            if caption.startswith("[LINK]"):
                raw = caption[6:]
                if "::" in raw:
                    link_url, link_title = raw.split("::", 1)
                else:
                    link_url, link_title = raw, ""
                link_url = link_url.strip()
                link_title = link_title.strip() or link_url
                mat_item = {
                    "id": mat_id,
                    "task_id": t_id,
                    "type": "link",
                    "url": link_url,
                    "title": link_title,
                    "caption": link_title,
                    "file_id": link_url,
                    "uploader_id": row["uploader_id"],
                    "uploader_nick": u_info.get("telegram_nick") or f"@ученик_{row['uploader_id']}",
                    "uploader_name": u_info.get("full_name") or ""
                }
            else:
                mat_item = {
                    "id": mat_id,
                    "task_id": t_id,
                    "type": "image",
                    "file_id": f"/api/materials/{mat_id}",
                    "url": f"/api/materials/{mat_id}",
                    "caption": caption,
                    "uploader_id": row["uploader_id"],
                    "uploader_nick": u_info.get("telegram_nick") or f"@ученик_{row['uploader_id']}",
                    "uploader_name": u_info.get("full_name") or ""
                }

            materials.setdefault(t_id, []).append(mat_item)
        return materials, comment_counts
    except Exception as e:
        logger.error(f"Error in Supabase get_materials_and_comments_batch: {e}")
        return {tid: [] for tid in task_ids}, {tid: 0 for tid in task_ids}

def get_materials_for_tasks_batch(task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    materials, _ = get_materials_and_comments_batch(task_ids)
    return materials

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

def normalize_time_str(val: Any) -> Optional[str]:
    """Приводит строку времени к стандартному формату HH:MM."""
    if val is None:
        return None
    s = str(val).strip().replace(".", ":")
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
    elif s.isdigit():
        h = int(s)
        if 0 <= h <= 23:
            return f"{h:02d}:00"
    return None

def parse_custom_times(custom_time_raw: Any) -> List[str]:
    """Парсит строку или список произвольных времён в отсортированный уникальный список ['07:00', '19:30']."""
    if not custom_time_raw:
        return []
    if isinstance(custom_time_raw, list):
        raw_items = custom_time_raw
    else:
        raw_items = str(custom_time_raw).split(",")
    res: List[str] = []
    for item in raw_items:
        norm = normalize_time_str(item)
        if norm and norm not in res:
            res.append(norm)
    return sorted(res)

_DEFAULT_PREFS = {
    "notify_08": True,
    "notify_16": True,
    "notify_18": True,
    "notify_weekly": True,
    "notify_today": True,
    "notify_tomorrow": True,
    "notify_2days": True,
    "notify_overdue": True,
    "notify_custom": True,
    "custom_time": "",
    "custom_times": [],
}

def get_notification_prefs(user_id: int) -> Optional[Dict[str, Any]]:
    """Return notification preferences for a user from Supabase. Returns None if table/connection fails."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        res = client.table("user_notification_prefs").select("*").eq("user_id", user_id).limit(1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            raw_custom = str(row.get("custom_time", "") or "")
            c_times = parse_custom_times(raw_custom)
            return {
                "notify_08": bool(row.get("notify_08", True)),
                "notify_16": bool(row.get("notify_16", True)),
                "notify_18": bool(row.get("notify_18", True)),
                "notify_weekly": bool(row.get("notify_weekly", True)),
                "notify_today": bool(row.get("notify_today", True)),
                "notify_tomorrow": bool(row.get("notify_tomorrow", True)),
                "notify_2days": bool(row.get("notify_2days", True)),
                "notify_overdue": bool(row.get("notify_overdue", True)),
                "notify_custom": bool(row.get("notify_custom", False)) if "notify_custom" in row else bool(c_times),
                "custom_time": ", ".join(c_times),
                "custom_times": c_times,
            }
        return _DEFAULT_PREFS.copy()
    except Exception as e:
        logger.warning(f"Supabase user_notification_prefs unavailable ({e}), using SQLite fallback")
        return None

def upsert_notification_prefs(
    user_id: int,
    notify_08: bool = True,
    notify_16: bool = True,
    notify_18: bool = True,
    notify_2days: bool = True,
    notify_weekly: bool = True,
    notify_today: bool = True,
    notify_tomorrow: bool = True,
    notify_overdue: bool = True,
    notify_custom: bool = True,
    custom_time: Any = "",
    custom_times: Optional[List[str]] = None
) -> bool:
    """Save/update notification preferences for a user in Supabase."""
    client = get_supabase_client()
    if not client:
        return False

    if custom_times is not None:
        c_times = parse_custom_times(custom_times)
    else:
        c_times = parse_custom_times(custom_time)
    formatted_custom = ", ".join(c_times)
    has_custom = notify_custom if c_times else False

    # 1. Попытка записать со всеми новыми полями включая custom_time
    try:
        client.table("user_notification_prefs").upsert({
            "user_id": user_id,
            "notify_08": notify_08,
            "notify_16": notify_16,
            "notify_18": notify_18,
            "notify_2days": notify_2days,
            "notify_weekly": notify_weekly,
            "notify_today": notify_today,
            "notify_tomorrow": notify_tomorrow,
            "notify_overdue": notify_overdue,
            "notify_custom": has_custom,
            "custom_time": formatted_custom,
        }, on_conflict="user_id").execute()
        return True
    except Exception:
        # 2. Фолбэк без custom_time
        try:
            client.table("user_notification_prefs").upsert({
                "user_id": user_id,
                "notify_08": notify_08,
                "notify_16": notify_16,
                "notify_18": notify_18,
                "notify_2days": notify_2days,
                "notify_weekly": notify_weekly,
                "notify_today": notify_today,
                "notify_tomorrow": notify_tomorrow,
                "notify_overdue": notify_overdue,
            }, on_conflict="user_id").execute()
            return True
        except Exception:
            # 3. Фолбэк на базовую схему
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
            except Exception:
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
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_users = pool.submit(lambda: client.table("users").select("id, telegram_nick, full_name, role, class_name").execute())
            f_tasks = pool.submit(lambda: client.table("tasks").select("user_id").execute())
            f_mats = pool.submit(lambda: client.table("task_materials").select("uploader_id").execute())
            f_comps = pool.submit(lambda: client.table("user_completions").select("user_id").execute())
            users_res = f_users.result()
            tasks_res = f_tasks.result()
            materials_res = f_mats.result()
            comps_res = f_comps.result()
        users_map = {u["id"]: u for u in (users_res.data or [])}

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

# ─────────────────────────────────────────────────────────────
# ГРУППОВЫЕ ЧАТЫ (GROUP CHATS) И ПОИСК ЗАДАНИЙ ДЛЯ КЛАССА
# ─────────────────────────────────────────────────────────────

def upsert_group_chat(chat_id: int, title: str = "", chat_type: str = "group", added_by: Optional[int] = None, is_active: int = 1, remind_time: str = "18:00", class_name: str = "10-А") -> bool:
    """Сохраняет или обновляет информацию о групповом чате класса."""
    client = get_supabase_client()
    if not client:
        return False
    try:
        data = {
            "chat_id": chat_id,
            "title": title or "Групповой чат",
            "chat_type": chat_type,
            "class_name": class_name,
            "is_active": is_active,
            "remind_time": remind_time,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        if added_by is not None:
            data["added_by"] = added_by
        client.table("group_chats").upsert(data, on_conflict="chat_id").execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase upsert_group_chat: {e}")
        return False

def get_group_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает информацию о групповом чате."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        res = client.table("group_chats").select("*").eq("chat_id", chat_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        return None
    except Exception as e:
        logger.error(f"Error in Supabase get_group_chat: {e}")
        return None

def get_active_group_chats() -> List[Dict[str, Any]]:
    """Возвращает список всех активных групповых чатов для отправки напоминаний."""
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = client.table("group_chats").select("*").eq("is_active", 1).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Error in Supabase get_active_group_chats: {e}")
        return []

def set_group_reminder(chat_id: int, is_active: bool, remind_time: Optional[str] = None) -> bool:
    """Включает/выключает напоминания в группе или меняет время отправки."""
    client = get_supabase_client()
    if not client:
        return False
    try:
        data = {
            "is_active": 1 if is_active else 0,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        if remind_time:
            data["remind_time"] = remind_time
        client.table("group_chats").update(data).eq("chat_id", chat_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase set_group_reminder: {e}")
        return False

def get_all_subjects(class_name: str = "10-А") -> List[str]:
    """Возвращает список всех уникальных предметов из базы данных."""
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = client.table("tasks").select("subject").or_(f"class_name.eq.{class_name},class_name.is.null").execute()
        rows = res.data or []
        subjects = sorted(list({r["subject"].strip() for r in rows if r.get("subject")}))
        return subjects
    except Exception as e:
        logger.error(f"Error in Supabase get_all_subjects: {e}")
        return []

def get_class_tasks_for_date(
    date_str: str,
    class_name: str = "10-А",
    subject: Union[str, List[str], None] = None
) -> List[Dict[str, Any]]:
    """Возвращает все домашние задания класса на конкретную дату с прикрепленными материалами."""
    client = get_supabase_client()
    if not client:
        return []
    try:
        query = client.table("tasks").select(
            "*, users!tasks_user_id_fkey(telegram_nick, full_name, role)"
        ).eq("due_date", date_str).or_(f"class_name.eq.{class_name},class_name.is.null")

        res = query.order("subject").order("id").execute()
        rows = res.data or []

        # Фильтрация по предмету (поддерживает строку или список родственных предметов)
        if subject:
            if isinstance(subject, str):
                target_subjs = [subject.strip().lower()]
            else:
                target_subjs = [str(s).strip().lower() for s in subject if s and str(s).strip()]

            filtered_rows = []
            for r in rows:
                r_subj = (r.get("subject") or "").strip().lower()
                r_desc = (r.get("description") or "").strip().lower()
                if any(ts in r_subj or (len(ts) >= 4 and ts in r_desc) for ts in target_subjs):
                    filtered_rows.append(r)
            rows = filtered_rows

        tasks = []
        for row in rows:
            u_info = row.get("users") or {}
            author_nick = u_info.get("telegram_nick") or f"@ученик_{row.get('user_id')}"
            author_name = u_info.get("full_name") or ""
            tasks.append({
                "id": row["id"],
                "user_id": row.get("user_id"),
                "subject": row.get("subject"),
                "description": row.get("description"),
                "due_date": str(row.get("due_date")),
                "last_edited_by": row.get("last_edited_by"),
                "last_edited_by_name": row.get("last_edited_by_name"),
                "last_edited_at": row.get("last_edited_at"),
                "author_nick": author_nick,
                "author_name": author_name,
                "role": u_info.get("role", "student"),
                "class_name": row.get("class_name", class_name)
            })
        task_ids = [t["id"] for t in tasks]
        materials_batch = get_materials_for_tasks_batch(task_ids)
        for t in tasks:
            t["materials"] = materials_batch.get(t["id"], [])
        return tasks
    except Exception as e:
        logger.error(f"Error in Supabase get_class_tasks_for_date: {e}")
        return []

def get_upcoming_tasks_for_subject(
    subject_query: Union[str, List[str]],
    start_date_str: str,
    class_name: str = "10-А",
    limit: int = 5
) -> List[Dict[str, Any]]:
    """Возвращает ближайшие задания по предмету (или списку предметов) начиная со start_date_str."""
    client = get_supabase_client()
    if not client:
        return []
    try:
        query = client.table("tasks").select(
            "*, users!tasks_user_id_fkey(telegram_nick, full_name, role)"
        ).gte("due_date", start_date_str).or_(f"class_name.eq.{class_name},class_name.is.null").order("due_date").order("id")

        res = query.execute()
        rows = res.data or []

        if isinstance(subject_query, str):
            target_subjs = [subject_query.strip().lower()]
        else:
            target_subjs = [str(s).strip().lower() for s in subject_query if s and str(s).strip()]

        filtered_rows = []
        for r in rows:
            r_subj = (r.get("subject") or "").strip().lower()
            r_desc = (r.get("description") or "").strip().lower()
            if any(ts in r_subj or (len(ts) >= 4 and ts in r_desc) for ts in target_subjs):
                filtered_rows.append(r)

        rows = filtered_rows[:limit]
        tasks = []
        for row in rows:
            u_info = row.get("users") or {}
            author_nick = u_info.get("telegram_nick") or f"@ученик_{row.get('user_id')}"
            author_name = u_info.get("full_name") or ""
            tasks.append({
                "id": row["id"],
                "user_id": row.get("user_id"),
                "subject": row.get("subject"),
                "description": row.get("description"),
                "due_date": str(row.get("due_date")),
                "last_edited_by": row.get("last_edited_by"),
                "last_edited_by_name": row.get("last_edited_by_name"),
                "last_edited_at": row.get("last_edited_at"),
                "author_nick": author_nick,
                "author_name": author_name,
                "role": u_info.get("role", "student"),
                "class_name": row.get("class_name", class_name)
            })
        task_ids = [t["id"] for t in tasks]
        materials_batch = get_materials_for_tasks_batch(task_ids)
        for t in tasks:
            t["materials"] = materials_batch.get(t["id"], [])
        return tasks
    except Exception as e:
        logger.error(f"Error in Supabase get_upcoming_tasks_for_subject: {e}")
        return []

# ─────────────────────────────────────────────────────────────
# КОММЕНТАРИИ К ДОМАШНИМ ЗАДАНИЯМ (TASK COMMENTS)
# ─────────────────────────────────────────────────────────────

def add_task_comment(task_id: int, user_id: int, text: str, author_name: Optional[str] = None, author_nick: Optional[str] = None) -> Optional[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat()
        payload = {
            "text": text.strip(),
            "author_name": (author_name or "").strip(),
            "author_nick": (author_nick or "").strip(),
            "created_at": now_iso
        }
        caption = f"[COMMENT]{json.dumps(payload, ensure_ascii=False)}"
        row = {
            "task_id": task_id,
            "file_id": "comment",
            "caption": caption,
            "uploader_id": user_id
        }
        res = client.table("task_materials").insert(row).execute()
        if res.data and len(res.data) > 0:
            inserted = res.data[0]
            c_id = inserted["id"]
            return {
                "id": c_id,
                "task_id": task_id,
                "user_id": user_id,
                "text": text.strip(),
                "author_name": author_name or "",
                "author_nick": author_nick or f"@{user_id}",
                "author_role": "student",
                "created_at": now_iso
            }
        return None
    except Exception as e:
        logger.error(f"Error in Supabase add_task_comment: {e}")
        return None

def get_task_comments(task_id: int) -> List[Dict[str, Any]]:
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = client.table("task_materials").select(
            "id, task_id, caption, uploader_id, created_at, users(telegram_nick, full_name, role)"
        ).eq("task_id", task_id).ilike("caption", "[COMMENT]%").order("id").execute()

        comments = []
        for r in (res.data or []):
            caption = r.get("caption") or ""
            raw_json = caption[9:]  # remove "[COMMENT]"
            try:
                data = json.loads(raw_json)
            except Exception:
                data = {"text": raw_json}

            u_info = r.get("users") or {}
            c_nick = data.get("author_nick") or u_info.get("telegram_nick") or f"@ученик_{r['uploader_id']}"
            c_name = data.get("author_name") or u_info.get("full_name") or "Ученик"
            c_role = u_info.get("role") or "student"
            c_time = data.get("created_at") or r.get("created_at") or ""

            comments.append({
                "id": r["id"],
                "task_id": r["task_id"],
                "user_id": r["uploader_id"],
                "text": data.get("text", ""),
                "author_name": c_name,
                "author_nick": c_nick,
                "author_role": c_role,
                "created_at": c_time
            })
        return comments
    except Exception as e:
        logger.error(f"Error in Supabase get_task_comments: {e}")
        return []

def get_comments_count_batch(task_ids: List[int]) -> Dict[int, int]:
    if not task_ids:
        return {}
    client = get_supabase_client()
    if not client:
        return {tid: 0 for tid in task_ids}
    try:
        res = client.table("task_materials").select(
            "id, task_id, caption"
        ).in_("task_id", task_ids).ilike("caption", "[COMMENT]%").execute()

        counts = {tid: 0 for tid in task_ids}
        for r in (res.data or []):
            t_id = r.get("task_id")
            if t_id in counts:
                counts[t_id] += 1
        return counts
    except Exception as e:
        logger.error(f"Error in Supabase get_comments_count_batch: {e}")
        return {tid: 0 for tid in task_ids}

def delete_task_comment(comment_id: Any, user_id: int = 0) -> bool:
    client = get_supabase_client()
    if not client:
        return False
    try:
        if isinstance(comment_id, dict):
            comment_id = comment_id.get("id", 0)
        comment_id = int(comment_id)
        if user_id > 0:
            user_res = client.table("users").select("role").eq("id", user_id).limit(1).execute()
            is_admin = bool(user_res.data and user_res.data[0].get("role") == "admin")
            if not is_admin:
                chk = client.table("task_materials").select("uploader_id").eq("id", comment_id).limit(1).execute()
                if not chk.data or chk.data[0].get("uploader_id") != user_id:
                    logger.warning(f"Unauthorized comment deletion attempt by user {user_id}")
                    return False
        client.table("task_materials").delete().eq("id", comment_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error in Supabase delete_task_comment: {e}")
        return False

