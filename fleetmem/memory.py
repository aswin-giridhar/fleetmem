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
            # Next fencing token for this resource. Monotonic because the read and the
            # insert happen inside one serializable transaction: a concurrent claimant
            # either sees this row or conflicts and retries, never reuses the number.
            cur.execute(
                """SELECT COALESCE(MAX(epoch), 0) + 1 AS next FROM resource_claims
                   WHERE fleet_id = %s AND resource_id = %s""",
                (self.fleet_id, resource_id),
            )
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

    # ------------------------------------------------------- semantic memory

    def remember(self, robot_id: str, lesson: str, *, kind: str = "incident",
                 location: str | None = None, confidence: float = 1.0,
                 report: dict | None = None) -> dict:
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
                     confidence, artifact_uri)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (self.fleet_id, robot_id, kind, lesson, location,
                 to_pgvector(vector), provider, confidence, artifact_uri),
            )
            return cur.fetchone()

        row = self.db.run_in_txn(_txn)
        self.record_event(robot_id, "memory_written",
                          {"lesson": lesson, "provider": provider, "location": location,
                           "artifact_uri": artifact_uri})
        log.info("remembered [%s] %s", robot_id, lesson)
        return row

    def recall(self, query: str, limit: int = 5, max_distance: float | None = None) -> list[dict]:
        """Semantic recall across the WHOLE fleet.

        The fleet_id prefix is the first column of the vector index, so isolation is
        enforced by the index itself rather than by a filter a future refactor could drop.
        """
        vector, _ = embed(query)
        rows = self.db.query(
            """
            SELECT id, robot_id, kind, lesson, location, provider, artifact_uri, created_at,
                   embedding <=> %s AS distance
            FROM fleet_memory
            WHERE fleet_id = %s
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (to_pgvector(vector), self.fleet_id, to_pgvector(vector), limit),
        )
        if max_distance is not None:
            rows = [r for r in rows if r["distance"] is not None and r["distance"] <= max_distance]
        return rows

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
