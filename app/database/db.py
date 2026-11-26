import sqlite3
import logging
from typing import Optional, List, Dict, Any, Tuple

try:
    from app.database import supabase_db
except ImportError:
    import supabase_db

logger = logging.getLogger(__name__)

DB_NAME = 'homework.db'

def is_using_supabase() -> bool:
    return supabase_db.is_supabase_configured()

def init_db():
    # Always ensure local SQLite schema exists for seamless fallback
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        telegram_nick TEXT,
        full_name TEXT,
        role TEXT DEFAULT 'student',
        class_name TEXT DEFAULT '10-А',
        is_verified BOOLEAN DEFAULT 1
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        description TEXT NOT NULL,
        due_date TEXT NOT NULL,
        class_name TEXT DEFAULT '10-А'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_completions (
        user_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, task_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS task_materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        caption TEXT,
        uploader_id INTEGER NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        author_name TEXT,
        author_nick TEXT,
        text TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_notification_prefs (
        user_id INTEGER PRIMARY KEY,
        notify_08 INTEGER DEFAULT 1,
        notify_16 INTEGER DEFAULT 1,
        notify_18 INTEGER DEFAULT 1,
        notify_2days INTEGER DEFAULT 1,
        notify_weekly INTEGER DEFAULT 1,
        notify_today INTEGER DEFAULT 1,
        notify_tomorrow INTEGER DEFAULT 1,
        notify_overdue INTEGER DEFAULT 1,
        notify_custom INTEGER DEFAULT 0,
        custom_time TEXT DEFAULT '19:00'
    )
    """)
    for col, col_def in (
        ("notify_2days", "INTEGER DEFAULT 1"),
        ("notify_weekly", "INTEGER DEFAULT 1"),
        ("notify_today", "INTEGER DEFAULT 1"),
        ("notify_tomorrow", "INTEGER DEFAULT 1"),
        ("notify_overdue", "INTEGER DEFAULT 1"),
        ("notify_custom", "INTEGER DEFAULT 0"),
        ("custom_time", "TEXT DEFAULT '19:00'")
    ):
        try:
            cursor.execute(f"ALTER TABLE user_notification_prefs ADD COLUMN {col} {col_def}")
        except Exception:
            pass

    for col, col_def in (
        ("last_edited_by", "INTEGER"),
        ("last_edited_by_name", "TEXT"),
        ("last_edited_at", "TEXT")
    ):
        try:
            cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_def}")
        except Exception:
            pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS group_chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        chat_type TEXT,
        class_name TEXT DEFAULT '10-А',
        is_active INTEGER DEFAULT 1,
        remind_time TEXT DEFAULT '18:00',
        added_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    for col, col_def in (
        ("class_name", "TEXT DEFAULT '10-А'"),
        ("is_active", "INTEGER DEFAULT 1"),
        ("remind_time", "TEXT DEFAULT '18:00'"),
        ("added_by", "INTEGER")
    ):
        try:
            cursor.execute(f"ALTER TABLE group_chats ADD COLUMN {col} {col_def}")
        except Exception:
            pass

    conn.commit()
    conn.close()

    if is_using_supabase():
        logger.info("⚡ Using Supabase as primary database backend")
    else:
        logger.info("📁 Using local SQLite database (homework.db)")

def upsert_user(user_id: int, telegram_nick: Optional[str] = None, full_name: Optional[str] = None, role: str = 'student', class_name: str = '10-А'):
    try:
        from app.services.sync_service import sync_global_user
        sync_global_user(user_id, telegram_nick=telegram_nick, full_name=full_name, role=role, class_name=class_name, source="sqlite")
    except Exception:
        pass

    if is_using_supabase():
        return supabase_db.upsert_user(user_id, telegram_nick, full_name, role, class_name)
    
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO users (id, telegram_nick, full_name, role, class_name)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        telegram_nick = COALESCE(excluded.telegram_nick, users.telegram_nick),
        full_name = COALESCE(excluded.full_name, users.full_name),
        role = excluded.role,
        class_name = excluded.class_name
    """, (user_id, telegram_nick, full_name, role, class_name))
    conn.commit()
    conn.close()

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_user(user_id)
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, telegram_nick, full_name, role, class_name FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_users(class_name: str = '10-А') -> List[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_all_users(class_name)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, telegram_nick, full_name, role, class_name FROM users WHERE class_name = ? OR class_name IS NULL", (class_name,))
    rows = cursor.fetchall()
    users = [dict(r) for r in rows]
    conn.close()
    return users

def add_task(user_id: int, subject: str, description: str, due_date: str, class_name: str = '10-А', telegram_nick: Optional[str] = None, full_name: Optional[str] = None) -> int:
    if is_using_supabase():
        res = supabase_db.add_task(user_id, subject, description, due_date, class_name, telegram_nick, full_name)
        if res is not None:
            return res

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    if telegram_nick or full_name:
        upsert_user(user_id, telegram_nick=telegram_nick, full_name=full_name, class_name=class_name)

    sql = """
    INSERT INTO tasks (user_id, subject, description, due_date, class_name)
    VALUES (?, ?, ?, ?, ?)
    """
    cursor.execute(sql, (user_id, subject, description, due_date, class_name))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id

def get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_task_by_id(task_id)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    sql = """
    SELECT t.*,
           COALESCE(u.telegram_nick, '@ученик_' || t.user_id) as author_nick,
           COALESCE(u.full_name, '') as author_name,
           COALESCE(u.role, 'student') as role
    FROM tasks t
    LEFT JOIN users u ON t.user_id = u.id
    WHERE t.id = ?
    """
    cursor.execute(sql, (task_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_pending_tasks(user_id: int, class_name: str = '10-А') -> List[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_pending_tasks(user_id, class_name)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    sql = """
    SELECT t.id, t.user_id, t.subject, t.description, t.due_date,
           t.last_edited_by, t.last_edited_by_name, t.last_edited_at,
           COALESCE(u.telegram_nick, '@ученик_' || t.user_id) as author_nick,
           COALESCE(u.full_name, '') as author_name,
           COALESCE(u.role, 'student') as role
    FROM tasks t
    LEFT JOIN users u ON t.user_id = u.id
    WHERE t.id NOT IN (SELECT task_id FROM user_completions WHERE user_id = ?)
    ORDER BY t.due_date ASC
    """
    cursor.execute(sql, (user_id,))
    rows = cursor.fetchall()
    tasks = [dict(r) for r in rows]
    conn.close()
    return tasks

def mark_task_done(task_id: int, user_id: int):
    if is_using_supabase():
        return supabase_db.mark_task_done(task_id, user_id)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    sql = "INSERT OR IGNORE INTO user_completions (user_id, task_id) VALUES (?, ?)"
    cursor.execute(sql, (user_id, task_id))
    conn.commit()
    conn.close()

def update_task(task_id: int, subject: str, description: str, due_date: str, user_id: Optional[int] = None, editor_name: Optional[str] = None) -> tuple[bool, str]:
    if is_using_supabase():
        return supabase_db.update_task(task_id, subject, description, due_date, user_id, editor_name)

    task = get_task_by_id(task_id)
    if not task:
        return False, "Задание не найдено"

    # Проверка прав: изменять может только автор задания или админ
    task_author = task.get("user_id")
    if user_id and task_author and int(task_author) != int(user_id):
        author_nick = task.get("author_nick") or f"ID {task_author}"
        return False, f"Изменять задание может только автор, который его опубликовал ({author_nick})"

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    clean_editor_name = editor_name or task.get("author_name") or task.get("author_nick") or "Редактор"

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE tasks
        SET subject = ?, description = ?, due_date = ?,
            last_edited_by = ?, last_edited_by_name = ?, last_edited_at = ?
        WHERE id = ?
    """, (subject, description, due_date, user_id, clean_editor_name, now_str, task_id))
    conn.commit()
    conn.close()
    return True, "Задание успешно обновлено"

def delete_task(task_id: int, user_id: Optional[int] = None) -> tuple[bool, str]:
    if is_using_supabase():
        return supabase_db.delete_task(task_id, user_id)

    task = get_task_by_id(task_id)
    if not task:
        return True, "Задание уже удалено"

    # Проверка прав: удалять может только автор задания
    task_author = task.get("user_id")
    if user_id and task_author and int(task_author) != int(user_id):
        author_nick = task.get("author_nick") or f"ID {task_author}"
        return False, f"Удалять задание может только автор, который его опубликовал ({author_nick})"

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_materials WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM user_completions WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return True, "Задание удалено"

def delete_material(material_id: int) -> bool:
    if is_using_supabase():
        return supabase_db.delete_material(material_id)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_materials WHERE id = ?", (material_id,))
    conn.commit()
    conn.close()
    return True

def delete_all_tasks(user_id: int):
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    sql = "DELETE FROM tasks WHERE user_id = ?"
    cursor.execute(sql, (user_id,))
    conn.commit()
    conn.close()

def get_today_tasks(user_id: int, date_str: str) -> List[Dict[str, Any]]:
    tasks = [t for t in get_pending_tasks(user_id) if str(t.get('due_date')) == str(date_str)]
    task_ids = [t['id'] for t in tasks if 'id' in t]
    if task_ids:
        materials_batch = get_materials_for_tasks_batch(task_ids)
        for t in tasks:
            t['materials'] = materials_batch.get(t['id'], [])
    return tasks

def get_overdue_tasks(user_id: int, today_str: str) -> List[Dict[str, Any]]:
    tasks = get_pending_tasks(user_id)
    return [t for t in tasks if str(t.get('due_date')) < str(today_str)]

def add_material(task_id: int, file_id: str, caption: str, uploader_id: int):
    return add_materials_batch(task_id, [{"file_id": file_id, "caption": caption}], uploader_id)

def add_materials_batch(task_id: int, items: List[Dict[str, str]], uploader_id: int):
    if not items:
        return []
    if is_using_supabase():
        return supabase_db.add_materials_batch(task_id, items, uploader_id)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    cursor = conn.cursor()
    created_ids = []
    for item in items:
        cursor.execute("INSERT OR IGNORE INTO task_materials (task_id, file_id, caption, uploader_id) VALUES (?, ?, ?, ?)",
                       (task_id, item["file_id"], item.get("caption", ""), uploader_id))
        created_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()
    return created_ids

def get_materials_and_comments_batch(task_ids: List[int]) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[int, int]]:
    """Возвращает материалы и счётчики комментариев для списка заданий за минимальное число запросов."""
    if not task_ids:
        return {}, {}
    if is_using_supabase():
        return supabase_db.get_materials_and_comments_batch(task_ids)
    return get_materials_for_tasks_batch(task_ids), get_comments_count_batch(task_ids)

def get_materials_for_task(task_id: int) -> List[Dict[str, Any]]:
    batch = get_materials_for_tasks_batch([task_id])
    return batch.get(task_id, [])

def get_materials_for_tasks_batch(task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not task_ids:
        return {}
    if is_using_supabase():
        return supabase_db.get_materials_for_tasks_batch(task_ids)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in task_ids)
    sql = f"""
        SELECT m.id, m.task_id, m.file_id, m.caption, m.uploader_id,
               COALESCE(u.telegram_nick, '@ученик_' || m.uploader_id) as uploader_nick,
               COALESCE(u.full_name, '') as uploader_name
        FROM task_materials m
        LEFT JOIN users u ON m.uploader_id = u.id
        WHERE m.task_id IN ({placeholders})
        ORDER BY m.id ASC
    """
    cursor.execute(sql, task_ids)
    rows = cursor.fetchall()
    conn.close()

    result: Dict[int, List[Dict[str, Any]]] = {tid: [] for tid in task_ids}
    for r in rows:
        t_id = r["task_id"]
        caption = r["caption"] or ""
        if caption.startswith("[COMMENT]"):
            continue
        item = supabase_db.build_material_item(
            mat_id=r["id"],
            task_id=t_id,
            caption=caption,
            uploader_id=r["uploader_id"],
            uploader_nick=r["uploader_nick"],
            uploader_name=r["uploader_name"],
            file_id=r["file_id"] or "",
        )
        result.setdefault(t_id, []).append(item)
    return result

def get_material_by_id(material_id: int) -> Optional[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_material_by_id(material_id)

    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, task_id, file_id, caption, uploader_id FROM task_materials WHERE id = ?", (material_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# ─────────────────────────────────────────────────────────────
# USER NOTIFICATION PREFERENCES (SQLite fallback)
# ─────────────────────────────────────────────────────────────

def normalize_time_str(val: Any) -> Optional[str]:
    """Приводит строку времени к стандартному формату HH:MM (например: '7' -> '07:00', '18:30' -> '18:30')."""
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

def get_notification_prefs(user_id: int) -> Dict[str, Any]:
    if is_using_supabase():
        prefs = supabase_db.get_notification_prefs(user_id)
        if prefs is not None:
            return prefs
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM user_notification_prefs WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            keys = row.keys()
            raw_custom = str(row["custom_time"] or "") if "custom_time" in keys else ""
            c_times = parse_custom_times(raw_custom)
            return {
                "notify_08": bool(row["notify_08"]) if "notify_08" in keys else True,
                "notify_16": bool(row["notify_16"]) if "notify_16" in keys else True,
                "notify_18": bool(row["notify_18"]) if "notify_18" in keys else True,
                "notify_weekly": bool(row["notify_weekly"]) if "notify_weekly" in keys else True,
                "notify_today": bool(row["notify_today"]) if "notify_today" in keys else True,
                "notify_tomorrow": bool(row["notify_tomorrow"]) if "notify_tomorrow" in keys else True,
                "notify_2days": bool(row["notify_2days"]) if "notify_2days" in keys else True,
                "notify_overdue": bool(row["notify_overdue"]) if "notify_overdue" in keys else True,
                "notify_custom": bool(row["notify_custom"]) if "notify_custom" in keys else bool(c_times),
                "custom_time": ", ".join(c_times),
                "custom_times": c_times,
            }
    except Exception:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return _DEFAULT_PREFS.copy()

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
    if custom_times is not None:
        c_times = parse_custom_times(custom_times)
    else:
        c_times = parse_custom_times(custom_time)
    formatted_custom = ", ".join(c_times)
    has_custom = notify_custom if c_times else False

    saved = False
    if is_using_supabase():
        saved = supabase_db.upsert_notification_prefs(
            user_id, notify_08, notify_16, notify_18, notify_2days, notify_weekly,
            notify_today, notify_tomorrow, notify_overdue, has_custom, formatted_custom, c_times
        )
    if not saved:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO user_notification_prefs (
                    user_id, notify_08, notify_16, notify_18, notify_2days, notify_weekly,
                    notify_today, notify_tomorrow, notify_overdue, notify_custom, custom_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notify_08 = excluded.notify_08,
                    notify_16 = excluded.notify_16,
                    notify_18 = excluded.notify_18,
                    notify_2days = excluded.notify_2days,
                    notify_weekly = excluded.notify_weekly,
                    notify_today = excluded.notify_today,
                    notify_tomorrow = excluded.notify_tomorrow,
                    notify_overdue = excluded.notify_overdue,
                    notify_custom = excluded.notify_custom,
                    custom_time = excluded.custom_time
            """, (
                user_id,
                int(notify_08), int(notify_16), int(notify_18),
                int(notify_2days), int(notify_weekly),
                int(notify_today), int(notify_tomorrow), int(notify_overdue),
                int(has_custom), str(formatted_custom)
            ))
            conn.commit()
        except Exception:
            try:
                cursor.execute("""
                    INSERT INTO user_notification_prefs (
                        user_id, notify_08, notify_16, notify_18, notify_2days, notify_weekly,
                        notify_today, notify_tomorrow, notify_overdue
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        notify_08 = excluded.notify_08,
                        notify_16 = excluded.notify_16,
                        notify_18 = excluded.notify_18,
                        notify_2days = excluded.notify_2days,
                        notify_weekly = excluded.notify_weekly,
                        notify_today = excluded.notify_today,
                        notify_tomorrow = excluded.notify_tomorrow,
                        notify_overdue = excluded.notify_overdue
                """, (
                    user_id,
                    int(notify_08), int(notify_16), int(notify_18),
                    int(notify_2days), int(notify_weekly),
                    int(notify_today), int(notify_tomorrow), int(notify_overdue)
                ))
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
    return True

# ─────────────────────────────────────────────────────────────
# LEADERBOARD / RATING (КЭШИРОВАНИЕ НА 4 ЧАСА)
# ─────────────────────────────────────────────────────────────

import time
import datetime
import os

_LEADERBOARD_CACHE: Dict[str, Dict[str, Any]] = {}
LEADERBOARD_CACHE_TTL = 4 * 3600  # 4 часа (14400 секунд)

def get_leaderboard(class_name: str = "10-А", force_refresh: bool = False) -> List[Dict[str, Any]]:
    if is_using_supabase():
        res = supabase_db.get_leaderboard(class_name, force_refresh=force_refresh)
        if res is not None:
            return res

    now = time.time()
    cached = _LEADERBOARD_CACHE.get(class_name)
    if not force_refresh and cached and (now - cached.get("timestamp", 0) < LEADERBOARD_CACHE_TTL):
        return cached.get("data", [])

    try:
        from collections import Counter
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, telegram_nick, full_name, role, class_name FROM users WHERE class_name = ?", (class_name,))
        users_map = {row["id"]: dict(row) for row in cursor.fetchall()}

        cursor.execute("SELECT user_id FROM tasks WHERE class_name = ?", (class_name,))
        t_counts = Counter(row["user_id"] for row in cursor.fetchall())

        cursor.execute("SELECT uploader_id FROM task_materials WHERE uploader_id IS NOT NULL")
        m_counts = Counter(row["uploader_id"] for row in cursor.fetchall())

        cursor.execute("SELECT user_id FROM user_completions")
        c_counts = Counter(row["user_id"] for row in cursor.fetchall())
        conn.close()

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
        logger.error(f"Error calculating SQLite leaderboard: {e}")
        return cached.get("data", []) if cached else []

# ─────────────────────────────────────────────────────────────
# АВТООЧИСТКА УСТАРЕВШИХ ЗАДАНИЙ (> 30 ДНЕЙ / ЗА МЕСЯЦ)
# ─────────────────────────────────────────────────────────────

def cleanup_old_tasks(days: int = 30) -> int:
    """
    Полная очистка всех заданий, которым уже за месяц (по умолчанию старше 30 дней).
    Удаляет дочерние записи (материалы, отметки выполнения), сами задания и файлы на диске.
    Поддерживает как Supabase, так и локальный SQLite.
    Возвращает количество удаленных заданий.
    """
    deleted_count = 0
    if is_using_supabase():
        deleted_count = supabase_db.cleanup_old_tasks(days=days)

    try:
        cutoff_date = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tasks WHERE due_date < ?", (cutoff_date,))
        old_ids = [row[0] for row in cursor.fetchall()]
        if old_ids:
            placeholders = ",".join("?" * len(old_ids))
            cursor.execute(f"DELETE FROM task_materials WHERE task_id IN ({placeholders})", old_ids)
            cursor.execute(f"DELETE FROM user_completions WHERE task_id IN ({placeholders})", old_ids)
            cursor.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", old_ids)
            conn.commit()
            if not is_using_supabase():
                deleted_count = len(old_ids)
            logger.info(f"🧹 SQLite: очищено {len(old_ids)} заданий старше {days} дней (дедлайн до {cutoff_date})")
        conn.close()

        # Очищаем связанные файлы фото из uploads/
        if old_ids and os.path.exists("uploads"):
            prefixes = tuple(f"{tid}_" for tid in old_ids)
            for fname in os.listdir("uploads"):
                if fname.startswith(prefixes):
                    try:
                        os.remove(os.path.join("uploads", fname))
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"Error in SQLite cleanup_old_tasks: {e}")

    global _LEADERBOARD_CACHE
    _LEADERBOARD_CACHE.clear()
    return deleted_count

# ─────────────────────────────────────────────────────────────
# ГРУППОВЫЕ ЧАТЫ (GROUP CHATS) И ПОИСК ЗАДАНИЙ ДЛЯ КЛАССА
# ─────────────────────────────────────────────────────────────

def upsert_group_chat(chat_id: int, title: str = "", chat_type: str = "group", added_by: Optional[int] = None, is_active: int = 1, remind_time: str = "18:00", class_name: str = "10-А") -> bool:
    """Сохраняет или обновляет информацию о групповом чате класса."""
    if is_using_supabase():
        res = supabase_db.upsert_group_chat(chat_id, title, chat_type, added_by, is_active, remind_time, class_name)
        if res:
            return True

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        sql = """
        INSERT INTO group_chats (chat_id, title, chat_type, class_name, is_active, remind_time, added_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = excluded.title,
            chat_type = excluded.chat_type,
            class_name = excluded.class_name,
            is_active = excluded.is_active,
            updated_at = CURRENT_TIMESTAMP
        """
        cursor.execute(sql, (chat_id, title or "Групповой чат", chat_type, class_name, is_active, remind_time, added_by))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error in upsert_group_chat: {e}")
        return False

def get_group_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает информацию о групповом чате."""
    if is_using_supabase():
        res = supabase_db.get_group_chat(chat_id)
        if res is not None:
            return res

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM group_chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error in get_group_chat: {e}")
        return None

def get_active_group_chats() -> List[Dict[str, Any]]:
    """Возвращает список всех активных групповых чатов для отправки напоминаний."""
    if is_using_supabase():
        res = supabase_db.get_active_group_chats()
        if res:
            return res

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM group_chats WHERE is_active = 1")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error in get_active_group_chats: {e}")
        return []

def set_group_reminder(chat_id: int, is_active: bool, remind_time: Optional[str] = None) -> bool:
    """Включает/выключает напоминания в группе или меняет время отправки."""
    if is_using_supabase():
        res = supabase_db.set_group_reminder(chat_id, is_active, remind_time)
        if res:
            return True

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        if remind_time:
            norm_time = normalize_time_str(remind_time) or remind_time
            cursor.execute("UPDATE group_chats SET is_active = ?, remind_time = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
                           (1 if is_active else 0, norm_time, chat_id))
        else:
            cursor.execute("UPDATE group_chats SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
                           (1 if is_active else 0, chat_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error in set_group_reminder: {e}")
        return False

def get_all_subjects(class_name: str = "10-А") -> List[str]:
    """Возвращает список всех уникальных предметов из базы данных."""
    if is_using_supabase():
        res = supabase_db.get_all_subjects(class_name)
        if res:
            return res

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT subject FROM tasks WHERE class_name = ? OR class_name IS NULL ORDER BY subject ASC", (class_name,))
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.error(f"Error in get_all_subjects: {e}")
        return []

def get_class_tasks_for_date(
    date_str: str,
    class_name: str = "10-А",
    subject: Union[str, List[str], None] = None
) -> List[Dict[str, Any]]:
    """Возвращает все домашние задания класса на конкретную дату с прикрепленными материалами."""
    if is_using_supabase():
        res = supabase_db.get_class_tasks_for_date(date_str, class_name, subject)
        if res is not None:
            return res

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        sql = """
        SELECT t.*,
               COALESCE(u.telegram_nick, '@ученик_' || t.user_id) as author_nick,
               COALESCE(u.full_name, '') as author_name,
               COALESCE(u.role, 'student') as role
        FROM tasks t
        LEFT JOIN users u ON t.user_id = u.id
        WHERE t.due_date = ? AND (t.class_name = ? OR t.class_name IS NULL)
        ORDER BY t.subject ASC, t.id ASC
        """
        cursor.execute(sql, (date_str, class_name))
        rows = cursor.fetchall()
        tasks = [dict(r) for r in rows]
        conn.close()

        if subject:
            if isinstance(subject, str):
                target_subjs = [subject.strip().lower()]
            else:
                target_subjs = [str(s).strip().lower() for s in subject if s and str(s).strip()]
            tasks = [
                t for t in tasks
                if any(ts in (t.get("subject") or "").lower() or (len(ts) >= 4 and ts in (t.get("description") or "").lower()) for ts in target_subjs)
            ]

        task_ids = [t["id"] for t in tasks]
        materials_batch = get_materials_for_tasks_batch(task_ids)
        for t in tasks:
            t["materials"] = materials_batch.get(t["id"], [])
        return tasks
    except Exception as e:
        logger.error(f"Error in get_class_tasks_for_date: {e}")
        return []

def get_upcoming_tasks_for_subject(
    subject_query: Union[str, List[str]],
    start_date_str: str,
    class_name: str = "10-А",
    limit: int = 5
) -> List[Dict[str, Any]]:
    """Возвращает ближайшие задания по предмету начиная со start_date_str."""
    if is_using_supabase():
        res = supabase_db.get_upcoming_tasks_for_subject(subject_query, start_date_str, class_name, limit)
        if res is not None:
            return res

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        sql = """
        SELECT t.*,
               COALESCE(u.telegram_nick, '@ученик_' || t.user_id) as author_nick,
               COALESCE(u.full_name, '') as author_name,
               COALESCE(u.role, 'student') as role
        FROM tasks t
        LEFT JOIN users u ON t.user_id = u.id
        WHERE t.due_date >= ? AND (t.class_name = ? OR t.class_name IS NULL)
        ORDER BY t.due_date ASC, t.id ASC
        """
        cursor.execute(sql, (start_date_str, class_name))
        rows = cursor.fetchall()
        tasks = [dict(r) for r in rows]
        conn.close()

        if subject_query:
            if isinstance(subject_query, str):
                target_subjs = [subject_query.strip().lower()]
            else:
                target_subjs = [str(s).strip().lower() for s in subject_query if s and str(s).strip()]
            tasks = [
                t for t in tasks
                if any(ts in (t.get("subject") or "").lower() or (len(ts) >= 4 and ts in (t.get("description") or "").lower()) for ts in target_subjs)
            ]

        tasks = tasks[:limit]
        task_ids = [t["id"] for t in tasks]
        materials_batch = get_materials_for_tasks_batch(task_ids)
        for t in tasks:
            t["materials"] = materials_batch.get(t["id"], [])
        return tasks
    except Exception as e:
        logger.error(f"Error in get_upcoming_tasks_for_subject: {e}")
        return []

# ─────────────────────────────────────────────────────────────
# КОММЕНТАРИИ К ДОМАШНИМ ЗАДАНИЯМ (TASK COMMENTS)
# ─────────────────────────────────────────────────────────────

def add_task_comment(task_id: int, user_id: int, text: str, author_name: Optional[str] = None, author_nick: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Добавляет новый комментарий к заданию."""
    res_supa = None
    if is_using_supabase():
        res_supa = supabase_db.add_task_comment(task_id, user_id, text, author_name, author_nick)

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO task_comments (task_id, user_id, author_name, author_nick, text) VALUES (?, ?, ?, ?, ?)",
            (task_id, user_id, author_name or "", author_nick or "", text.strip())
        )
        local_id = cursor.lastrowid
        conn.commit()
        conn.close()

        if res_supa:
            return res_supa

        return {
            "id": local_id,
            "task_id": task_id,
            "user_id": user_id,
            "text": text.strip(),
            "author_name": author_name or "",
            "author_nick": author_nick or f"@{user_id}",
            "author_role": "student",
            "created_at": ""
        }
    except Exception as e:
        logger.error(f"Error in SQLite add_task_comment: {e}")
        return res_supa

def get_task_comments(task_id: int) -> List[Dict[str, Any]]:
    """Возвращает все комментарии к заданию."""
    if is_using_supabase():
        supa_comments = supabase_db.get_task_comments(task_id)
        if supa_comments is not None and len(supa_comments) > 0:
            return supa_comments

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        sql = """
            SELECT c.id, c.task_id, c.user_id, c.text, c.created_at,
                   COALESCE(NULLIF(c.author_name, ''), u.full_name, 'Ученик') as author_name,
                   COALESCE(NULLIF(c.author_nick, ''), u.telegram_nick, '@ученик_' || c.user_id) as author_nick,
                   COALESCE(u.role, 'student') as author_role
            FROM task_comments c
            LEFT JOIN users u ON c.user_id = u.id
            WHERE c.task_id = ?
            ORDER BY c.id ASC
        """
        cursor.execute(sql, (task_id,))
        rows = cursor.fetchall()
        comments = [dict(r) for r in rows]
        conn.close()
        return comments
    except Exception as e:
        logger.error(f"Error in SQLite get_task_comments: {e}")
        return []

def get_comments_count_batch(task_ids: List[int]) -> Dict[int, int]:
    """Возвращает количество комментариев для списка заданий {task_id: count}."""
    if not task_ids:
        return {}
    if is_using_supabase():
        return supabase_db.get_comments_count_batch(task_ids)

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in task_ids)
        sql = f"""
            SELECT task_id, COUNT(*) as cnt
            FROM task_comments
            WHERE task_id IN ({placeholders})
            GROUP BY task_id
        """
        cursor.execute(sql, task_ids)
        rows = cursor.fetchall()
        counts = {tid: 0 for tid in task_ids}
        for tid, cnt in rows:
            counts[tid] = cnt
        conn.close()
        return counts
    except Exception as e:
        logger.error(f"Error in SQLite get_comments_count_batch: {e}")
        return {tid: 0 for tid in task_ids}

def delete_task_comment(comment_id: Any, user_id: int = 0) -> bool:
    """Удаляет комментарий."""
    if isinstance(comment_id, dict):
        comment_id = comment_id.get("id", 0)
    try:
        comment_id = int(comment_id)
    except (ValueError, TypeError):
        return False

    if is_using_supabase():
        res = supabase_db.delete_task_comment(comment_id, user_id)
        if res:
            try:
                conn = sqlite3.connect(DB_NAME, timeout=15.0)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM task_comments WHERE id = ?", (comment_id,))
                conn.commit()
                conn.close()
            except Exception:
                pass
            return True

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15.0)
        cursor = conn.cursor()
        if user_id > 0:
            cursor.execute("SELECT role FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            is_admin = bool(u_row and u_row[0] == "admin")
            if not is_admin:
                cursor.execute("SELECT user_id FROM task_comments WHERE id = ?", (comment_id,))
                c_row = cursor.fetchone()
                if not c_row or c_row[0] != user_id:
                    conn.close()
                    return False
        cursor.execute("DELETE FROM task_comments WHERE id = ?", (comment_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error in SQLite delete_task_comment: {e}")
        return False

init_db()
