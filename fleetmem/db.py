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

import atexit
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import CONFIG
from .errors import MemoryBackendError

log = logging.getLogger("fleetmem.db")

RETRYABLE = {"40001", "40P01"}      # serialization failure, deadlock detected
MAX_ATTEMPTS = 5
BASE_BACKOFF = 0.05


class Database:
    """Pooled access to CockroachDB.

    The pool matters more than it looks. Opening a connection to a managed cluster costs a
    full TLS handshake — measured at ~1.0s from us-west-2 to a London cluster — so a
    connect-per-query design makes every claim and every recall pay that toll, and the
    agent looks slow when the database is fine. Reusing warm connections removes it.
    """

    def __init__(self, dsn: str | None = None, min_size: int = 2, max_size: int = 12):
        self.dsn = dsn or CONFIG.dsn
        self._pool: ConnectionPool | None = None
        self._min, self._max = min_size, max_size

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            try:
                self._pool = ConnectionPool(
                    self.dsn, min_size=self._min, max_size=self._max,
                    kwargs={"row_factory": dict_row},
                    open=True, timeout=20,
                )
                self._pool.wait(timeout=25)
            except Exception as exc:
                self._pool = None
                raise MemoryBackendError(f"cannot reach CockroachDB: {exc}") from exc
        return self._pool

    @contextmanager
    def connect(self, autocommit: bool = True) -> Iterator[psycopg.Connection]:
        pool = self._get_pool()
        try:
            with pool.connection() as conn:
                conn.autocommit = autocommit
                yield conn
        except MemoryBackendError:
            raise
        except psycopg.OperationalError as exc:
            # CAREFUL: in psycopg3 SerializationFailure subclasses OperationalError, so a
            # naive catch here swallows CockroachDB's retryable 40001/40P01 errors and
            # reports them as an outage — disabling the retry logic in run_in_txn entirely.
            # Retryable conflicts must propagate unchanged; only genuine connection loss
            # becomes MemoryBackendError.
            if getattr(exc, "sqlstate", None) in RETRYABLE:
                raise
            raise MemoryBackendError(f"lost connection to CockroachDB: {exc}") from exc

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

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

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
        raw = (Path(__file__).parent / "schema.sql").read_text()
        # Strip line comments BEFORE splitting on ';'. A semicolon inside a comment would
        # otherwise cut a CREATE TABLE in half, and the resulting syntax error points at
        # the prose rather than the cause.
        sql = "\n".join(line.split("--")[0] if "--" in line and "'" not in line else line
                        for line in raw.splitlines())
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

# Close the pool on interpreter shutdown; otherwise psycopg warns about worker threads
# that outlive the process, which buries real output in noise.
atexit.register(DB.close)
