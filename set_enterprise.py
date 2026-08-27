import sqlite3
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cursor = db.cursor()
cursor.execute("UPDATE tenants SET plan = 'enterprise', max_users = 999, max_chats = 999, max_leads_per_month = 999999 WHERE id = 1")
print(f"Updated {cursor.rowcount} tenant(s) to enterprise")
db.commit()
db.close()
print("DONE")
