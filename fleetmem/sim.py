"""Warehouse simulation.

Robot positions are simulated in memory and stepped at ~10 fps. The DATABASE is written only
on meaningful events — a claim, a release, a lesson, a decision — never per frame. That
keeps CockroachDB Cloud Request Unit consumption proportional to decisions rather than to
animation, which matters on a metered cluster that has to stay alive for weeks of judging.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field

from .agent import RobotAgent
from .errors import ResourceHeldError
from .memory import FleetMemory, ensure_fleet

log = logging.getLogger("fleetmem.sim")

WIDTH, HEIGHT = 100.0, 60.0

DOCKS = {
    "dock-1": (88.0, 10.0),
    "dock-2": (88.0, 26.0),
    "dock-3": (88.0, 42.0),
    "charger-1": (6.0, 12.0),
    "charger-2": (6.0, 44.0),
}

SEED_LESSONS = [
    ("R1", "pallet at bay 12 slips when lifted too fast", "dock-3"),
    ("R4", "charger-2 reports a false full battery above 80 percent", "charger-2"),
    ("R2", "floor near dock-1 is wet on rainy mornings, approach slowly", "dock-1"),
]


@dataclass
class Robot:
    id: str
    x: float
    y: float
    target: str | None = None
    holding: str | None = None
    status: str = "idle"
    speed: float = 0.9
    note: str = ""
    trail: list = field(default_factory=list)


class Warehouse:
    def __init__(self, fleet_name: str = "warehouse-1"):
        self._fleet_name = fleet_name
        self.fleet_id = ensure_fleet(fleet_name)
        self.memory = FleetMemory(self.fleet_id)
        self.robots: dict[str, Robot] = {}
        self.lock = threading.RLock()
        self.tick = 0
        self.log_lines: list[dict] = []
        self._stop = threading.Event()
        self._seed()

    # ------------------------------------------------------------------ setup

    def refresh_fleet(self) -> None:
        """Re-resolve the fleet id.

        reset_db.py can drop and recreate `fleets` underneath a running server, leaving this
        process holding an id that no longer exists — every write then fails with a foreign
        key violation. Re-resolving is cheap and turns a fatal state into a recoverable one.
        """
        from .memory import ensure_fleet
        self.fleet_id = ensure_fleet(self._fleet_name)
        self.memory = FleetMemory(self.fleet_id)
        # Re-seed: a schema reset wipes the fleet's lessons, and a warehouse with an empty
        # memory is the one thing this demo must never show. Seeding is idempotent.
        self._seed_lessons()

    def fleet_is_valid(self) -> bool:
        rows = self.memory.db.query("SELECT 1 AS ok FROM fleets WHERE id = %s",
                                    (self.fleet_id,))
        return bool(rows)

    def _seed(self):
        random.seed(7)
        for i in range(1, 7):
            rid = f"R{i}"
            self.robots[rid] = Robot(rid, x=random.uniform(10, 45),
                                     y=random.uniform(6, HEIGHT - 6))
        self._seed_lessons()

    def _seed_lessons(self):
        """Ensure the fleet's baseline lessons exist. Idempotent."""
        try:
            existing = {m["lesson"] for m in self.memory.recall("warehouse", limit=50)}
        except Exception as exc:
            log.warning("could not read existing memories: %s", exc)
            return
        for robot_id, lesson, location in SEED_LESSONS:
            if lesson not in existing:
                self.memory.remember(robot_id, lesson, location=location)

    def emit(self, kind: str, text: str, robot_id: str | None = None):
        entry = {"t": self.tick, "kind": kind, "text": text, "robot": robot_id,
                 "ts": time.time()}
        self.log_lines.append(entry)
        del self.log_lines[:-60]

    # ------------------------------------------------------------- behaviour

    def assign(self, robot_id: str, task: str, candidates: list[str] | None = None) -> dict:
        """Run one agent decision + claim cycle. Safe to call concurrently."""
        candidates = candidates or list(DOCKS)
        robot = self.robots[robot_id]
        agent = RobotAgent(robot_id, self.memory)
        result = agent.execute(task, candidates)
        decision = result["decision"]

        with self.lock:
            if result["granted"]:
                robot.target = result["granted"]
                robot.holding = result["granted"]
                robot.status = "moving"
                robot.speed = 0.45 if decision.get("speed") == "slow" else 0.9
                robot.note = decision.get("reason", "")
                self.emit("claim", f"{robot_id} claimed {result['granted']}"
                                   + (" (slow: recalled hazard)" if decision.get("memory_used") else ""),
                          robot_id)
                if decision.get("memory_used"):
                    self.emit("recall", f"{robot_id} recalled: {decision['memory_used']}", robot_id)
            else:
                robot.status = "blocked"
                robot.note = "all candidates held"
                self.emit("blocked", f"{robot_id} found every candidate held", robot_id)
        return result

    def race(self, resource: str = "dock-3", robots: tuple[str, str] = ("R1", "R2")) -> list[dict]:
        """THE demo: two agents, one dock, genuinely concurrent."""
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(len(robots))

        def go(rid):
            try:
                mem = FleetMemory(self.fleet_id)
                agent = RobotAgent(rid, mem)
                decision = agent.plan(f"deliver pallet to {resource}", [resource])
            except Exception as exc:
                log.exception("race worker %s failed before claiming", rid)
                barrier.wait()
                with lock:
                    results.append({"robot": rid, "granted": False, "holder": None,
                                    "error": f"{type(exc).__name__}: {exc}"})
                return
            barrier.wait()                      # genuine simultaneity
            try:
                mem.claim(resource, rid, purpose="deliver pallet")
                outcome = {"robot": rid, "granted": True, "holder": rid,
                           "reason": decision.reason, "memory_used": decision.memory_used}
            except ResourceHeldError as held:
                outcome = {"robot": rid, "granted": False, "holder": held.holder,
                           "reason": decision.reason, "memory_used": decision.memory_used}
            except Exception as exc:
                # An exception raised in a worker thread does not reach the caller. Without
                # this branch the race silently returns an empty result list and the API
                # reports 200 OK for something that entirely failed to run.
                log.exception("race worker %s failed", rid)
                outcome = {"robot": rid, "granted": False, "holder": None,
                           "error": f"{type(exc).__name__}: {exc}"}
            with lock:
                results.append(outcome)

        with self.lock:
            for rid in robots:
                self.memory.release(resource, rid)
            self.robots[robots[0]].x, self.robots[robots[0]].y = 40, 20
            self.robots[robots[1]].x, self.robots[robots[1]].y = 40, 46

        threads = [threading.Thread(target=go, args=(r,)) for r in robots]
        [t.start() for t in threads]; [t.join() for t in threads]

        winner = next((r for r in results if r["granted"]), None)
        with self.lock:
            for r in results:
                robot = self.robots[r["robot"]]
                if r["granted"]:
                    robot.target = resource; robot.holding = resource
                    robot.status = "moving"; robot.note = "claim granted"
                else:
                    alt = next((d for d in DOCKS if d != resource
                                and not self.memory.holder_of(d)), None)
                    robot.target = alt; robot.holding = None
                    robot.status = "rerouting"
                    robot.note = f"{resource} held by {r['holder']} -> {alt}"
        if winner:
            self.emit("race", f"RACE on {resource}: {winner['robot']} won; "
                              f"{[r['robot'] for r in results if not r['granted']]} re-routed")
        return results

    def step(self):
        """Advance the simulation one frame. No database writes here."""
        with self.lock:
            self.tick += 1
            for robot in self.robots.values():
                if not robot.target:
                    continue
                tx, ty = DOCKS.get(robot.target, (robot.x, robot.y))
                dx, dy = tx - robot.x, ty - robot.y
                dist = math.hypot(dx, dy)
                if dist < 1.0:
                    if robot.status != "arrived":
                        robot.status = "arrived"
                        self.emit("arrive", f"{robot.id} arrived at {robot.target}", robot.id)
                    continue
                robot.x += dx / dist * robot.speed
                robot.y += dy / dist * robot.speed
                robot.trail.append((round(robot.x, 1), round(robot.y, 1)))
                del robot.trail[:-25]

    def release_all(self):
        with self.lock:
            for robot in self.robots.values():
                if robot.holding:
                    self.memory.release(robot.holding, robot.id)
                robot.holding = None; robot.target = None
                robot.status = "idle"; robot.note = ""; robot.trail.clear()
            self.emit("reset", "all claims released")

    def snapshot(self) -> dict:
        with self.lock:
            live = self.memory.live_claims()
            claims = {c["resource_id"]: c["robot_id"] for c in live}
            # The database is the source of truth. A lease can expire while a robot is
            # parked, and a robot still rendered as "holding" when its claim is gone is the
            # view lying about the memory layer — the one thing this demo must never do.
            for robot in self.robots.values():
                if robot.holding and claims.get(robot.holding) != robot.id:
                    robot.holding = None
                    robot.status = "idle"
                    robot.note = "lease expired"
            # seconds remaining on each lease, so the UI can show a claim expiring
            ttl = {c["resource_id"]: max(0, int(c["ttl"].total_seconds()))
                   for c in live if c.get("ttl") is not None}
            return {
                "tick": self.tick,
                "width": WIDTH, "height": HEIGHT,
                "docks": [{"id": k, "x": v[0], "y": v[1], "holder": claims.get(k)}
                          for k, v in DOCKS.items()],
                "robots": [{"id": r.id, "x": round(r.x, 2), "y": round(r.y, 2),
                            "status": r.status, "target": r.target, "holding": r.holding,
                            "note": r.note, "trail": r.trail[-20:]}
                           for r in self.robots.values()],
                "claims": claims,
                "claim_ttl": ttl,
                "log": self.log_lines[-18:],
            }

    def run_forever(self, fps: int = 10):
        while not self._stop.is_set():
            self.step()
            time.sleep(1 / fps)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()

    def stop(self):
        self._stop.set()
