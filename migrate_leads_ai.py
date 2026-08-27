import sqlite3
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cursor = db.cursor()
cols = [r[1] for r in cursor.execute("PRAGMA table_info(leads)").fetchall()]
migrations = [
    ("hotness", "ALTER TABLE leads ADD COLUMN hotness VARCHAR(10) DEFAULT 'cold'"),
    ("ai_summary", "ALTER TABLE leads ADD COLUMN ai_summary TEXT"),
    ("next_action", "ALTER TABLE leads ADD COLUMN next_action VARCHAR(10)"),
    ("budget", "ALTER TABLE leads ADD COLUMN budget VARCHAR(20)"),
    ("timeline", "ALTER TABLE leads ADD COLUMN timeline VARCHAR(20)"),
    ("readiness", "ALTER TABLE leads ADD COLUMN readiness VARCHAR(20)"),
]
for name, sql in migrations:
    if name not in cols:
        cursor.execute(sql)
        print(f"Added: {name}")
    else:
        print(f"Exists: {name}")
db.commit()

# Update existing leads with hotness based on score
cursor.execute("""
    UPDATE leads SET hotness = CASE
        WHEN lead_score >= 90 THEN 'hot'
        WHEN lead_score >= 80 THEN 'warm'
        ELSE 'cold'
    END
    WHERE hotness IS NULL OR hotness = ''
""")
updated = cursor.rowcount
print(f"Updated {updated} existing leads with hotness")
db.commit()
db.close()
print("MIGRATION DONE")
