import sqlite3
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cur = db.cursor()
cur.execute("SELECT id, plan, config FROM tenants")
rows = cur.fetchall()
for r in rows:
    print(f"Tenant {r[0]}: plan={r[1]}, config={r[2]}")
db.close()
