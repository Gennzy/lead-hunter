import sqlite3
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cursor = db.cursor()
cols = [r[1] for r in cursor.execute("PRAGMA table_info(leads)").fetchall()]
if "city" not in cols:
    cursor.execute("ALTER TABLE leads ADD COLUMN city VARCHAR(100)")
    print("Added: city")
else:
    print("Exists: city")
db.commit()
db.close()
print("MIGRATION DONE")
