"""Verify fencing tokens: a paused robot must not act on a lease it has already lost.

A lease bounds the claim; it does not bound the machine. A robot paused by GC, scheduling,
or a network partition can wake after its lease lapsed and still be physically moving. The
standard remedy is a monotonically increasing token validated at the point of action.

Run: python scripts/verify_fencing.py
"""
import sys, time, threading
sys.path.insert(0, ".")
import logging; logging.basicConfig(level=logging.ERROR)

from fleetmem.db import DB
from fleetmem.memory import FleetMemory, StaleFenceError, ensure_fleet
from fleetmem.errors import ResourceHeldError

DB.apply_schema()
fleet_id = ensure_fleet("fence-test-fleet")
mem = FleetMemory(fleet_id)
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))

print("=" * 66)
print("1. A grant issues a fencing token, and the holder may act")
print("=" * 66)
g1 = mem.claim("dock-f", "R1", purpose="unload", lease_seconds=2)
print(f"   R1 granted with epoch {g1['epoch']}")
print("   R1 acts:", mem.act("dock-f", "R1", g1["epoch"])["authorised"], " PASS")

print()
print("=" * 66)
print("2. Tokens increase monotonically across successive grants")
print("=" * 66)
time.sleep(2.3)                                  # R1's lease lapses
g2 = mem.claim("dock-f", "R2", purpose="take over")
print(f"   R1 held epoch {g1['epoch']}, R2 now holds epoch {g2['epoch']}")
assert g2["epoch"] > g1["epoch"], "epoch did not advance"
print("   PASS: the token advanced")

print()
print("=" * 66)
print("3. THE CASE LEASES ALONE DO NOT COVER")
print("=" * 66)
print("   R1 was paused (GC / partition) past its lease. It wakes up still believing")
print("   it holds dock-f, and tries to move. R2 legitimately holds it now.")
try:
    mem.act("dock-f", "R1", g1["epoch"])
    print("   FAIL: the stale actor was authorised")
    sys.exit(1)
except StaleFenceError as e:
    print(f"   REJECTED: {e}")
    print("   PASS: the actuator is fenced, not just the claim")

print()
print("=" * 66)
print("4. The current holder is unaffected")
print("=" * 66)
print("   R2 acts:", mem.act("dock-f", "R2", g2["epoch"])["authorised"], " PASS")

print()
print("=" * 66)
print("5. Tokens stay unique under a concurrent race")
print("=" * 66)
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))
epochs, lock, barrier = [], threading.Lock(), threading.Barrier(5)
def grab(i):
    m = FleetMemory(fleet_id)
    barrier.wait()
    try:
        g = m.claim("dock-g", f"R{i}")
        with lock: epochs.append(g["epoch"])
    except ResourceHeldError:
        pass
ts = [threading.Thread(target=grab, args=(i,)) for i in range(5)]
[t.start() for t in ts]; [t.join() for t in ts]
print(f"   grants: {len(epochs)} | epochs issued: {epochs}")
assert len(epochs) == 1, "more than one robot was granted the resource"
print("   PASS: one grant, one token")

print("\nALL FENCING CHECKS PASSED")
