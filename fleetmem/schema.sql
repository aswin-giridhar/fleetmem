-- FleetMem schema. CockroachDB v25.2+ (developed and probed against v26.2.5).
--
-- Two design points carry the whole system:
--   1. `resource_claims.one_holder_per_resource` — a PARTIAL unique index. At most one live
--      claim per physical resource, enforced by the database rather than by agent goodwill.
--   2. `fleet_memory.mem_recall` — a VECTOR INDEX whose PREFIX column is fleet_id, so fleet
--      isolation is a property of the index, not of a WHERE clause an app bug can drop.

SET CLUSTER SETTING feature.vector_index.enabled = true;

CREATE TABLE IF NOT EXISTS fleets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        STRING NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS robots (
    id          STRING NOT NULL,
    fleet_id    UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    status      STRING NOT NULL DEFAULT 'idle',
    x           FLOAT8 NOT NULL DEFAULT 0,
    y           FLOAT8 NOT NULL DEFAULT 0,
    battery     FLOAT8 NOT NULL DEFAULT 100,
    goal        STRING,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fleet_id, id)
);

CREATE TABLE IF NOT EXISTS resources (
    id          STRING NOT NULL,
    fleet_id    UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    kind        STRING NOT NULL,            -- dock | corridor | charger
    x           FLOAT8 NOT NULL,
    y           FLOAT8 NOT NULL,
    PRIMARY KEY (fleet_id, id)
);

-- The claim ledger. Append-only: claims are released by setting released_at, never deleted,
-- so the history of who held what remains auditable.
CREATE TABLE IF NOT EXISTS resource_claims (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fleet_id        UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    resource_id     STRING NOT NULL,
    robot_id        STRING NOT NULL,
    purpose         STRING,
    -- Fencing token. A lease bounds the CLAIM, not the MACHINE. A robot
    -- paused by GC or a network partition can resume after its lease lapsed, while another
    -- robot legitimately holds the dock — and nothing stops the first robot's actuator.
    -- The standard remedy (Chubby, ZooKeeper, etcd, Kubernetes) is a monotonically
    -- increasing token issued with each grant, which the protected resource validates,
    -- rejecting any action carrying a token lower than the highest it has seen.
    epoch           INT8 NOT NULL DEFAULT 1,
    claimed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Leases. A robot that crashes must not hold a dock forever, so every claim carries an
    -- expiry that a live robot renews by heartbeat. The unique index predicate CANNOT test
    -- expiry (now() is not immutable, so it cannot appear in an index predicate) — instead
    -- expired claims are reaped inside the same transaction as the next claim attempt,
    -- which keeps "reap then claim" atomic under serializable isolation.
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '30 seconds'),
    renewed_at      TIMESTAMPTZ,
    expired         BOOL NOT NULL DEFAULT false,
    released_at     TIMESTAMPTZ,
    INDEX by_robot (fleet_id, robot_id, released_at)
);

-- THE constraint. Two agents cannot both hold one dock, however they race.
CREATE UNIQUE INDEX IF NOT EXISTS one_holder_per_resource
    ON resource_claims (fleet_id, resource_id) WHERE released_at IS NULL;

-- Semantic fleet memory. One robot's lesson becomes every robot's knowledge, and survives
-- reboot, redeploy and node loss.
CREATE TABLE IF NOT EXISTS fleet_memory (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fleet_id    UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    robot_id    STRING NOT NULL,
    kind        STRING NOT NULL DEFAULT 'incident',
    lesson      STRING NOT NULL,
    location    STRING,
    embedding   VECTOR(1024),
    provider    STRING NOT NULL DEFAULT 'unknown',  -- which embedder produced this vector
    artifact_uri STRING,                            -- s3:// URI of the full incident report
    confidence  FLOAT8 NOT NULL DEFAULT 1.0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    VECTOR INDEX mem_recall (fleet_id, embedding vector_cosine_ops)
);

-- Durable checkpoints: a killed worker resumes at its last completed step instead of
-- replaying side effects it already performed.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fleet_id    UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    robot_id    STRING NOT NULL,
    task        STRING NOT NULL,
    step        INT NOT NULL DEFAULT 0,
    state       JSONB NOT NULL DEFAULT '{}',
    status      STRING NOT NULL DEFAULT 'running',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Observability + audit. Every decision an agent takes lands here, with its reason.
CREATE TABLE IF NOT EXISTS agent_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fleet_id    UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    robot_id    STRING,
    kind        STRING NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX by_time (fleet_id, created_at DESC)
);
