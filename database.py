import sqlite3
import os
import datetime as dt
from datetime import datetime

DB_PATH = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        credits INTEGER DEFAULT 0,
        plan TEXT DEFAULT 'free',
        plan_expires TEXT,
        joined_at TEXT,
        total_spent REAL DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        amount INTEGER,
        description TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        credits_spent INTEGER,
        created_at TEXT
    )''')
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"user_id": row[0], "username": row[1], "first_name": row[2],
                "credits": row[3], "plan": row[4], "plan_expires": row[5],
                "joined_at": row[6], "total_spent": row[7]}
    return None

def create_user(user_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO users
        (user_id, username, first_name, credits, plan, joined_at)
        VALUES (?, ?, ?, 0, 'free', ?)''',
        (user_id, username, first_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def add_credits(user_id: int, amount: int, description: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    c.execute('''INSERT INTO transactions (user_id, type, amount, description, created_at)
        VALUES (?, 'add', ?, ?, ?)''',
        (user_id, amount, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def spend_credits(user_id: int, amount: int, action: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row or row[0] < amount:
        conn.close()
        return False
    c.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (amount, user_id))
    c.execute('''INSERT INTO usage_log (user_id, action, credits_spent, created_at)
        VALUES (?, ?, ?, ?)''', (user_id, action, amount, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True

def set_plan(user_id: int, plan: str, expires: str, credits_to_add: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''UPDATE users SET plan = ?, plan_expires = ?, credits = credits + ?
        WHERE user_id = ?''', (plan, expires, credits_to_add, user_id))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d")

    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{today}%",))
    new_today = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM users WHERE joined_at >= ?", (week_ago,))
    new_week = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM users WHERE plan != 'free'")
    paid_users = c.fetchone()[0]

    c.execute("SELECT SUM(credits_spent) FROM usage_log")
    total_credits = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM usage_log WHERE action LIKE 'image%'")
    total_images = c.fetchone()[0]

    conn.close()
    return {
        "total_users": total_users,
        "new_today": new_today,
        "new_week": new_week,
        "paid_users": paid_users,
        "total_credits_spent": total_credits,
        "total_images": total_images
    }

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, credits, plan, joined_at FROM users ORDER BY joined_at DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_user_ids():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def log_question(user_id: int, service_keyword: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO usage_log (user_id, action, credits_spent, created_at)
        VALUES (?, ?, 0, ?)''', (user_id, f"question_{service_keyword}", datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_popular_services(limit: int = 5):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT action, COUNT(*) as cnt FROM usage_log
        WHERE action LIKE 'question_%'
        GROUP BY action ORDER BY cnt DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [(r[0].replace("question_", ""), r[1]) for r in rows]

def block_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET plan = 'blocked' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unblock_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET plan = 'free' WHERE user_id = ? AND plan = 'blocked'", (user_id,))
    conn.commit()
    conn.close()

def is_blocked(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT plan FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None and row[0] == 'blocked'

def find_user_by_username(username: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    username = username.lstrip('@')
    c.execute("SELECT user_id, username, first_name, credits, plan, joined_at FROM users WHERE LOWER(username) = LOWER(?)", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"user_id": row[0], "username": row[1], "first_name": row[2],
                "credits": row[3], "plan": row[4], "joined_at": row[5]}
    return None

def get_today_activity():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) FROM usage_log WHERE created_at LIKE ?", (f"{today}%",))
    messages_today = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT user_id) FROM usage_log WHERE created_at LIKE ?", (f"{today}%",))
    active_users_today = c.fetchone()[0]
    conn.close()
    return {"messages_today": messages_today, "active_users_today": active_users_today}

def get_top_active_users(limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.username, u.first_name, COUNT(l.id) as cnt
        FROM usage_log l JOIN users u ON l.user_id = u.user_id
        GROUP BY l.user_id ORDER BY cnt DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_credits_history(limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT t.user_id, u.username, u.first_name, t.amount, t.description, t.created_at
        FROM transactions t LEFT JOIN users u ON t.user_id = u.user_id
        WHERE t.type = 'add'
        ORDER BY t.created_at DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_credits_spent_by_user(limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.username, u.first_name, SUM(l.credits_spent) as total
        FROM usage_log l JOIN users u ON l.user_id = u.user_id
        WHERE l.credits_spent > 0
        GROUP BY l.user_id ORDER BY total DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_maintenance_mode():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("SELECT value FROM settings WHERE key = 'maintenance'")
    row = c.fetchone()
    conn.close()
    return row is not None and row[0] == '1'

def set_maintenance_mode(enabled: bool):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('maintenance', ?)", ('1' if enabled else '0',))
    conn.commit()
    conn.close()

def get_welcome_message():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("SELECT value FROM settings WHERE key = 'welcome_msg'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_welcome_message(text: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('welcome_msg', ?)", (text,))
    conn.commit()
    conn.close()

def log_message(user_id: int):
    """Логируем каждое сообщение для статистики активности"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO usage_log (user_id, action, credits_spent, created_at) VALUES (?, 'message', 0, ?)",
              (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
