"""
Database migration script.
Run after git pull to add missing columns and tables.
Usage: python migrate_db.py
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "lead_hunter.db"

COLUMN_MIGRATIONS = [
    ("leads", "deal_amount", "REAL"),
    ("leads", "deal_currency", "TEXT"),
    ("leads", "deal_closed_at", "DATETIME"),
    ("leads", "last_responded_at", "DATETIME"),
    ("users", "max_leads", "INTEGER"),
    ("users", "commission_rate", "REAL"),
    ("users", "weight", "REAL"),
    ("users", "telegram_id", "INTEGER"),
]

TABLE_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS employee_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        period TEXT NOT NULL,
        target_leads INTEGER DEFAULT 0,
        target_deals INTEGER DEFAULT 0,
        target_revenue REAL DEFAULT 0.0,
        actual_leads INTEGER DEFAULT 0,
        actual_deals INTEGER DEFAULT 0,
        actual_revenue REAL DEFAULT 0.0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS ix_employee_targets_user_id ON employee_targets(user_id)""",
    """CREATE INDEX IF NOT EXISTS ix_employee_targets_period ON employee_targets(user_id, period)""",
    """CREATE TABLE IF NOT EXISTS commissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        lead_id INTEGER,
        period TEXT NOT NULL,
        deal_amount REAL DEFAULT 0.0,
        commission_rate REAL DEFAULT 0.0,
        commission_amount REAL DEFAULT 0.0,
        bonus_amount REAL DEFAULT 0.0,
        is_paid INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS ix_commissions_user_id ON commissions(user_id)""",
    """CREATE INDEX IF NOT EXISTS ix_commissions_period ON commissions(user_id, period)""",
    """CREATE TABLE IF NOT EXISTS penalties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        amount REAL DEFAULT 0.0,
        description TEXT,
        lead_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS ix_penalties_user_id ON penalties(user_id)""",
    """CREATE TABLE IF NOT EXISTS work_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        login_at DATETIME,
        logout_at DATETIME,
        total_seconds INTEGER DEFAULT 0,
        actions_count INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS ix_work_sessions_user_id ON work_sessions(user_id)""",
    """CREATE INDEX IF NOT EXISTS ix_work_sessions_period ON work_sessions(user_id, date)""",
    """CREATE TABLE IF NOT EXISTS response_time_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user_id INTEGER,
        lead_id INTEGER NOT NULL,
        assigned_at DATETIME NOT NULL,
        first_response_at DATETIME,
        response_seconds INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS ix_response_time_logs_user_id ON response_time_logs(user_id)""",
    """CREATE INDEX IF NOT EXISTS ix_response_time_logs_lead_id ON response_time_logs(lead_id)""",
]


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    # Add columns
    for table, column, col_type in COLUMN_MIGRATIONS:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"Added column: {table}.{column} ({col_type})")
        else:
            print(f"Exists: {table}.{column}")

    # Create tables
    for sql in TABLE_MIGRATIONS:
        try:
            c.execute(sql)
            if sql.strip().startswith("CREATE TABLE"):
                print(f"Created table: {sql.split('IF NOT EXISTS')[1].split('(')[0].strip()}")
        except sqlite3.OperationalError as e:
            if "already exists" in str(e):
                pass
            else:
                print(f"Error: {e}")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
