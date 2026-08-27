import sqlite3, json
db = sqlite3.connect("/home/leadhunter/lead-hunter/lead_hunter.db")
cur = db.cursor()

# Check current state
cur.execute("SELECT id, plan, config FROM tenants")
rows = cur.fetchall()
for r in rows:
    print(f"Tenant {r[0]}: plan={r[1]}, config_type={type(r[2]).__name__}")
    if r[2]:
        try:
            cfg = json.loads(r[2]) if isinstance(r[2], str) else r[2]
            print(f"  config.plan = {cfg.get('plan', 'NOT SET')}")
        except:
            print(f"  config raw = {r[2][:200]}")

# Fix: set plan in both column AND config JSON
cur.execute("UPDATE tenants SET plan = 'enterprise' WHERE id = 1")
print(f"\nUpdated plan column: {cur.rowcount} rows")

# Also update config JSON
cur.execute("SELECT config FROM tenants WHERE id = 1")
row = cur.fetchone()
if row and row[0]:
    try:
        cfg = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    except:
        cfg = {}
else:
    cfg = {}

cfg["plan"] = "enterprise"
cfg_json = json.dumps(cfg)
cur.execute("UPDATE tenants SET config = ? WHERE id = 1", (cfg_json,))
print(f"Updated config JSON: {cur.rowcount} rows")

db.commit()

# Verify
cur.execute("SELECT id, plan, config FROM tenants WHERE id = 1")
r = cur.fetchone()
print(f"\nVerification: plan={r[1]}")
if r[2]:
    cfg = json.loads(r[2])
    print(f"  config.plan = {cfg.get('plan', 'NOT SET')}")

db.close()
print("DONE")
