# FleetMem — Build Status

**Last updated:** 2026-08-18 19:35 UTC · **Submission deadline:** 2026-08-18 21:00 UTC
(read from the system clock, not estimated)

---

## What FleetMem is

Agentic memory for autonomous robot fleets, on CockroachDB + AWS.

A fleet of warehouse robots shares **one** CockroachDB memory layer. Memory is what makes
each agent competent (it recalls what the fleet has learned) and what makes it **safe**
(two agents cannot take the same irreversible physical action).

> **The thesis:** in a traditional app a lost write shows a stale page. For an agent, memory
> is the input to an *action* — so a lost or racy write means it does the irreversible thing
> **twice**. Two robots claiming one dock is a collision, not a duplicate email.

### Why this domain
[AWS discontinued RoboMaker on 2025-09-10](https://www.therobotreport.com/aws-robomaker-shuts-down-after-failing-to-gain-traction/)
— console and APIs gone, users redirected to plain AWS Batch. Meanwhile robot fleets still
coordinate through ROS 2 DDS (ephemeral), rosbag files, and per-robot SQLite; RMF prevents
resource conflicts **in process memory**, so a fleet-manager crash evaporates every claim.
That is a verified gap in *both* sponsors' stacks, in a sector that is textbook CockroachDB
(partition-prone, irreversible actions, no acceptable maintenance window) and has never
heard the pitch.

---

## ✅ BUILT AND VERIFIED

### Memory layer — `fleetmem/` (1,585 lines total)

| Module | Lines | What it does |
|---|---:|---|
| `memory.py` | 218 | Claims, semantic recall, checkpoints, audit — the core thesis |
| `sim.py` | 212 | Warehouse simulation; **writes to DB on events only, not per frame** |
| `agent.py` | 201 | Robot agent: recall → decide → claim → act, with Bedrock + local fallback |
| `api.py` | 146 | FastAPI + WebSocket + honest `/healthz` |
| `embeddings.py` | 125 | Bedrock Titan v2 (1024-dim) with deterministic local fallback |
| `db.py` | 122 | Connection handling, `40001` retry with backoff, typed outage errors |
| `config.py` | 114 | Component-or-DSN config resolution, secret redaction |
| `schema.sql` | 93 | Tables, the partial unique index, the vector index |
| `errors.py` | 32 | Typed errors — "absent" never collapses into "broken" |
| `web/index.html` | 225 | Live canvas UI |

### Empirically proven (not asserted)

| Property | Evidence |
|---|---|
| **Vector index w/ fleet prefix** | `VECTOR INDEX (fleet_id, embedding vector_cosine_ops)` — survives `SHOW CREATE TABLE` round-trip |
| **One holder per resource** | Barrier-synchronised race: 1 grant, 1 `23505` rejection, **1 row** |
| **The constraint is what does it** | Known-bad case with index dropped → **2 holders = collision** |
| **Shared semantic memory** | R1's lesson retrieved by another robot, distance 0.3489 vs 0.96 unrelated |
| **Durable checkpoints** | Worker killed mid-task resumes at step 3, side effects not replayed |
| **Release + re-claim** | Constraint permits reuse, forbids double-holding |
| **Runs on CockroachDB Cloud** | v26.2.5, London `aws-eu-west-2`, ~1.0s cold connect |
| **Vector index on Cloud Basic** | `SET CLUSTER SETTING` accepted **and** `CREATE ... VECTOR INDEX` succeeded |

### Design decisions worth defending

1. **No dual-write.** The lesson row and its embedding are written in **one transaction**.
   A separate vector store cannot close that gap, and its drift is silent.
2. **The `UNIQUE ... WHERE released_at IS NULL` index is an agent-safety mechanism**, not a
   schema detail. It is the only reason two racing agents cannot both act.
3. **`23505` not `40001` matters.** The loser gets a *deterministic* rejection carrying the
   current holder, so it **re-routes** instead of retrying into a dock that will never free.
   A generic "retry on DB error" wrapper would have made this an infinite loop.
4. **Tenant isolation is the vector index prefix**, not a `WHERE` clause a refactor can drop.
5. **Failure modes are typed.** `MemoryBackendError` ≠ "no rows". An agent that cannot tell
   an outage from an empty result will drive into a dock whose claim it merely failed to read.
6. **Provider provenance on every vector.** Mixing Titan and fallback vectors in one index
   would still compute distances and still look plausible — so each row records its embedder.
7. **RU-aware.** The simulation animates at 10fps but writes only on decisions, so a metered
   Cloud cluster survives weeks of judging.

---

## ⏳ PENDING

### Blocking the submission
| # | Item | Consequence |
|---|---|---|
| ~~P1~~ | ~~AWS credentials~~ ✅ **RESOLVED** — account 835863670059. S3, Titan v2 and Nova Lite all invoking. | Stage One requirement satisfied |
| ~~P2~~ | ~~Public repo~~ ✅ **https://github.com/aswin-giridhar/fleetmem** (Apache-2.0) | Done |
| ~~P3~~ | Functional demo URL | ✅ **https://18-237-2-184.nip.io/** — TLS valid to Nov 16 |
| **P4** | Video < 3 min, public on YouTube/Vimeo | Hard requirement; must show the memory layer at work |

### Built but not yet wired
| # | Item | State |
|---|---|---|
| ~~P5~~ | Bedrock Titan v2 embeddings | ✅ **LIVE** — recall distance 0.17 vs 0.86+ unrelated |
| ~~P6~~ | Bedrock reasoning | ✅ **LIVE** via Converse API + Amazon Nova Lite. Anthropic profiles unavailable in this account; Converse made the swap a config change |
| ~~P7~~ | S3 artifact store | ✅ **LIVE** — bucket created, artifacts round-tripped |
| ~~P11~~ | Node-kill resilience | ✅ **DONE** — 80 ops, 56 after the kill, **0 failures** |
| ~~NEW~~ | Claim leases | ✅ **DONE** — crashed robots no longer hold docks forever; reap-then-claim proven atomic |
| P8 | MCP server read-only service account | MCP configured in `~/.claude.json`; the read-only account is not yet created |
| P9 | ccloud CLI (3rd tool) | Not installed |
| P10 | Agent Skills (4th tool) | Not integrated |
| P11 | 3-node local cluster node-kill footage | Not recorded |
| P12 | README + architecture diagram | Not written |

---

## Hackathon requirement coverage

| Requirement | Status |
|---|---|
| ≥2 CockroachDB tools | 🟢 Vector Indexing live on Cloud; MCP configured (read-only service account pending); ccloud CLI installed |
| ≥1 AWS service | 🟢 **Bedrock (Titan v2 + Nova Lite) + S3, all invoking** |
| CockroachDB as persistent memory | 🟢 Done and proven |
| Public repo + OSS licence | 🟢 https://github.com/aswin-giridhar/fleetmem (Apache-2.0) |
| Functional demo URL | 🟢 https://18-237-2-184.nip.io/ |
| Video < 3 min | 🔴 Not recorded |
| Text description / tool write-ups | 🟡 Template ready in private notes |

---

## Honest assessment

The **memory layer — the thing the first and heaviest judging criterion asks about — is
built, running against CockroachDB Cloud, and empirically verified including the negative
case.** That is the hard part and it is done.

What remains is mostly *packaging* (repo, licence, README, video) plus one genuine blocker:
**AWS credentials**. Everything AWS-shaped is written and unexecuted. If credentials do not
arrive, the fastest compliant path is **S3** or **Lambda** — neither needs Bedrock's separate
per-model access grant, and either satisfies the ≥1-AWS-service requirement.
