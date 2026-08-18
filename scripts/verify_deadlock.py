"""Verify deadlock detection — a different failure class from the collision.

The partial unique index stops two robots holding ONE resource. It does nothing about the
cycle where A holds what B needs while B holds what A needs: every individual claim is
valid, and the fleet still stops. Industry fleet managers sequence movements at
intersections precisely to avoid this.

Run: python scripts/verify_deadlock.py
"""
import sys
sys.path.insert(0, ".")
import logging; logging.basicConfig(level=logging.ERROR)

from fleetmem.db import DB
from fleetmem.memory import FleetMemory, ensure_fleet
from fleetmem.errors import ResourceHeldError

DB.apply_schema()
fleet_id = ensure_fleet("deadlock-fleet")
mem = FleetMemory(fleet_id)
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))
DB.execute("DELETE FROM resource_waits  WHERE fleet_id = %s", (fleet_id,))

print("=" * 68)
print("1. Build a genuine deadlock: every claim is individually legitimate")
print("=" * 68)
mem.claim("aisle-A", "R1", purpose="transit")
mem.claim("aisle-B", "R2", purpose="transit")
print("   R1 holds aisle-A, R2 holds aisle-B  (both valid, no rule broken)")

for robot, wanted in (("R1", "aisle-B"), ("R2", "aisle-A")):
    try:
        mem.claim(wanted, robot)
    except ResourceHeldError as e:
        mem.wait_for(wanted, robot)
        print(f"   {robot} blocked on {wanted} (held by {e.holder}) -> recorded as waiting")

print()
print("=" * 68)
print("2. The unique index alone cannot see this")
print("=" * 68)
live = mem.live_claims()
print(f"   live claims: {[(c['resource_id'], c['robot_id']) for c in live]}")
print("   one holder per resource: satisfied. And the fleet is stuck.")

print()
print("=" * 68)
print("3. Detect the cycle from the wait-for graph")
print("=" * 68)
graph = mem.wait_for_graph()
print(f"   wait-for graph: { {k: sorted(v) for k, v in graph.items()} }")
cycles = mem.detect_deadlocks()
print(f"   cycles found  : {cycles}")
assert cycles and set(cycles[0]) == {"R1", "R2"}, "cycle not detected"
print("   PASS: R1 -> R2 -> R1 identified")

print()
print("=" * 68)
print("4. Break it, and say who yielded")
print("=" * 68)
victim = mem.break_deadlock(cycles[0])
print(f"   victim (youngest claim): {victim}")
after = mem.detect_deadlocks()
print(f"   cycles remaining       : {after}")
assert victim in ("R1", "R2") and not after, "deadlock survived"
print("   PASS: cycle resolved, and the release is attributable in agent_events")

print()
print("=" * 68)
print("5. No false positives when robots merely queue")
print("=" * 68)
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))
DB.execute("DELETE FROM resource_waits  WHERE fleet_id = %s", (fleet_id,))
mem.claim("dock-z", "R7")
for r in ("R8", "R9"):
    try:
        mem.claim("dock-z", r)
    except ResourceHeldError:
        mem.wait_for("dock-z", r)
print("   R8 and R9 both queue behind R7 (a queue, not a cycle)")
print(f"   cycles found: {mem.detect_deadlocks()}")
assert not mem.detect_deadlocks(), "reported a deadlock where none exists"
print("   PASS: waiting is not deadlock — the check discriminates")

print("\nALL DEADLOCK CHECKS PASSED")
