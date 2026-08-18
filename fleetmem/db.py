"""CockroachDB access layer.

Everything that touches the database goes through here so that two properties hold
everywhere rather than in most places:

  * serialization failures (SQLSTATE 40001) are retried with backoff — CockroachDB returns
    these BY DESIGN under contention, and a client that does not retry looks flaky when the
    database is in fact working correctly;
  * a backend outage raises MemoryBackendError and never returns an empty result set. An
    agent that cannot tell "no memories matched" from "the database is unreachable" will
    happily drive into a dock it has simply failed to read the claim for.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from .config import CONFIG
from .errors import MemoryBackendError

log = logging.getLogger("fleetmem.db")

RETRYABLE = {"40001", "40P01"}      # serialization failure, deadlock detected
MAX_ATTEMPTS = 5
BASE_BACKOFF = 0.05


class Database:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or CONFIG.dsn

    @contextmanager
    def connect(self, autocommit: bool = True) -> Iterator[psycopg.Connection]:
        try:
            conn = psycopg.connect(self.dsn, autocommit=autocommit, row_factory=dict_row)
        except psycopg.Error as exc:
            # Connection-level failure is an outage, never "no data".
            raise MemoryBackendError(f"cannot reach CockroachDB: {exc}") from exc
        try:
            yield conn
        finally:
            conn.close()

    def run_in_txn(self, fn, *, max_attempts: int = MAX_ATTEMPTS) -> Any:
        """Run fn(cursor) in a transaction, retrying only genuine serialization conflicts.

        fn must be idempotent with respect to its own retries: it may be called more than
        once. Anything non-retryable propagates immediately rather than being smothered.
        """
        last: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                with self.connect(autocommit=False) as conn:
                    with conn.cursor() as cur:
                        result = fn(cur)
                    conn.commit()
                    if attempt > 1:
                        log.info("txn succeeded on attempt %d", attempt)
                    return result
            except psycopg.Error as exc:
                code = getattr(exc, "sqlstate", None)
                if code in RETRYABLE and attempt < max_attempts:
                    delay = BASE_BACKOFF * (2 ** (attempt - 1))
                    log.warning("SQLSTATE %s, retrying in %.0fms (attempt %d/%d)",
                                code, delay * 1000, attempt, max_attempts)
                    time.sleep(delay)
                    last = exc
                    continue
                raise
        raise MemoryBackendError(f"transaction failed after {max_attempts} attempts: {last}")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def apply_schema(self) -> None:
        sql = (Path(__file__).parent / "schema.sql").read_text()
        with self.connect() as conn:
            with conn.cursor() as cur:
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    try:
                        cur.execute(stmt)
                    except psycopg.Error as exc:
                        # A managed cluster may refuse SET CLUSTER SETTING. That is worth
                        # surfacing loudly rather than failing the whole migration: the
                        # vector index may already be enabled by the platform.
                        if "feature.vector_index" in stmt:
                            log.warning(
                                "could not set feature.vector_index.enabled (%s). "
                                "If CREATE ... VECTOR INDEX below fails, this is why.", exc)
                            continue
                        raise

    def health(self) -> dict:
        """Used by /healthz. Distinguishes reachable-and-working from everything else."""
        started = time.perf_counter()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version() AS v, current_database() AS db")
                row = cur.fetchone()
        return {
            "ok": True,
            "version": row["v"],
            "database": row["db"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


DB = Database()
