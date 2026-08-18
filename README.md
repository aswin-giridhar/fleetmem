# FleetMem

**Agentic memory for autonomous robot fleets — CockroachDB as the system of record, on AWS.**

A fleet of warehouse robots shares **one** CockroachDB memory layer. Memory is what makes
each agent competent — it recalls what the fleet has learned — and what makes it **safe**:
two agents cannot take the same irreversible physical action.

> In a traditional app, a lost write shows someone a stale page.
> For an agent, memory is the input to an **action** — so a lost or racy write means it does
> the irreversible thing **twice**.
> Two robots claiming one dock is a **collision**, not a duplicate email.

---

## Why robots

[AWS discontinued RoboMaker on 2025-09-10](https://www.therobotreport.com/aws-robomaker-shuts-down-after-failing-to-gain-traction/)
— console and APIs retired, users redirected to plain AWS Batch. Meanwhile robot fleets
still coordinate through ROS 2 DDS (ephemeral pub/sub), rosbag files and per-robot SQLite.
[RMF](https://osrf.github.io/ros2multirobotbook/) prevents resource conflicts **in process
memory**, so a fleet-manager crash evaporates every claim.

A warehouse fleet is a partition-prone distributed system whose agents take irreversible
physical actions and cannot accept a maintenance window. That is textbook CockroachDB, in a
sector that has never heard the pitch.

---

## The three things the memory layer does

### 1. Exactly one robot may hold a physical resource

```sql
CREATE UNIQUE INDEX one_holder_per_resource
    ON resource_claims (fleet_id, resource_id) WHERE released_at IS NULL;
```

A **partial unique index**. Resources can be claimed and released repeatedly, but at most one
live claim exists at any instant — enforced by the database, not by agent goodwill.

The losing agent receives a deterministic `23505` **carrying the current holder**, so it
**re-routes**. That matters: `23505` is not `40001`. A serialization failure means *try
again*; a unique violation means *someone else has it and always will until they release*.
A generic "retry on any DB error" wrapper would spin forever against a dock that never frees.

**Proven both ways** — with the constraint, one holder; with it dropped, two holders and a
collision. A gate that nothing can fail is not a safety mechanism.

### 2. One robot's lesson becomes every robot's knowledge

```sql
CREATE TABLE fleet_memory (
    ...,
    embedding VECTOR(1024),
    VECTOR INDEX mem_recall (fleet_id, embedding vector_cosine_ops)
);
```

Fleet isolation is the **prefix column of the vector index**, not a `WHERE` clause a future
refactor can drop. A bug in application code cannot leak a memory across fleets.

The lesson row **and** its embedding are written in **one transaction**. There is no window
where the row exists and the vector does not. A separate vector store cannot offer this, and
the drift it causes is silent — distances still compute, answers still look plausible.

### 3. A crashed robot does not hold a dock forever

Every claim carries a **lease** that a live robot renews by heartbeat. The unique index
predicate cannot test expiry — `now()` is not immutable, so it cannot appear in an index
predicate — so expired claims are reaped **inside the same serializable transaction** as the
next claim attempt. Reap-then-claim is therefore atomic: two robots racing for a resource
whose holder has crashed still produce exactly one winner.

### 4. A killed worker resumes without replaying side effects

`agent_runs` checkpoints each step durably, so a worker that dies mid-task resumes where it
stopped rather than repeating physical actions it already performed.

---

## Architecture

```
   alert / task
        │
        ▼
   ┌─────────────┐     recall      ┌──────────────────────────────┐
   │ RobotAgent  │ ───────────────▶│      CockroachDB Cloud       │
   │             │                 │      (London, eu-west-2)     │
   │ recall      │◀────────────────│                              │
   │   ↓         │   lessons       │  fleet_memory   VECTOR INDEX │
   │ decide      │                 │  resource_claims  UNIQUE ix  │
   │   ↓         │     claim       │  agent_runs     checkpoints  │
   │ claim ──────┼────────────────▶│  agent_events   audit trail  │
   │   ↓         │  23505 / grant  └──────────────────────────────┘
   │ act         │
   └─────────────┘
        │                    ┌──────────────────────┐
        └───────────────────▶│ Amazon Bedrock       │
             embed + reason  │ Titan v2 · Claude    │
                             └──────────────────────┘
```

---

## CockroachDB tools used

| Tool | What the agent actually does with it |
|---|---|
| **Distributed Vector Indexing** | Every lesson a robot learns is embedded (1024-dim) and written **in the same transaction as its row**. Agents recall fleet-wide experience with `ORDER BY embedding <=> $1` before acting, and adapt — approaching slowly at a location another robot reported as hazardous. `VECTOR INDEX (fleet_id, embedding vector_cosine_ops)` puts tenant isolation inside the index. |
| **Cloud Managed MCP Server** | The introspection surface for the agent and for operators — `select_query`, `explain_query`, `get_table_schema`, `show_running_queries` against the live cluster. Deliberately scoped to a **read-only** service account: the docs confirm MCP is *not* read-only by default, so least privilege here is a design decision, not a default. Writes flow through application code that owns the transaction boundary. |

## AWS services used

| Service | Role |
|---|---|
| **Amazon Bedrock — Titan Text Embeddings V2** | Every lesson a robot learns is embedded (1024-dim) and stored in CockroachDB in the same transaction as its row. Measurably better than the local fallback: the relevant lesson sits at distance **0.17** vs **0.86–0.92** for unrelated memories. |
| **Amazon Bedrock — Amazon Nova Lite (Converse API)** | Agent planning. Given the task, the fleet's recalled lessons and which resources others hold, it picks a target and a speed. Real output: *"To avoid the wet floor near dock-1 and the risk of pallet slipping at dock-3"* → chose dock-2 at reduced speed. Uses the **provider-agnostic Converse API**, so Nova / Claude / Llama / Mistral are a config change, with automatic fallback between them. |
| **Amazon S3** | Bulk incident-report artifacts. The artifact is written **before** the memory row, so a lesson can never reference an object that was never stored. CockroachDB keeps the searchable memory and the `s3://` URI pointing back to the evidence. |

Without AWS credentials the app **still runs**, on a deterministic local embedder and policy
planner, and `/healthz` reports exactly which provider is live — probed by a real call, not
read from config. The safety properties are database properties, not model properties.

---

## Run it

```bash
# 1. a local CockroachDB (or point at your Cloud cluster — see .env.example)
docker run -d --name crdb -p 26257:26257 -p 8080:8080 \
  cockroachdb/cockroach:latest start-single-node --insecure

# 2. configure
cp .env.example .env      # edit if using CockroachDB Cloud

# 3. install + create schema
uv sync
uv run python scripts/reset_db.py

# 4. verify the memory layer end to end
uv run python scripts/verify_memory.py

# 5. run
uv run uvicorn fleetmem.api:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

### What you should see

`scripts/verify_memory.py` and `scripts/verify_leases.py` assert the core properties and
fail loudly if any regress:

```
1. CLAIM RACE     R1: DENIED (holder is R2 -> re-route) / R2: GRANTED   -> 1 live claim
2. SHARED MEMORY  d=0.3489 [R1] pallet at bay 12 slips when lifted too fast
3. CHECKPOINT     resumes at step 3, side effects not replayed
4. RELEASE        constraint permits reuse, forbids double-holding
ALL CHECKS PASSED

$ uv run python scripts/verify_leases.py
1. LIVE LEASE      R2 denied while R1's lease is live
2. HEARTBEAT       a renewing robot keeps its dock
3. CRASHED ROBOT   lease lapses, R7 reclaims the abandoned dock
4. HARD CASE       two robots race for an EXPIRED claim -> still exactly one winner
ALL LEASE CHECKS PASSED
```

In the UI, **⚡ Race R1 + R2 for dock-3** launches two agents at one dock simultaneously.
One wins; the other reads the winner's row and re-routes.

---

## Resilience: measured, not claimed

```bash
./infra/cluster3.sh up                       # local 3-node cluster
uv run python scripts/verify_resilience.py   # kills a node mid-workload
```

A continuous claim/release workload runs while one node is killed with `docker kill`:

```
t= 8s  *** docker kill crdb2 ***
...
operations total          : 80
succeeded                 : 80
failed                    : 0
operations after the kill : 56
VERDICT: PASS - the fleet's memory survived losing a node with zero failed writes
```

It kills a node the client is **not** connected to, and says so — losing the coordinating
node is a different (also survivable) scenario, and conflating them would overstate the
result. It runs locally because a managed Basic cluster cannot have a node killed, and
implying otherwise on camera would be dishonest.

## Production considerations

- **Serialization errors are normal.** CockroachDB returns `40001` under contention by
  design; `db.py` retries with exponential backoff. A client that doesn't looks flaky while
  the database is working correctly.
- **Absent ≠ broken.** `MemoryBackendError` is raised on backend failure and never
  collapses into an empty result. An agent that cannot tell an outage from "no claims" will
  drive into an occupied dock.
- **Secrets never logged.** DSNs are redacted before they reach logs or `/healthz`.
- **Honest health.** `/healthz` reports the *resolved* providers, probed by construction —
  not the configured model names, which say nothing about whether credentials exist.
- **Metered-cluster aware.** The simulation animates at 10fps but writes only on decisions.
- **Vector index caveats respected.** Index created before seeding (backfill blocks writes on
  a non-empty table); vectors inserted individually (batching degrades performance);
  `IMPORT INTO` unsupported on tables with vector indexes, so seeding uses `INSERT`.

## Status

See [STATUS.md](STATUS.md) for what is built, what is verified, and what is pending.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
