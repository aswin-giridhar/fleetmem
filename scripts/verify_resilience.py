"""Kill a CockroachDB node while the fleet is actively claiming resources.

The hackathon's own thesis is that an agent whose memory goes offline does not degrade —
it stops. This script tests that claim directly: a continuous claim/release workload runs
against a 3-node cluster while one node is killed, and every operation is recorded as it
happens so the timeline shows exactly what the fleet experienced.

Honest by construction: it kills a node the client is NOT connected to, and says so. Losing
the coordinating node is a different (also survivable) scenario involving client reconnect;
conflating the two would overstate the result.

Prereq:  ./infra/cluster3.sh up
Run:     python scripts/verify_resilience.py
"""
import subprocess
import sys
import threading
import time

sys.path.insert(0, ".")
import logging
logging.basicConfig(level=logging.ERROR)

from fleetmem.db import Database
from fleetmem.memory import FleetMemory, ensure_fleet
from fleetmem.errors import ResourceHeldError

DSN = "postgresql://root@localhost:26261/fleet?sslmode=disable"   # node 1
VICTIM = "crdb2"                                                   # NOT the node we use
DURATION = 24
KILL_AT = 8

db = Database(DSN)
db.apply_schema()
fleet_id = ensure_fleet("resilience-fleet", db=db)
db.execute("DELETE FROM resource_claims WHERE fleet_id = %s", (fleet_id,))

timeline: list[tuple[float, str, str]] = []
lock = threading.Lock()
stop = threading.Event()
counts = {"ok": 0, "failed": 0}


def workload():
    """Claim and release a dock, over and over, recording every outcome."""
    mem = FleetMemory(fleet_id, db=db)
    i = 0
    started = time.time()
    while not stop.is_set():
        i += 1
        elapsed = time.time() - started
        try:
            mem.claim(f"dock-{i % 3}", f"R{i % 5}", purpose="resilience probe")
            mem.release(f"dock-{i % 3}", f"R{i % 5}")
            with lock:
                counts["ok"] += 1
                timeline.append((elapsed, "ok", ""))
        except ResourceHeldError:
            with lock:
                counts["ok"] += 1          # expected contention, not a failure
                timeline.append((elapsed, "ok", "held"))
        except Exception as exc:
            with lock:
                counts["failed"] += 1
                timeline.append((elapsed, "FAILED", f"{type(exc).__name__}: {str(exc)[:60]}"))
        time.sleep(0.25)


print("=" * 68)
print("NODE-KILL RESILIENCE TEST")
print("=" * 68)
print(f"  cluster : 3 nodes (local docker)")
print(f"  client  : connected to node 1 (localhost:26261)")
print(f"  victim  : {VICTIM}  <- a DIFFERENT node from the one the client uses")
print(f"  plan    : run {DURATION}s of claim/release, kill {VICTIM} at t={KILL_AT}s")
print()

worker = threading.Thread(target=workload, daemon=True)
worker.start()

killed_at = None
for second in range(DURATION):
    time.sleep(1)
    if second == KILL_AT:
        print(f"  t={second:>2}s  *** docker kill {VICTIM} ***")
        subprocess.run(["docker", "kill", VICTIM], capture_output=True)
        killed_at = second
    else:
        with lock:
            ok, failed = counts["ok"], counts["failed"]
        print(f"  t={second:>2}s  ok={ok:<4} failed={failed}")

stop.set()
worker.join(timeout=5)

print()
print("=" * 68)
print("RESULT")
print("=" * 68)
with lock:
    after = [t for t in timeline if killed_at and t[0] >= killed_at]
    fails_after = [t for t in after if t[1] == "FAILED"]
    print(f"  operations total          : {counts['ok'] + counts['failed']}")
    print(f"  succeeded                 : {counts['ok']}")
    print(f"  failed                    : {counts['failed']}")
    print(f"  operations after the kill : {len(after)}")
    print(f"  failures after the kill   : {len(fails_after)}")
    for t in fails_after[:5]:
        print(f"      t={t[0]:.1f}s  {t[2]}")

print()
subprocess.run(["docker", "start", VICTIM], capture_output=True)
print(f"  ({VICTIM} restarted for the next run)")

verdict = counts["failed"] == 0
print()
print("  VERDICT:", "PASS - the fleet's memory survived losing a node with zero failed writes"
      if verdict else
      f"  {counts['failed']} operations failed - see the timeline above")
sys.exit(0 if verdict else 1)
