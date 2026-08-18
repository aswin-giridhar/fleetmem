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
| **Stale memory / staleness detection** (called an open problem) | ✅ **Built.** `valid_until` expiry, supersession without deletion, and lifecycle filtering in recall |
| **Retrieval beyond similarity** — needs temporal metadata, reranking, lifecycle | ✅ **Built.** Bi-temporal columns, reranking by confidence and recency, recurring conditions exempt from decay |
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

### ✅ Bi-temporal memory — event time vs ingestion time  *(BUILT)*

Zep's temporal knowledge graph records **two** timestamps for every fact: *event time*
(when it actually happened) and *ingestion time* (when the system learned it). FleetMem
records only `created_at`, which conflates them.

For a fleet this is not academic. "The floor near dock-1 is wet on rainy mornings" is a
*recurring condition*, not an event at 08:14. And a lesson about a bay that has since been
reconfigured should be retrievable-but-superseded, not silently authoritative.

✅ **BUILT.** `observed_at` and `created_at` are now separate; `valid_until` expires
time-bounded facts; `superseded_by`/`superseded_at` retire corrections without deleting
them; `recurrence` marks patterns that should not decay. `recall(as_of=...)` reconstructs
what the fleet believed at any past moment. See `scripts/verify_memory_lifecycle.py`.

### ✅ Regulatory audit trail — now claimed, and now actually sufficient  *(BUILT)*

AMRs fall under **ISO 3691-4** internationally and **ANSI/RIA R15.08** in the US. These
require documented safety functions, performance levels, defined operational zones, and
technical files supporting acceptance testing and incident reporting.

FleetMem's `agent_events` table records every decision, the memories recalled to make it,
and which model produced it. **That is an incident-reconstruction record for an autonomous
system.** With bi-temporal memory it is now genuinely sufficient rather than merely
suggestive: you can replay not just what the agent did, but what it *believed at the time*,
including lessons since corrected. The README now frames it that way rather than as
"observability", which undersold it considerably.

### ✅ Deadlock, not just collision  *(BUILT)*

Industry fleet managers *"sequence movements at intersections to avoid deadlock"*. The
unique index prevents two robots holding one resource, but not the cycle where A holds what
B needs while B holds what A needs — every claim individually valid, fleet stopped.

✅ **BUILT.** `resource_waits` records what each robot is blocked on, `detect_deadlocks()`
finds cycles in the wait-for graph, and `break_deadlock()` makes the youngest claim yield
and records who yielded. Verified to fire on a genuine cycle **and not** on a plain queue —
a detector that cannot tell those apart is worse than none.

---

## Honest scorecard

| Criterion | Assessment |
|---|---|
| **Agentic Memory Design** | **Strong.** Vector + transactional in one system, no dual-write, isolation in the index prefix, leases, checkpoints. Proven against managed CockroachDB, including the negative case. |
| **Technical Implementation** | **Strong**, and the bug history helps rather than hurts: three false-reporting bugs and a retry-swallowing regression were found *and fixed*, each recorded with its root cause. |
| **Real-World Impact** | **Good, undersold.** The ISO 3691-4 audit angle is sitting there unused. |
| **Production Readiness** | **Strong.** Least-privilege MCP, typed failures, pooling (1065ms → 20ms), node-kill measured, hardened deployment, and fencing tokens closing the lease-alone gap. |
| **Creativity & Originality** | **Strong.** AWS retired RoboMaker; robot fleets still coordinate through ephemeral in-process state. Nobody else will bring a database to this fight. |

## What the load test taught us

Worth recording, because it inverted an assumption. The first load run (50 robots, 8
resources) leaked **202 serialization failures**, and the cause was the fencing code added
hours earlier: `SELECT MAX(epoch)+1` forced every concurrent claimant to read the same rows,
so **the locking mechanism had become the dominant source of lock contention**. Moving the
token to a non-transactional sequence and adding jittered backoff took errors to zero and
throughput from 18 to 45 ops/sec.

The general lesson: a correctness mechanism added without a load test can degrade the very
property it protects, and nothing in a functional test will reveal it.

## Biggest remaining weaknesses, in order

1. **Simulated robots.** Say it first; a ROS 2 bridge is the real fix.
   The memory layer is real; only the bodies are simulated. A ROS 2 bridge is the fix, and
   it is the single biggest credibility jump still available.
2. **Single region.** "Globally distributed" remains untested here — the cluster is London,
   the compute us-west-2, which exercises cross-region latency but not multi-region topology.
3. **Latency under contention.** p50 772ms at 50 robots is dominated by queueing on a
   32-connection pool against a remote cluster. Co-locating compute with the cluster and
   widening the pool are the obvious next moves, both unmeasured.
4. **The agent's reasoning is narrow — and this is a schema limitation, not a model one.**
   The prompt asks for exactly four fields (`target`, `speed`, `reason`, `memory_used`), so
   negotiation and preemption are impossible for *any* model, however capable. An earlier
   draft of this document blamed the model, which was wrong. We did A/B the four models
   available in the account on the real prompt, and switched Nova Lite → **Nova Pro**: it was
   both faster (1041ms vs 1312ms) and the only one that cited the memory it used. Widening
   the output schema is the actual fix.

## If there is time for exactly one more thing
**Widen the agent's output schema** so it can negotiate and preempt rather than only pick a
dock and a speed. With the ROS 2 bridge in place, the memory layer and the actuator path are
both real; the reasoning is now the narrowest part of the loop.
