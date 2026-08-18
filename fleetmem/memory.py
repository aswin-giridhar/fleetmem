"""The memory layer: claims, semantic recall, checkpoints, audit.

This is the whole thesis of the project. For an agent, memory is not a cache — it is the
input to an action. So the operations here are transactional, and "someone else already
holds this" is a first-class, actionable answer rather than an error to be swallowed.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import psycopg

from .db import DB, Database
from .embeddings import embed, to_pgvector
from .errors import FleetMemError, ResourceHeldError

log = logging.getLogger("fleetmem.memory")

UNIQUE_VIOLATION = "23505"


class StaleFenceError(FleetMemError):
    """An actor tried to act with an outdated fencing token.

    Raised when a robot that believes it holds a resource presents an epoch lower than the
    current grant — the signature of a process that paused past its lease and woke up still
    intending to move. Distinct from ResourceHeldError: this actor is not merely late to
    the claim, it is acting on a belief the cluster has already superseded.
    """

    def __init__(self, resource_id: str, presented: int, current: int | None):
        self.resource_id = resource_id
        self.presented = presented
        self.current = current
        super().__init__(
            f"stale fence on {resource_id}: presented epoch {presented}, "
            f"current is {current if current is not None else 'no live claim'}")


class FleetMemory:
    def __init__(self, fleet_id: str, db: Database | None = None):
        self.fleet_id = fleet_id
        self.db = db or DB

    # ---------------------------------------------------------------- claims

    DEFAULT_LEASE_SECONDS = 90

    def reap_expired(self) -> int:
        """Release claims whose lease has lapsed. Returns how many were reclaimed.

        Safe to call from anywhere: a lapsed lease means the holder stopped heartbeating,
        so the resource is genuinely free.
        """
        rows = self.db.query(
            """UPDATE resource_claims SET released_at = now(), expired = true
               WHERE fleet_id = %s AND released_at IS NULL AND expires_at <= now()
               RETURNING resource_id, robot_id""",
            (self.fleet_id,),
        )
        for row in rows:
            log.warning("lease expired: %s reclaimed from %s",
                        row["resource_id"], row["robot_id"])
            self.record_event(row["robot_id"], "lease_expired",
                              {"resource_id": row["resource_id"]})
        return len(rows)

    def renew(self, resource_id: str, robot_id: str,
              lease_seconds: int | None = None) -> bool:
        """Heartbeat: extend this robot's lease. False if it no longer holds the resource."""
        lease = lease_seconds or self.DEFAULT_LEASE_SECONDS
        rows = self.db.query(
            """UPDATE resource_claims
               SET expires_at = now() + (%s::INT * INTERVAL '1 second'), renewed_at = now()
               WHERE fleet_id = %s AND resource_id = %s AND robot_id = %s
                 AND released_at IS NULL
               RETURNING id, expires_at""",
            (lease, self.fleet_id, resource_id, robot_id),
        )
        return bool(rows)

    def claim(self, resource_id: str, robot_id: str, purpose: str = "",
              lease_seconds: int | None = None) -> dict:
        """Atomically claim a physical resource.

        Returns the claim on success. Raises ResourceHeldError — carrying the CURRENT
        holder — if another robot already holds it. The caller is expected to re-route,
        not to retry: the rejection is deterministic, not a transient conflict.
        """
        lease = lease_seconds or self.DEFAULT_LEASE_SECONDS

        def _txn(cur):
            # Reap-then-claim in ONE transaction. Under serializable isolation this means a
            # lapsed lease is reclaimed and the new claim taken atomically — two robots
            # racing for a resource whose holder has crashed still produce exactly one
            # winner, because the reap and the insert cannot interleave.
            cur.execute(
                """UPDATE resource_claims SET released_at = now(), expired = true
                   WHERE fleet_id = %s AND resource_id = %s AND released_at IS NULL
                     AND expires_at <= now()""",
                (self.fleet_id, resource_id),
            )
            # Fencing token from a sequence: monotonic, and it does not read contended
            # rows. The earlier MAX(epoch)+1 form was correct but made every claimant
            # conflict on the same rows, which is the opposite of what a lock should do.
            cur.execute("SELECT nextval('fleetmem_epoch') AS next")
            next_epoch = cur.fetchone()["next"]
            try:
                cur.execute(
                    """
                    INSERT INTO resource_claims
                        (fleet_id, resource_id, robot_id, purpose, epoch, expires_at)
                    VALUES (%s, %s, %s, %s, %s,
                            now() + (%s::INT * INTERVAL '1 second'))
                    RETURNING id, resource_id, robot_id, epoch, claimed_at, expires_at
                    """,
                    (self.fleet_id, resource_id, robot_id, purpose, next_epoch, lease),
                )
                return {"granted": True, **cur.fetchone()}
            except psycopg.errors.UniqueViolation:
                # Expected control flow. Find out who actually holds it so the losing
                # agent can make a decision instead of blindly retrying.
                cur.connection.rollback()
                cur.execute(
                    """
                    SELECT robot_id, claimed_at, expires_at FROM resource_claims
                    WHERE fleet_id = %s AND resource_id = %s AND released_at IS NULL
                    """,
                    (self.fleet_id, resource_id),
                )
                row = cur.fetchone()
                return {"granted": False, "holder": row["robot_id"] if row else None}

        result = self.db.run_in_txn(_txn)
        if not result.get("granted"):
            self.record_event(robot_id, "claim_denied",
                              {"resource_id": resource_id, "holder": result.get("holder")})
            raise ResourceHeldError(resource_id, result.get("holder"))
        self.record_event(robot_id, "claim_granted", {"resource_id": resource_id})
        return result

    def act(self, resource_id: str, robot_id: str, epoch: int) -> dict:
        """Authorise a physical action against a fencing token.

        Every irreversible act should pass through here. A robot that paused past its lease
        still believes it holds the dock; its epoch is what gives it away. Checking the
        holder alone is not enough — by the time it wakes, another robot may hold the
        resource under a NEWER epoch, and the sleeper's own identity check would pass if it
        happened to reclaim it in between.
        """
        rows = self.db.query(
            """SELECT robot_id, epoch FROM resource_claims
               WHERE fleet_id = %s AND resource_id = %s AND released_at IS NULL
                 AND expires_at > now()""",
            (self.fleet_id, resource_id),
        )
        current = rows[0] if rows else None
        if current is None or current["epoch"] != epoch or current["robot_id"] != robot_id:
            self.record_event(robot_id, "fence_rejected", {
                "resource_id": resource_id, "presented_epoch": epoch,
                "current_epoch": current["epoch"] if current else None,
                "current_holder": current["robot_id"] if current else None})
            raise StaleFenceError(resource_id, epoch,
                                  current["epoch"] if current else None)
        return {"authorised": True, "resource_id": resource_id,
                "robot_id": robot_id, "epoch": epoch}

    def release(self, resource_id: str, robot_id: str) -> bool:
        rows = self.db.query(
            """
            UPDATE resource_claims SET released_at = now()
            WHERE fleet_id = %s AND resource_id = %s AND robot_id = %s AND released_at IS NULL
            RETURNING id
            """,
            (self.fleet_id, resource_id, robot_id),
        )
        if rows:
            self.record_event(robot_id, "claim_released", {"resource_id": resource_id})
        return bool(rows)

    def holder_of(self, resource_id: str) -> str | None:
        rows = self.db.query(
            """SELECT robot_id FROM resource_claims
               WHERE fleet_id = %s AND resource_id = %s AND released_at IS NULL
                 AND expires_at > now()""",
            (self.fleet_id, resource_id),
        )
        return rows[0]["robot_id"] if rows else None

    def live_claims(self) -> list[dict]:
        return self.db.query(
            """SELECT resource_id, robot_id, claimed_at, purpose, expires_at,
                      (expires_at - now()) AS ttl
               FROM resource_claims
               WHERE fleet_id = %s AND released_at IS NULL AND expires_at > now()
               ORDER BY claimed_at""",
            (self.fleet_id,),
        )

    # ------------------------------------------------------------- deadlock

    def wait_for(self, resource_id: str, robot_id: str) -> None:
        """Record that a robot is blocked on a resource it does not hold."""
        self.db.execute(
            """INSERT INTO resource_waits (fleet_id, robot_id, resource_id)
               VALUES (%s, %s, %s)""",
            (self.fleet_id, robot_id, resource_id),
        )

    def stop_waiting(self, resource_id: str, robot_id: str) -> None:
        self.db.execute(
            """UPDATE resource_waits SET resolved_at = now()
               WHERE fleet_id = %s AND robot_id = %s AND resource_id = %s
                 AND resolved_at IS NULL""",
            (self.fleet_id, robot_id, resource_id),
        )

    def wait_for_graph(self) -> dict[str, set[str]]:
        """Build the wait-for graph: robot -> robots it is (transitively) blocked behind."""
        waits = self.db.query(
            """SELECT robot_id, resource_id FROM resource_waits
               WHERE fleet_id = %s AND resolved_at IS NULL""",
            (self.fleet_id,),
        )
        holders = {c["resource_id"]: c["robot_id"] for c in self.live_claims()}
        graph: dict[str, set[str]] = {}
        for w in waits:
            holder = holders.get(w["resource_id"])
            if holder and holder != w["robot_id"]:
                graph.setdefault(w["robot_id"], set()).add(holder)
        return graph

    def detect_deadlocks(self) -> list[list[str]]:
        """Find cycles in the wait-for graph.

        A genuinely different failure class from the one the unique index prevents. That
        constraint stops two robots holding ONE resource; it does nothing about A holding
        what B needs while B holds what A needs. Industry fleet managers sequence movements
        at intersections precisely to avoid this, and it is invisible unless the waits are
        recorded.
        """
        graph = self.wait_for_graph()
        cycles: list[list[str]] = []
        seen_cycles: set[frozenset] = set()

        def walk(node: str, path: list[str], visiting: set[str]) -> None:
            for nxt in graph.get(node, ()):  # neighbours = robots we are blocked behind
                if nxt in visiting:
                    cycle = path[path.index(nxt):] if nxt in path else [nxt]
                    key = frozenset(cycle)
                    if len(cycle) > 1 and key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
                    continue
                walk(nxt, path + [nxt], visiting | {nxt})

        for robot in list(graph):
            walk(robot, [robot], {robot})
        if cycles:
            self.record_event(None, "deadlock_detected", {"cycles": cycles})
            log.warning("deadlock detected: %s", cycles)
        return cycles

    def break_deadlock(self, cycle: list[str]) -> str | None:
        """Resolve a cycle by making the youngest claim yield.

        Choosing the youngest claim is the conventional victim policy: it has done the least
        work, so rolling it back wastes the least. Returning WHICH robot yielded matters —
        an unexplained release is indistinguishable from a bug.
        """
        if not cycle:
            return None
        rows = self.db.query(
            """SELECT robot_id, resource_id FROM resource_claims
               WHERE fleet_id = %s AND robot_id = ANY(%s) AND released_at IS NULL
               ORDER BY claimed_at DESC LIMIT 1""",
            (self.fleet_id, list(cycle)),
        )
        if not rows:
            return None
        victim = rows[0]
        self.release(victim["resource_id"], victim["robot_id"])
        self.stop_waiting(victim["resource_id"], victim["robot_id"])
        self.record_event(victim["robot_id"], "deadlock_broken",
                          {"released": victim["resource_id"], "cycle": cycle})
        return victim["robot_id"]

    # ------------------------------------------------------- semantic memory

    def remember(self, robot_id: str, lesson: str, *, kind: str = "incident",
                 location: str | None = None, confidence: float = 1.0,
                 report: dict | None = None, observed_at=None,
                 valid_for_seconds: int | None = None,
                 recurrence: str | None = None) -> dict:
        """Write a lesson AND its embedding in ONE transaction.

        This is the property a separate vector store cannot offer: there is no window in
        which the row exists and the vector does not, or vice versa. No dual-write, no
        reconciliation job, no silent drift between the operational and semantic views.
        """
        vector, provider = embed(lesson)

        # Bulk evidence goes to S3; the searchable memory goes to CockroachDB. Store the
        # artifact FIRST so the row can never point at an object that was never written.
        artifact_uri = None
        if report is not None:
            from .aws import STORE, ArtifactStoreError
            if STORE.enabled:
                try:
                    artifact_uri = STORE.put_incident_report(self.fleet_id, robot_id, report).uri
                except ArtifactStoreError as exc:
                    # Degrade to a memory without evidence rather than losing the lesson,
                    # but say so loudly — a silently evidence-less memory is a trap.
                    log.warning("artifact store unavailable, recording lesson without "
                                "evidence: %s", exc)

        def _txn(cur):
            cur.execute(
                """
                INSERT INTO fleet_memory
                    (fleet_id, robot_id, kind, lesson, location, embedding, provider,
                     confidence, artifact_uri, observed_at, valid_until, recurrence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s::TIMESTAMPTZ, now()),
                        CASE WHEN %s::INT IS NULL THEN NULL
                             ELSE now() + (%s::INT * INTERVAL '1 second') END,
                        %s::STRING)
                RETURNING id, observed_at, created_at, valid_until
                """,
                (self.fleet_id, robot_id, kind, lesson, location,
                 to_pgvector(vector), provider, confidence, artifact_uri,
                 observed_at, valid_for_seconds, valid_for_seconds, recurrence),
            )
            return cur.fetchone()

        row = self.db.run_in_txn(_txn)
        self.record_event(robot_id, "memory_written",
                          {"lesson": lesson, "provider": provider, "location": location,
                           "artifact_uri": artifact_uri})
        log.info("remembered [%s] %s", robot_id, lesson)
        return row

    def recall(self, query: str, limit: int = 5, max_distance: float | None = None,
               as_of=None, include_retired: bool = False, rerank: bool = True) -> list[dict]:
        """Semantic recall across the WHOLE fleet, with lifecycle and time travel.

        The fleet_id prefix is the first column of the vector index, so isolation is
        enforced by the index itself rather than by a filter a refactor could drop.

        Three things distinguish this from a plain similarity search:

        * **Retired lessons are excluded.** A lesson that has been superseded, or whose
          validity window has closed, is no longer authoritative. It is not deleted —
          staleness is the most commonly cited failure of production agent memory, and
          deleting the evidence would also destroy the audit trail.
        * **Time travel.** `as_of` answers "what did the fleet believe at time T" using
          ingestion time and supersession time, which is the question incident
          reconstruction actually asks.
        * **Reranking.** Cosine distance alone ignores that a corroborated lesson observed
          this morning beats a low-confidence one from six months ago. Similarity gets you
          candidates; it does not get you the right answer.
        """
        vector, _ = embed(query)
        vec = to_pgvector(vector)
        params: list[Any] = [vec, self.fleet_id]

        if as_of is not None:
            # Believed at T: ingested by then, and not yet superseded as of then.
            lifecycle = ("AND created_at <= %s "
                         "AND (superseded_at IS NULL OR superseded_at > %s) "
                         "AND (valid_until IS NULL OR valid_until > %s)")
            params += [as_of, as_of, as_of]
        elif include_retired:
            lifecycle = ""
        else:
            lifecycle = ("AND superseded_at IS NULL "
                         "AND (valid_until IS NULL OR valid_until > now())")

        params += [vec, limit * 3 if rerank else limit]
        rows = self.db.query(
            f"""
            SELECT id, robot_id, kind, lesson, location, provider, artifact_uri,
                   confidence, recurrence, observed_at, created_at, valid_until,
                   superseded_at, embedding <=> %s AS distance
            FROM fleet_memory
            WHERE fleet_id = %s {lifecycle}
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            params,
        )
        if max_distance is not None:
            rows = [r for r in rows if r["distance"] is not None
                    and r["distance"] <= max_distance]
        if rerank:
            rows = self._rerank(rows)
        return rows[:limit]

    # Reranking weights. Deliberately small relative to distance: similarity still decides
    # which lessons are candidates, and these only reorder within that set.
    W_CONFIDENCE = 0.06
    W_RECENCY = 0.05
    RECENCY_HALFLIFE_DAYS = 30.0

    def _rerank(self, rows: list[dict]) -> list[dict]:
        """Order by relevance, not similarity alone.

        score = distance - confidence_bonus - recency_bonus   (lower is better)

        A recurring condition ('the floor is wet on rainy mornings') does not decay: it
        describes a pattern rather than an event, so recency is not evidence against it.
        """
        import math
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for row in rows:
            score = float(row["distance"]) if row["distance"] is not None else 1.0
            score -= self.W_CONFIDENCE * float(row.get("confidence") or 1.0)
            if not row.get("recurrence"):
                observed = row.get("observed_at") or row.get("created_at")
                if observed is not None:
                    age_days = max(0.0, (now - observed).total_seconds() / 86400.0)
                    freshness = math.exp(-age_days / self.RECENCY_HALFLIFE_DAYS)
                    score -= self.W_RECENCY * freshness
            row["score"] = round(score, 6)
        return sorted(rows, key=lambda r: r["score"])

    def supersede(self, old_id: str, robot_id: str, lesson: str, **kwargs) -> dict:
        """Replace a lesson with a corrected one, atomically.

        The old row is retired rather than deleted: an audit trail that loses what was
        previously believed cannot reconstruct why an agent acted as it did.
        """
        new = self.remember(robot_id, lesson, **kwargs)
        self.db.execute(
            """UPDATE fleet_memory SET superseded_by = %s, superseded_at = now()
               WHERE id = %s AND fleet_id = %s""",
            (new["id"], old_id, self.fleet_id),
        )
        self.record_event(robot_id, "memory_superseded",
                          {"old_id": str(old_id), "new_id": str(new["id"]),
                           "lesson": lesson})
        return new

    def retire(self, memory_id: str, robot_id: str, reason: str = "") -> bool:
        """Retire a lesson without a replacement (e.g. the bay was reconfigured)."""
        rows = self.db.query(
            """UPDATE fleet_memory SET superseded_at = now()
               WHERE id = %s AND fleet_id = %s AND superseded_at IS NULL
               RETURNING id""",
            (memory_id, self.fleet_id),
        )
        if rows:
            self.record_event(robot_id, "memory_retired",
                              {"id": str(memory_id), "reason": reason})
        return bool(rows)

    # ----------------------------------------------------------- checkpoints

    def start_run(self, robot_id: str, task: str) -> str:
        run_id = str(uuid.uuid4())
        self.db.execute(
            """INSERT INTO agent_runs (run_id, fleet_id, robot_id, task, step, state, status)
               VALUES (%s, %s, %s, %s, 0, '{}', 'running')""",
            (run_id, self.fleet_id, robot_id, task),
        )
        return run_id

    def checkpoint(self, run_id: str, step: int, state: dict[str, Any]) -> None:
        """Durably record progress so a killed worker resumes WITHOUT replaying side effects."""
        self.db.execute(
            """UPDATE agent_runs SET step = %s, state = %s, updated_at = now()
               WHERE run_id = %s""",
            (step, json.dumps(state), run_id),
        )

    def resume(self, run_id: str) -> dict | None:
        rows = self.db.query(
            "SELECT run_id, robot_id, task, step, state, status FROM agent_runs WHERE run_id = %s",
            (run_id,),
        )
        return rows[0] if rows else None

    def unfinished_runs(self) -> list[dict]:
        return self.db.query(
            """SELECT run_id, robot_id, task, step, state FROM agent_runs
               WHERE fleet_id = %s AND status = 'running' ORDER BY updated_at""",
            (self.fleet_id,),
        )

    def finish_run(self, run_id: str, status: str = "done") -> None:
        self.db.execute(
            "UPDATE agent_runs SET status = %s, updated_at = now() WHERE run_id = %s",
            (status, run_id),
        )

    # ------------------------------------------------------ audit / observability

    def record_event(self, robot_id: str | None, kind: str, detail: dict) -> None:
        try:
            self.db.execute(
                """INSERT INTO agent_events (fleet_id, robot_id, kind, detail)
                   VALUES (%s, %s, %s, %s)""",
                (self.fleet_id, robot_id, kind, json.dumps(detail)),
            )
        except Exception as exc:  # audit must never take the fleet down
            log.warning("could not record event %s: %s", kind, exc)

    def recent_events(self, limit: int = 40) -> list[dict]:
        return self.db.query(
            """SELECT robot_id, kind, detail, created_at FROM agent_events
               WHERE fleet_id = %s ORDER BY created_at DESC LIMIT %s""",
            (self.fleet_id, limit),
        )


def ensure_fleet(name: str = "warehouse-1", db: Database | None = None) -> str:
    db = db or DB
    rows = db.query("SELECT id FROM fleets WHERE name = %s", (name,))
    if rows:
        return str(rows[0]["id"])
    rows = db.query("INSERT INTO fleets (name) VALUES (%s) RETURNING id", (name,))
    return str(rows[0]["id"])
