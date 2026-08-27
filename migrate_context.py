import sqlite3
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cursor = db.cursor()

# Create user_message_history table
cursor.execute("""
CREATE TABLE IF NOT EXISTS user_message_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username VARCHAR(255),
    first_name VARCHAR(255),
    chat_title VARCHAR(512) NOT NULL,
    message_id INTEGER,
    text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
)
""")
print("Created user_message_history table")

# Create indexes
cursor.execute("CREATE INDEX IF NOT EXISTS ix_umh_tenant_user_chat ON user_message_history(tenant_id, user_id, chat_title)")
cursor.execute("CREATE INDEX IF NOT EXISTS ix_umh_tenant_chat ON user_message_history(tenant_id, chat_title)")
cursor.execute("CREATE INDEX IF NOT EXISTS ix_umh_user_id ON user_message_history(user_id)")
print("Created indexes")

db.commit()
db.close()
print("MIGRATION DONE")
