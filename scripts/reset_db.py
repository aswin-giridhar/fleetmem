"""Drop and recreate the FleetMem schema.

Exists because CREATE TABLE IF NOT EXISTS will happily leave an older, incompatible table
in place and report success. Destructive by design; never point it at production.
"""
import sys
sys.path.insert(0, ".")
from fleetmem.db import DB

TABLES = ["agent_events", "agent_runs", "fleet_memory", "resource_claims",
          "resources", "robots", "fleets"]

for t in TABLES:
    DB.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
print("dropped:", ", ".join(TABLES))
DB.apply_schema()
print("schema reapplied")
cols = DB.query("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema='public' AND column_name='fleet_id' ORDER BY table_name""")
print("tables carrying fleet_id:", ", ".join(c["table_name"] for c in cols))
