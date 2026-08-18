# FleetMem

**Agentic memory for autonomous robot fleets — CockroachDB as the system of record, on AWS.**

A fleet of warehouse robots shares **one** CockroachDB memory layer. Memory is what makes
each agent competent — it recalls what the fleet has learned — and what makes it **safe**:
two agents cannot take the same irreversible physical action.

**Live demo → https://18-237-2-184.nip.io/**  ·  **Source → https://github.com/aswin-giridhar/fleetmem**

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

### 4. A paused robot cannot act on a lease it has already lost

A lease bounds the **claim**; it does not bound the **machine**. A robot paused by GC or a
network partition can wake after its lease lapsed and still be physically moving — the
classic gap that leases alone never close.

Every grant therefore issues a monotonically increasing **fencing token**, and every
irreversible act is authorised against it:

```python
grant = memory.claim("dock-3", "R1")     # -> {"epoch": 7, ...}
memory.act("dock-3", "R1", grant["epoch"])   # raises StaleFenceError if superseded
```

`scripts/verify_fencing.py` asserts the case that matters: R1 pauses past its lease, R2
legitimately takes the dock at a higher epoch, and R1's attempt to act is rejected —
*"stale fence on dock-f: presented epoch 1, current is 2"*. This is the same mechanism
Chubby, ZooKeeper, etcd and Kubernetes use.

### 5. Memory has a lifecycle — it is not true forever

Staleness is repeatedly named as one of the open problems in production agent memory:
outdated preferences, resolved tasks and superseded facts quietly degrade retrieval. Every
lesson therefore carries **two clocks and a lifecycle**:

| Column | Meaning |
|---|---|
| `observed_at` | when the condition actually held (**event time**) |
| `created_at` | when the fleet learned it (**ingestion time**) |
| `valid_until` | for time-bounded facts — expires on its own |
| `superseded_by` / `superseded_at` | retired by a correction, **not deleted** |
| `recurrence` | marks a recurring condition, which does not decay like an event |

```python
memory.supersede(old_id, "R5", "dock-4 lane is blocked by racking since the refit")
memory.recall("dock-4 lane")                       # returns only the correction
memory.recall("dock-4 lane", as_of=yesterday)      # returns what the fleet believed then
```

That last line is the point. **"What did the fleet believe on Tuesday, and why did it act
that way?"** is exactly what incident reconstruction under **ISO 3691-4** (international)
and **ANSI/RIA R15.08** (US) requires of an autonomous system — and it is unanswerable if
event time and ingestion time are conflated, or if corrections destroy the record.

Recall also **reranks** rather than trusting cosine distance alone: a corroborated,
recently-observed lesson outranks a stale low-confidence one, while a recurring condition
is exempt from decay because it describes a pattern rather than an event.

### 6. Deadlock is a different failure from collision

The unique index stops two robots holding one resource. It does nothing about A holding what
B needs while B holds what A needs — every claim is individually valid and the fleet still
stops. `resource_waits` records what each robot is blocked on, `detect_deadlocks()` finds
cycles in the wait-for graph, and `break_deadlock()` makes the youngest claim yield and
records who yielded. Verified to detect a real cycle **and** to not fire on a plain queue.

### 7. A killed worker resumes without replaying side effects

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

## It drives real ROS 2

The robot bodies in the web demo are simulated. They do not have to be —
`ros2/fleetmem_bridge.py` is a real `rclpy` node that puts the database between a navigation
stack and the actuator:

- subscribes `/<robot>/odom` (`nav_msgs/Odometry`)
- publishes `/<robot>/cmd_vel` (`geometry_msgs/Twist`)
- calls `claim` / `act` / `renew` / `release` between them

`act(dock, robot, epoch)` runs on **every tick that commands motion**, not once at claim
time — a fencing token that is only checked at acquisition is not gating anything.

```bash
./ros2/verify_ros_bridge.sh     # builds the image, runs two converging robots
```

Observed across four runs — two robots 6 m either side of one dock, converging at equal
speed:

```
[R1] CLAIM GRANTED on ros-dock-1 epoch=11 -- actuator authorised
[R2] CLAIM DENIED  on ros-dock-1 -- held by R1; publishing zero Twist to /R2/cmd_vel
[R1] RELEASED ros-dock-1 after 8.0s dwell
[R2] CLAIM GRANTED on ros-dock-1 epoch=14
```

The denied robot's integrated pose **froze at +2.94 for eleven seconds** and resumed the
instant CockroachDB granted its claim. `fake_robot.py` integrates the `cmd_vel` it receives,
so a denial is physically observable rather than merely logged, and independent
`ros2 topic echo` subscribers captured the zero Twists rather than the bridge reporting on
itself.

**Not verified, and worth knowing:** both nodes run in one container, so cross-container DDS
discovery is untested; there is no Gazebo or TF tree; the `StaleFenceError` path is reachable
but was not exercised in these runs; and it has never been run against the Cloud cluster,
deliberately, to avoid polluting the demo database. See `ros2/README.md`.

## Deployment

The demo runs on an EC2 instance in `us-west-2`, behind Caddy with an automatic Let's
Encrypt certificate, talking to a CockroachDB Cloud Basic cluster in **London
(`aws-eu-west-2`)**. `infra/deploy_ec2.sh` provisions it end to end.

The application runs as an unprivileged user bound to `127.0.0.1:8000` under systemd
hardening; only Caddy is exposed. IMDSv2 is required with a hop limit of 1, and the service
user is firewalled away from `169.254.169.254`, so a compromised process cannot read
instance metadata or user-data.

**Connections are pooled.** Against a managed cluster a new connection costs a full TLS
handshake — measured at ~1065ms cross-region — so a connect-per-query design made every
claim pay it. With a warm pool the median query is **20ms** locally and **127ms** from
us-west-2 to London, the latter being genuine round-trip distance.

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

Five scripts assert the core properties and fail loudly if any regress —
`verify_memory.py`, `verify_leases.py`, `verify_fencing.py`,
`verify_memory_lifecycle.py`, `verify_deadlock.py` — plus `verify_load.py` and
`verify_resilience.py` for throughput and node loss:

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

$ uv run python scripts/verify_fencing.py
1. GRANT           issues a fencing token; the holder may act
2. MONOTONIC       successive grants advance the token
3. PAUSED ROBOT    stale epoch REJECTED — the actuator is fenced, not just the claim
4. CURRENT HOLDER  unaffected
5. RACE            one grant, one token
ALL FENCING CHECKS PASSED

$ uv run python scripts/verify_memory_lifecycle.py
1. TWO CLOCKS      event time and ingestion time recorded separately
2. SUPERSESSION    correction retires the old lesson without deleting it
3. TIME TRAVEL     the earlier belief is reconstructible (as_of)
4. VALIDITY        a time-bounded fact expires on its own
5. RERANKING       a recurring condition outranks a decayed one-off
ALL MEMORY LIFECYCLE CHECKS PASSED

$ uv run python scripts/verify_deadlock.py
3. DETECT          R1 -> R2 -> R1 identified from the wait-for graph
4. RESOLVE         youngest claim yields, and who yielded is recorded
5. NO FALSE ALARM  a queue behind one holder is not a deadlock
ALL DEADLOCK CHECKS PASSED
```

In the UI, **⚡ Race R1 + R2 for dock-3** launches two agents at one dock simultaneously.
One wins; the other reads the winner's row and re-routes.

---

## Load: measured, not claimed

```bash
uv run python scripts/verify_load.py 50 20     # 50 robots, 8 contended resources, 20s
```

```
claim attempts      : 1029
granted / denied    : 229 / 800
errors              : 0
THROUGHPUT          : 45 claim operations/sec
latency p50 / p95   : 772 ms / 1887 ms
invariant violations: 0
VERDICT: PASS — sustained contention, zero double-holds, zero errors
```

The invariant is asserted *continuously while the load runs*, not just at the end. A
throughput number without it would be meaningless: fast and wrong is worse than slow and
right, because a fleet that is fast and wrong collides.

This test earned its keep. The first run produced **202 escaped serialization failures**,
and the cause was the fencing implementation itself — `SELECT MAX(epoch)+1` made every
concurrent claimant read the same rows, so the lock mechanism became the main source of
contention. Moving the token to a non-transactional sequence and adding jittered backoff
took errors to zero and throughput from 18 to 45 ops/sec.

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
