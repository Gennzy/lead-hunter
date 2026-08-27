import sqlite3
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cursor = db.cursor()
cols = [r[1] for r in cursor.execute("PRAGMA table_info(tenants)").fetchall()]
migrations = [
    ("plan", "ALTER TABLE tenants ADD COLUMN plan VARCHAR(20) DEFAULT 'free'"),
    ("max_users", "ALTER TABLE tenants ADD COLUMN max_users INTEGER DEFAULT 3"),
    ("max_leads_per_month", "ALTER TABLE tenants ADD COLUMN max_leads_per_month INTEGER DEFAULT 100"),
    ("max_chats", "ALTER TABLE tenants ADD COLUMN max_chats INTEGER DEFAULT 5"),
    ("trial_ends_at", "ALTER TABLE tenants ADD COLUMN trial_ends_at TIMESTAMP"),
    ("subscription_ends_at", "ALTER TABLE tenants ADD COLUMN subscription_ends_at TIMESTAMP"),
    ("stripe_customer_id", "ALTER TABLE tenants ADD COLUMN stripe_customer_id VARCHAR(255)"),
    ("stripe_subscription_id", "ALTER TABLE tenants ADD COLUMN stripe_subscription_id VARCHAR(255)"),
]
for name, sql in migrations:
    if name not in cols:
        cursor.execute(sql)
        print(f"Added: {name}")
    else:
        print(f"Exists: {name}")
db.commit()
db.close()
print("MIGRATION DONE")
