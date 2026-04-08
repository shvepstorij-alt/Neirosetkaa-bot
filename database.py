import sqlite3
import os
from datetime import datetime

DB_PATH = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Пользователи
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

    # Транзакции кредитов
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        amount INTEGER,
        description TEXT,
        created_at TEXT
    )''')

    # История использования генераций
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
        return {
            "user_id": row[0], "username": row[1], "first_name": row[2],
            "credits": row[3], "plan": row[4], "plan_expires": row[5],
            "joined_at": row[6], "total_spent": row[7]
        }
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
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE plan != 'free'")
    paid_users = c.fetchone()[0]
    c.execute("SELECT SUM(credits_spent) FROM usage_log")
    total_credits = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM usage_log WHERE action LIKE 'image%'")
    total_images = c.fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
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
