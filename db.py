import sqlite3
import logging
from typing import Optional, List, Dict, Any
import supabase_db

logger = logging.getLogger(__name__)

DB_NAME = 'homework.db'

def is_using_supabase() -> bool:
    return supabase_db.is_supabase_configured()

def init_db():
    # Always ensure local SQLite schema exists for seamless fallback
    conn = sqlite3.connect(DB_NAME)
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
    CREATE TABLE IF NOT EXISTS user_notification_prefs (
        user_id INTEGER PRIMARY KEY,
        notify_08 INTEGER DEFAULT 1,
        notify_16 INTEGER DEFAULT 1,
        notify_18 INTEGER DEFAULT 1,
        notify_2days INTEGER DEFAULT 1,
        notify_weekly INTEGER DEFAULT 1
    )
    """)
    for col in ("notify_2days", "notify_weekly"):
        try:
            cursor.execute(f"ALTER TABLE user_notification_prefs ADD COLUMN {col} INTEGER DEFAULT 1")
        except Exception:
            pass
    conn.commit()
    conn.close()

    if is_using_supabase():
        logger.info("⚡ Using Supabase as primary database backend")
    else:
        logger.info("📁 Using local SQLite database (homework.db)")

def upsert_user(user_id: int, telegram_nick: Optional[str] = None, full_name: Optional[str] = None, role: str = 'student', class_name: str = '10-А'):
    if is_using_supabase():
        return supabase_db.upsert_user(user_id, telegram_nick, full_name, role, class_name)
    
    conn = sqlite3.connect(DB_NAME)
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

def get_all_users(class_name: str = '10-А') -> List[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_all_users(class_name)

    conn = sqlite3.connect(DB_NAME)
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

    conn = sqlite3.connect(DB_NAME)
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

def get_pending_tasks(user_id: int, class_name: str = '10-А') -> List[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_pending_tasks(user_id, class_name)

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    sql = """
    SELECT t.id, t.user_id, t.subject, t.description, t.due_date,
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

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "INSERT OR IGNORE INTO user_completions (user_id, task_id) VALUES (?, ?)"
    cursor.execute(sql, (user_id, task_id))
    conn.commit()
    conn.close()

def update_task(task_id: int, subject: str, description: str, due_date: str, user_id: Optional[int] = None) -> bool:
    if is_using_supabase():
        return supabase_db.update_task(task_id, subject, description, due_date, user_id)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET subject = ?, description = ?, due_date = ? WHERE id = ?", (subject, description, due_date, task_id))
    conn.commit()
    conn.close()
    return True

def delete_task(task_id: int, user_id: Optional[int] = None):
    if is_using_supabase():
        return supabase_db.delete_task(task_id, user_id)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_materials WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM user_completions WHERE task_id = ?", (task_id,))
    if user_id:
        cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    else:
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

def delete_material(material_id: int) -> bool:
    if is_using_supabase():
        return supabase_db.delete_material(material_id)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_materials WHERE id = ?", (material_id,))
    conn.commit()
    conn.close()
    return True

def delete_all_tasks(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "DELETE FROM tasks WHERE user_id = ?"
    cursor.execute(sql, (user_id,))
    conn.commit()
    conn.close()

def get_today_tasks(user_id: int, date_str: str) -> List[Dict[str, Any]]:
    tasks = get_pending_tasks(user_id)
    return [t for t in tasks if str(t.get('due_date')) == str(date_str)]

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

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    created_ids = []
    for item in items:
        cursor.execute("INSERT OR IGNORE INTO task_materials (task_id, file_id, caption, uploader_id) VALUES (?, ?, ?, ?)",
                       (task_id, item["file_id"], item.get("caption", ""), uploader_id))
        created_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()
    return created_ids

def get_materials_for_task(task_id: int) -> List[Dict[str, Any]]:
    batch = get_materials_for_tasks_batch([task_id])
    return batch.get(task_id, [])

def get_materials_for_tasks_batch(task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not task_ids:
        return {}
    if is_using_supabase():
        return supabase_db.get_materials_for_tasks_batch(task_ids)

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in task_ids)
    sql = f"""
        SELECT m.id, m.task_id, m.caption, m.uploader_id,
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
        mat_id = r["id"]
        item = {
            "id": mat_id,
            "task_id": t_id,
            "file_id": f"/api/materials/{mat_id}",
            "url": f"/api/materials/{mat_id}",
            "caption": r["caption"] or "",
            "uploader_id": r["uploader_id"],
            "uploader_nick": r["uploader_nick"],
            "uploader_name": r["uploader_name"]
        }
        if t_id in result:
            result[t_id].append(item)
        else:
            result[t_id] = [item]
    return result

def get_material_by_id(material_id: int) -> Optional[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_material_by_id(material_id)

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, task_id, file_id, caption, uploader_id FROM task_materials WHERE id = ?", (material_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# ─────────────────────────────────────────────────────────────
# USER NOTIFICATION PREFERENCES (SQLite fallback)
# ─────────────────────────────────────────────────────────────

_DEFAULT_PREFS = {
    "notify_08": True,
    "notify_16": True,
    "notify_18": True,
    "notify_2days": True,
    "notify_weekly": True
}

def get_notification_prefs(user_id: int) -> Dict[str, bool]:
    if is_using_supabase():
        prefs = supabase_db.get_notification_prefs(user_id)
        if prefs is not None:
            return prefs
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT notify_08, notify_16, notify_18, notify_2days, notify_weekly FROM user_notification_prefs WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "notify_08": bool(row["notify_08"]),
                "notify_16": bool(row["notify_16"]),
                "notify_18": bool(row["notify_18"]),
                "notify_2days": bool(row["notify_2days"]) if "notify_2days" in row.keys() else True,
                "notify_weekly": bool(row["notify_weekly"]) if "notify_weekly" in row.keys() else True,
            }
    except Exception:
        try:
            cursor.execute("SELECT notify_08, notify_16, notify_18 FROM user_notification_prefs WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "notify_08": bool(row["notify_08"]),
                    "notify_16": bool(row["notify_16"]),
                    "notify_18": bool(row["notify_18"]),
                    "notify_2days": True,
                    "notify_weekly": True,
                }
        except Exception:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    return _DEFAULT_PREFS.copy()

def upsert_notification_prefs(user_id: int, notify_08: bool, notify_16: bool, notify_18: bool, notify_2days: bool = True, notify_weekly: bool = True) -> bool:
    saved = False
    if is_using_supabase():
        saved = supabase_db.upsert_notification_prefs(user_id, notify_08, notify_16, notify_18, notify_2days, notify_weekly)
    if not saved:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO user_notification_prefs (user_id, notify_08, notify_16, notify_18, notify_2days, notify_weekly)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notify_08 = excluded.notify_08,
                    notify_16 = excluded.notify_16,
                    notify_18 = excluded.notify_18,
                    notify_2days = excluded.notify_2days,
                    notify_weekly = excluded.notify_weekly
            """, (user_id, int(notify_08), int(notify_16), int(notify_18), int(notify_2days), int(notify_weekly)))
            conn.commit()
        except Exception:
            cursor.execute("""
                INSERT INTO user_notification_prefs (user_id, notify_08, notify_16, notify_18)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notify_08 = excluded.notify_08,
                    notify_16 = excluded.notify_16,
                    notify_18 = excluded.notify_18
            """, (user_id, int(notify_08), int(notify_16), int(notify_18)))
            conn.commit()
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
        conn = sqlite3.connect(DB_NAME)
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
        conn = sqlite3.connect(DB_NAME)
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

init_db()
