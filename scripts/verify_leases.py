"""Verify claim leases: a crashed robot must not hold a resource forever.

Run: python scripts/verify_leases.py
"""
import sys, time, threading
sys.path.insert(0, ".")
import logging; logging.basicConfig(level=logging.ERROR)

from fleetmem.db import DB
from fleetmem.memory import FleetMemory, ensure_fleet
from fleetmem.errors import ResourceHeldError

DB.apply_schema()
fleet_id = ensure_fleet("lease-test-fleet")
mem = FleetMemory(fleet_id)
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))

print("=" * 64)
print("1. A LIVE lease blocks other robots")
print("=" * 64)
mem.claim("dock-9", "R1", purpose="unload", lease_seconds=3)
try:
    mem.claim("dock-9", "R2", purpose="unload")
    print("   FAIL: R2 got a dock R1 holds"); sys.exit(1)
except ResourceHeldError as e:
    print(f"   R2 denied while R1's lease is live (holder {e.holder})  PASS")

print()
print("=" * 64)
print("2. HEARTBEAT keeps a working robot's claim alive")
print("=" * 64)
time.sleep(1.5)
print("   renew ->", mem.renew("dock-9", "R1", lease_seconds=3))
time.sleep(2.0)          # past the ORIGINAL 3s expiry, but renewed
holder = mem.holder_of("dock-9")
print(f"   holder after original expiry would have passed: {holder}")
assert holder == "R1", "renewal failed to keep the claim"
print("   PASS: a heartbeating robot keeps its dock")

print()
print("=" * 64)
print("3. A CRASHED robot's lease expires and the dock is reclaimable")
print("=" * 64)
print("   R1 crashes (stops heartbeating)...")
time.sleep(3.2)          # lease lapses, nobody renews
print("   holder now:", mem.holder_of("dock-9"), "(None = lease lapsed)")
res = mem.claim("dock-9", "R7", purpose="rescue")
print(f"   R7 claimed the abandoned dock: granted={res['granted']}")
assert mem.holder_of("dock-9") == "R7"
print("   PASS: crashed robot no longer blocks the fleet forever")

print()
print("=" * 64)
print("4. THE HARD CASE — two robots race for an EXPIRED claim")
print("=" * 64)
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))
mem.claim("dock-8", "DEAD", purpose="then crashes", lease_seconds=1)
time.sleep(1.4)                                    # DEAD's lease lapses
results, lock, barrier = [], threading.Lock(), threading.Barrier(2)

def grab(rid):
    m = FleetMemory(fleet_id)
    barrier.wait()
    try:
        m.claim("dock-8", rid, purpose="take over")
        with lock: results.append((rid, "GRANTED"))
    except ResourceHeldError as e:
        with lock: results.append((rid, f"DENIED (holder {e.holder})"))
    except Exception as e:
        with lock: results.append((rid, f"ERROR {type(e).__name__}"))

ts = [threading.Thread(target=grab, args=(r,)) for r in ("R3", "R4")]
[t.start() for t in ts]; [t.join() for t in ts]
for rid, outcome in sorted(results): print(f"   {rid}: {outcome}")
live = [c for c in mem.live_claims() if c["resource_id"] == "dock-8"]
granted = sum(1 for _, o in results if o == "GRANTED")
print(f"   live claims on dock-8: {len(live)} | granted: {granted}")
assert len(live) == 1 and granted == 1, "reap-then-claim was not atomic"
print("   PASS: reap and claim are atomic — still exactly one winner")

print("\nALL LEASE CHECKS PASSED")
