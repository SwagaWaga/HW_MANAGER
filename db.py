import sqlite3
DB_NAME = 'homework.db'


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    due_date TEXT NOT NULL,
    is_done INTEGER DEFAULT 0
)

    """)
    conn.commit()
    conn.close()

def add_task(user_id: int, subject: str, description: str, due_date: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = """
    INSERT INTO tasks (user_id, subject, description, due_date)
    VALUES (?, ?, ?, ?)
"""
    cursor.execute(sql, (user_id, subject, description, due_date))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id

def get_pending_tasks(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # lets us access columns like task["subject"]
    cursor = conn.cursor()
    
    sql = """
    SELECT id, subject, description, due_date
    FROM tasks
    WHERE user_id = ? AND is_done = 0
    ORDER BY due_date ASC
    """
    cursor.execute(sql, (user_id,))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def mark_task_done(task_id: int, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "UPDATE tasks SET is_done = 1 WHERE id = ? AND user_id = ?"
    cursor.execute(sql, (task_id, user_id))
    conn.commit()
    conn.close()

def delete_task(task_id: int, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = "DELETE FROM tasks WHERE id = ? AND user_id = ?"
    cursor.execute(sql, (task_id, user_id))
    conn.commit()
    conn.close()

def get_today_tasks(user_id: int, date_str: str):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # lets us access columns like task["subject"]
    cursor = conn.cursor()
    
    sql = """
        SELECT id, subject, description, due_date
        FROM tasks
        WHERE user_id = ? AND due_date = ? AND is_done = 0
    """
    cursor.execute(sql, (user_id, date_str))
    tasks = cursor.fetchall()
    conn.close()
    return tasks


if __name__ == "__main__":
    init_db()
    
    # 1. Add a test task
    add_task(12345, "Math", "Exercises 1-5 on page 45", "2026-09-24")
    
    # 2. View pending tasks
    tasks = get_pending_tasks(12345)
    print(f"Found {len(tasks)} pending task(s):")
    for t in tasks:
        print(f" - #{t['id']} [{t['subject']}]: {t['description']} (Due: {t['due_date']})")