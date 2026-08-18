# FleetMem — Assessment, Round 3

Written 2026-08-18 after the build was complete and deployed, and after researching how this
problem is actually solved in industry. Rounds 1 and 2 are superseded; most of their
high-value items are now built.

---

## Is the idea good? — what the research says

Three findings from industry and current agent-memory research, and what each means for us.

### 1. The architecture matches what production agent memory is independently converging on ✅

A 2026 survey of agent-memory systems lists what production actually requires: *"vector
search capability for semantic retrieval, SQL or structured queries for history and
metadata, **ACID transactions if multiple agents share state**, and scalability as your
memory corpus grows."*

That is a description of CockroachDB, arrived at from the agent side rather than the
database side. The same surveys classify memory systems as **vector-only** (fast, weak on
temporal/relational questions) or **vector + knowledge graph** (better for "who owns what"
and "what changed when").

FleetMem is a third shape those taxonomies mostly omit: **vector + transactional
relational**. "Who owns what" is not inferred from a graph — it is a `UNIQUE` constraint
that is *enforced*, not merely recorded. For a fleet taking physical actions, an enforced
answer beats a queryable one.

### 2. Memory failures are the top production problem — and mostly *silent* ✅

Reporting on 2025 deployments found memory-related failures were **the most frequently
reported category of reliability issue**, and that *"AI agent memory remains among the most
common points of silent failure."* Named failure modes:

| Named failure mode | FleetMem's position |
|---|---|
| **Multi-agent contamination** — "losing track of who said what" | ✅ Every lesson carries `robot_id`, and every vector records the embedder that produced it |
| **Stale memory / staleness detection** (called an open problem) | ❌ **Not addressed.** No TTL, decay, or supersession on lessons |
| **Retrieval beyond similarity** — needs temporal metadata, reranking, lifecycle | 🟡 Partial: timestamps and locations exist; no reranking or lifecycle |
| **Silent degradation** | ✅ Directly targeted — three false-reporting bugs found and fixed during the build |

### 3. The domain is moving this way, fast ✅

Amazon shipped **DeepFleet** (July 2025), a generative-AI system coordinating robot routes,
reporting ~10% higher fleet speed. Industry commentary is blunt that *"fleet management is
where AMR deployments either scale to deliver ROI or collapse under coordination
complexity."* The problem FleetMem targets is the one the sector says decides outcomes.

---

## What is commonly missed — and what we should take from it

### 🔴 Fencing tokens — the real gap, and the most valuable thing to build next

The classic distributed-locking literature is explicit that **a lease alone is not
sufficient**. A process that pauses — GC, scheduler starvation, a network partition — can
resume *after* its lease expired and still act. The standard remedy is a **fencing token**:
a monotonically increasing number issued with each grant, which the protected resource
validates, rejecting any write carrying a token lower than the highest it has seen. Chubby,
ZooKeeper, etcd, Kubernetes, HDFS and Cassandra all do this.

**FleetMem now implements this** (see `scripts/verify_fencing.py`). Before this round it
had leases but no token: A robot paused mid-motion can wake after its
lease lapsed while another robot legitimately holds the dock — and nothing stops the first
robot's *actuator*. The lease bounds the claim; it does not bound the machine.

✅ **BUILT.** `resource_claims.epoch` is allocated inside the same serializable transaction
as the claim, so it is monotonic and never reused. `memory.act()` authorises every
irreversible action against it and raises `StaleFenceError` when superseded. Verified:
R1 pauses past its lease, R2 takes the dock at a higher epoch, R1's act is rejected —
*"stale fence on dock-f: presented epoch 1, current is 2"*.

### 🟠 Bi-temporal memory — event time vs ingestion time

Zep's temporal knowledge graph records **two** timestamps for every fact: *event time*
(when it actually happened) and *ingestion time* (when the system learned it). FleetMem
records only `created_at`, which conflates them.

For a fleet this is not academic. "The floor near dock-1 is wet on rainy mornings" is a
*recurring condition*, not an event at 08:14. And a lesson about a bay that has since been
reconfigured should be retrievable-but-superseded, not silently authoritative. Two columns
and a filter would let an agent ask *"what did the fleet believe on Tuesday, and why did it
act that way?"* — which is also the audit question below.

### 🟠 Regulatory audit trail — an unclaimed strength we already have

AMRs fall under **ISO 3691-4** internationally and **ANSI/RIA R15.08** in the US. These
require documented safety functions, performance levels, defined operational zones, and
technical files supporting acceptance testing and incident reporting.

FleetMem's `agent_events` table already records every decision, the memories recalled to
make it, and which model produced it. **That is an incident-reconstruction record for an
autonomous system** — and the submission currently sells it as "observability", which
undersells it considerably. Reframing costs nothing and lands directly on Real-World Impact.

### 🟡 Deadlock, not just collision

Industry fleet managers *"sequence movements at intersections to avoid deadlock"*. FleetMem
prevents two robots holding one resource, but not the cycle where A holds what B needs while
B holds what A needs. A cycle detection query over `resource_claims` is a natural extension
and a genuinely different failure class from the one we solve.

---

## Honest scorecard

| Criterion | Assessment |
|---|---|
| **Agentic Memory Design** | **Strong.** Vector + transactional in one system, no dual-write, isolation in the index prefix, leases, checkpoints. Proven against managed CockroachDB, including the negative case. |
| **Technical Implementation** | **Strong**, and the bug history helps rather than hurts: three false-reporting bugs and a retry-swallowing regression were found *and fixed*, each recorded with its root cause. |
| **Real-World Impact** | **Good, undersold.** The ISO 3691-4 audit angle is sitting there unused. |
| **Production Readiness** | **Strong.** Least-privilege MCP, typed failures, pooling (1065ms → 20ms), node-kill measured, hardened deployment, and fencing tokens closing the lease-alone gap. |
| **Creativity & Originality** | **Strong.** AWS retired RoboMaker; robot fleets still coordinate through ephemeral in-process state. Nobody else will bring a database to this fight. |

## Biggest remaining weaknesses, in order

1. **Simulated robots.** Say it first; a ROS 2 bridge is the real fix.
2. **No staleness handling.** A named open problem in the field, unaddressed here.
3. **Single region.** "Globally distributed" is currently untested.
4. **No load evidence.** "At real scale" appears in the criteria; the largest test run was six concurrent claimants.

## If there is time for exactly one more thing
**Bi-temporal memory** — separate event time from ingestion time on `fleet_memory`. Two
columns and a filter, and it unlocks both staleness handling (weakness 2) and the audit
question ISO 3691-4 implies: *"what did the fleet believe on Tuesday, and why did it act
that way?"*
