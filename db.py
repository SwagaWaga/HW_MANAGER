import sqlite3
import logging
from typing import Optional, List, Dict, Any
import supabase_db

logger = logging.getLogger(__name__)

DB_NAME = 'homework.db'

def is_using_supabase() -> bool:
    return supabase_db.is_supabase_configured()

def init_db():
    # If Supabase is active, ensure connection works
    if is_using_supabase():
        logger.info("⚡ Using Supabase as primary database backend")
        return

    # Fallback to local SQLite
    logger.info("📁 Using local SQLite database (homework.db)")
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
    conn.commit()
    conn.close()

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

def delete_task(task_id: int, user_id: int):
    if is_using_supabase():
        return supabase_db.delete_task(task_id, user_id)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "DELETE FROM tasks WHERE id = ? AND user_id = ?"
    cursor.execute(sql, (task_id, user_id))
    conn.commit()
    conn.close()

def delete_all_tasks(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "DELETE FROM tasks WHERE user_id = ?"
    cursor.execute(sql, (user_id,))
    conn.commit()
    conn.close()

def get_today_tasks(user_id: int, date_str: str):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    sql = """
        SELECT id, subject, description, due_date
        FROM tasks
        WHERE due_date = ? AND id NOT IN (SELECT task_id FROM user_completions WHERE user_id = ?)
    """
    cursor.execute(sql, (date_str, user_id))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def get_overdue_tasks(user_id: int, today_str: str):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    sql = """
        SELECT id, subject, description, due_date
        FROM tasks
        WHERE due_date < ? AND id NOT IN (SELECT task_id FROM user_completions WHERE user_id = ?)
        ORDER BY due_date ASC
    """
    cursor.execute(sql, (today_str, user_id))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def add_material(task_id: int, file_id: str, caption: str, uploader_id: int):
    if is_using_supabase():
        return supabase_db.add_material(task_id, file_id, caption, uploader_id)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "INSERT OR IGNORE INTO task_materials (task_id, file_id, caption, uploader_id) VALUES (?, ?, ?, ?)"
    cursor.execute(sql, (task_id, file_id, caption, uploader_id))
    conn.commit()
    conn.close()

def get_materials_for_task(task_id: int) -> List[Dict[str, Any]]:
    if is_using_supabase():
        return supabase_db.get_materials_for_task(task_id)

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    sql = """
        SELECT m.id, m.file_id, m.caption, m.uploader_id,
               COALESCE(u.telegram_nick, '@ученик_' || m.uploader_id) as uploader_nick,
               COALESCE(u.full_name, '') as uploader_name
        FROM task_materials m
        LEFT JOIN users u ON m.uploader_id = u.id
        WHERE m.task_id = ?
    """
    cursor.execute(sql, (task_id,))
    rows = cursor.fetchall()
    materials = [dict(r) for r in rows]
    conn.close()
    return materials

init_db()
