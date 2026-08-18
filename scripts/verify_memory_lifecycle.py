"""Verify bi-temporal memory: staleness, supersession, reranking, and time travel.

Staleness is repeatedly named as an open problem in production agent memory — outdated
preferences, resolved tasks and superseded facts silently degrade retrieval quality. This
asserts that a superseded lesson stops being authoritative WITHOUT being destroyed, because
the audit trail needs to reconstruct what the fleet believed at the time it acted.

Run: python scripts/verify_memory_lifecycle.py
"""
import sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
import logging; logging.basicConfig(level=logging.ERROR)

from fleetmem.db import DB
from fleetmem.memory import FleetMemory, ensure_fleet

DB.apply_schema()
fleet_id = ensure_fleet("lifecycle-fleet")
mem = FleetMemory(fleet_id)
DB.execute("DELETE FROM fleet_memory WHERE fleet_id = %s", (fleet_id,))

print("=" * 68)
print("1. TWO CLOCKS — event time and ingestion time are recorded separately")
print("=" * 68)
yesterday = datetime.now(timezone.utc) - timedelta(days=1)
row = mem.remember("R1", "bay 7 conveyor jams when loaded above 40 kilos",
                   location="bay-7", observed_at=yesterday)
print(f"   observed_at (when it happened) : {row['observed_at']:%Y-%m-%d %H:%M}")
print(f"   created_at  (when we learned)  : {row['created_at']:%Y-%m-%d %H:%M}")
assert row["observed_at"] < row["created_at"], "the two clocks were conflated"
print("   PASS: 'when it happened' and 'when we learned it' are distinguishable")

print()
print("=" * 68)
print("2. SUPERSESSION — a corrected lesson retires the old one, without deleting it")
print("=" * 68)
old = mem.remember("R2", "dock-4 approach lane is clear", location="dock-4")
time.sleep(0.4)
new = mem.supersede(old["id"], "R5",
                    "dock-4 approach lane is blocked by racking since the refit",
                    location="dock-4")
live = [h["lesson"] for h in mem.recall("dock-4 approach lane", limit=5)]
allrows = [h["lesson"] for h in mem.recall("dock-4 approach lane", limit=5,
                                           include_retired=True)]
print(f"   authoritative now : {live}")
print(f"   still on record   : {len(allrows)} rows (retired one retained)")
assert any("blocked by racking" in l for l in live), "replacement not authoritative"
assert not any(l == "dock-4 approach lane is clear" for l in live), "stale lesson still live"
assert len(allrows) > len(live), "the retired lesson was destroyed"
print("   PASS: stale lesson is no longer returned, but is not lost")

print()
print("=" * 68)
print("3. TIME TRAVEL — what did the fleet believe BEFORE the correction?")
print("=" * 68)
before = old["created_at"] + timedelta(milliseconds=120)
past = [h["lesson"] for h in mem.recall("dock-4 approach lane", limit=5, as_of=before)]
print(f"   as_of {before:%H:%M:%S.%f}: {past}")
assert any("is clear" in l for l in past), "cannot reconstruct the earlier belief"
print("   PASS: the earlier belief is reconstructible — this is what incident")
print("         reconstruction under ISO 3691-4 / ANSI R15.08 actually requires")

print()
print("=" * 68)
print("4. VALIDITY WINDOW — a time-bounded fact expires on its own")
print("=" * 68)
mem.remember("R3", "spill in aisle 3 being cleaned right now", location="aisle-3",
             valid_for_seconds=2)
hit_now = [h["lesson"] for h in mem.recall("spill in aisle 3", limit=3)]
print(f"   immediately : {'found' if any('spill' in l for l in hit_now) else 'not found'}")
time.sleep(2.6)
hit_later = [h["lesson"] for h in mem.recall("spill in aisle 3", limit=3)]
print(f"   after expiry: {'found' if any('spill' in l for l in hit_later) else 'not found'}")
assert any("spill" in l for l in hit_now) and not any("spill" in l for l in hit_later)
print("   PASS: expired fact stopped being authoritative without anyone deleting it")

print()
print("=" * 68)
print("5. RERANKING — a recurring condition does not decay like an event")
print("=" * 68)
DB.execute("DELETE FROM fleet_memory WHERE fleet_id = %s", (fleet_id,))
long_ago = datetime.now(timezone.utc) - timedelta(days=180)
mem.remember("R1", "floor near the loading door is wet on rainy mornings",
             location="door-1", observed_at=long_ago, recurrence="rainy-mornings")
mem.remember("R2", "floor near the loading door was wet once in March",
             location="door-1", observed_at=long_ago)
hits = mem.recall("is the floor by the loading door slippery", limit=2)
for h in hits:
    print(f"   score {h['score']:.4f}  d={h['distance']:.4f}  "
          f"{'[recurring]' if h['recurrence'] else '[one-off] '}  {h['lesson'][:46]}")
assert hits[0]["recurrence"], "the recurring condition did not outrank the stale one-off"
print("   PASS: the recurring pattern outranks the decayed one-off")

print("\nALL MEMORY LIFECYCLE CHECKS PASSED")
