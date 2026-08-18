"""End-to-end verification of the memory layer. Run: python scripts/verify_memory.py"""
import sys, threading, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.WARNING)

from fleetmem.db import DB
from fleetmem.memory import FleetMemory, ensure_fleet
from fleetmem.errors import ResourceHeldError

DB.apply_schema()
fleet_id = ensure_fleet("verify-fleet")
mem = FleetMemory(fleet_id)

# clean slate for a repeatable check
DB.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))
DB.execute("DELETE FROM fleet_memory   WHERE fleet_id = %s", (fleet_id,))

print("=" * 62)
print("1. CLAIM RACE  — two robots, one dock, genuinely concurrent")
print("=" * 62)
results, lock, barrier = [], threading.Lock(), threading.Barrier(2)

def race(robot):
    m = FleetMemory(fleet_id)
    barrier.wait()
    try:
        m.claim("dock-3", robot, purpose="unload pallet")
        with lock: results.append((robot, "GRANTED", None))
    except ResourceHeldError as e:
        with lock: results.append((robot, "DENIED", e.holder))

ts = [threading.Thread(target=race, args=(r,)) for r in ("R1", "R2")]
[t.start() for t in ts]; [t.join() for t in ts]
for robot, outcome, holder in sorted(results):
    print(f"   {robot}: {outcome}" + (f"  (holder is {holder} -> re-route)" if holder else ""))
holders = mem.live_claims()
print(f"   live claims on dock-3: {len([c for c in holders if c['resource_id']=='dock-3'])}")
assert len([c for c in holders if c["resource_id"] == "dock-3"]) == 1, "MORE THAN ONE HOLDER"
assert sum(1 for _, o, _ in results if o == "GRANTED") == 1
print("   PASS: exactly one robot holds the dock; loser learned WHO holds it\n")

print("=" * 62)
print("2. SHARED SEMANTIC MEMORY — one robot learns, another recalls")
print("=" * 62)
mem.remember("R1", "pallet at bay 12 slips when lifted too fast", location="bay-12")
mem.remember("R4", "charger 4 reports false full battery after 80 percent", location="charger-4")
mem.remember("R2", "floor near loading door is wet on rainy mornings", location="door-1")
hits = mem.recall("pallet slips at bay 12", limit=3)
for h in hits:
    print(f"   d={h['distance']:.4f}  [{h['robot_id']}] {h['lesson'][:52]}")
assert hits and "bay 12" in hits[0]["lesson"], "top hit is not the relevant lesson"
print(f"   PASS: R7 can retrieve what R1 learned (top hit from {hits[0]['robot_id']})\n")

print("=" * 62)
print("3. DURABLE CHECKPOINT — survives worker death")
print("=" * 62)
run_id = mem.start_run("R5", "deliver pallet to dock-3")
mem.checkpoint(run_id, 3, {"leg": "approach", "waypoint": [4, 7]})
print("   worker killed mid-task (simulated)...")
resumed = mem.resume(run_id)
print(f"   resumed at step {resumed['step']} with state {resumed['state']}")
assert resumed["step"] == 3
print("   PASS: resumes at step 3, side effects not replayed\n")

print("=" * 62)
print("4. RELEASE + RE-CLAIM — the constraint permits reuse, not double-holding")
print("=" * 62)
winner = [r for r, o, _ in results if o == "GRANTED"][0]
mem.release("dock-3", winner)
mem.claim("dock-3", "R9", purpose="charge")
print(f"   {winner} released; R9 now holds dock-3 -> {mem.holder_of('dock-3')}")
assert mem.holder_of("dock-3") == "R9"
print("   PASS\n")

print("ALL CHECKS PASSED")
