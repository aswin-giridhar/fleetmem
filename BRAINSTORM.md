# FleetMem — What's Good, What Could Be Better

An honest assessment against the five judging criteria, plus enhancement ideas ranked by
value-per-hour. Written 2026-08-18.

---

## What is genuinely good

### 1. The core claim is *proven*, not asserted — including the negative case
Most submissions will say "CockroachDB guarantees consistency". This one demonstrates it:
two barrier-synchronised agents race for one dock, exactly one wins, and **dropping the
index reproduces the collision**. A gate that nothing can fail is a bug in a safety costume;
this one demonstrably separates.

### 2. It reframes a database feature as an agent-safety mechanism
The strongest idea in the project is a sentence, not a line of code:

> For an agent, memory is the input to an **action**. A lost or racy write doesn't show a
> stale page — it makes the agent do the irreversible thing **twice**.

Serializable isolation stops being "a database property" and becomes "the reason two robots
don't collide". That is the answer to *"insight into what makes agentic systems different"*.

### 3. `23505` vs `40001` is a real engineering insight
A unique violation is **deterministic**: someone holds it and will until they release. A
serialization failure is **transient**: try again. Most code lumps both into "DB error, retry"
— which spins forever against a dock that will never free. FleetMem distinguishes them and
re-routes on the former. This is the kind of detail a Sales Engineering judge notices.

### 4. No dual-write
The lesson row and its embedding commit in one transaction. Every Pinecone-plus-Postgres
competitor has a window where one exists without the other, and the drift is **silent** —
distances still compute, answers still look plausible. This is the single most defensible
technical argument in the submission.

### 5. Isolation lives in the index, not in application code
`VECTOR INDEX (fleet_id, embedding vector_cosine_ops)` means a bug in a `WHERE` clause
cannot leak one fleet's memory into another's recall. The guarantee is structural.

### 6. The system does not report falsely
`/healthz` reports providers **resolved by probe**, not configured model names — an early
version displayed "reasoning: bedrock" while running the local fallback, which was fixed
precisely because a confident wrong claim is worse than an admission of ignorance. Typed
errors keep "absent" and "broken" distinct. Every vector records which embedder produced it,
so Titan and fallback vectors can never be silently mixed into one meaningless index.

### 7. Domain choice is defensible on evidence
AWS **retired RoboMaker** (2025-09-10) and robot fleets still coordinate through ephemeral
DDS and in-process allocation. A verified gap in both sponsors' stacks, in a sector that is
textbook CockroachDB and has never heard the pitch.

---

## What could be improved — ranked by value per hour

### 🔴 High value, low effort

| # | Idea | Why it matters | Effort |
|---|---|---|---|
| **I1** | **Attach IAM permissions + a real S3 bucket** | Turns AWS from a stub into a working component; **Stage One is pass/fail on this** | 5 min (console) |
| **I2** | **Bedrock Titan embeddings live** | The fallback matches shared wording, not paraphrase. Titan makes "the floor is slippery near the loading door" recall the bay-12 lesson — a *much* better demo moment | 5 min once model access granted |
| **I3** | **Node-kill resilience footage** | 3-node local cluster, `docker kill` one node mid-race, fleet keeps going. This is the "memory that never goes down" thesis made visible | 30 min |
| **I4** | **Read-only MCP service account** | Makes "used correctly and safely" concrete: judges can point at a deliberately least-privileged account | 15 min |
| **I5** | **Architecture diagram** | Explicitly invited by the submission form; cheap scoring | 20 min |

### 🟡 High value, medium effort

| # | Idea | Why it matters | Effort |
|---|---|---|---|
| **I6** | **Memory decay / consolidation job** | Agent memory grows unboundedly and retrieval quality collapses. A background job that merges near-duplicate lessons *in one transaction* and decays stale ones is genuinely novel — nobody demos forgetting | 1–2 h |
| **I7** | **Confidence + contradiction detection** | Two robots report *contradictory* lessons about the same location. Which wins? Recency? Corroboration count? This is a real agentic-memory problem with no standard answer | 1–2 h |
| **I8** | **Claim leases with TTL** | Today a crashed robot holds its dock forever. Real fleets need `expires_at` + heartbeat renewal, so a dead robot's claim is reclaimable. **This is the most glaring production gap** | 45 min |
| **I9** | **Multi-region cluster** | "Globally distributed" is CockroachDB's headline and the demo is single-region. A second region with a follower read would show read-local/write-global | 1 h + credits |
| **I10** | **Replay / time-travel using AS OF SYSTEM TIME** | "What did the fleet believe at 14:32, and why did R3 act on it?" CockroachDB gives this almost free, and it is a *fantastic* answer to agent auditability | 45 min |

### 🟢 Strong ideas, larger effort

| # | Idea | Why it matters |
|---|---|---|
| **I11** | **Learned-from-outcome memory** | Right now lessons are asserted. Close the loop: record whether acting on a lesson *helped*, and weight recall by measured usefulness rather than by cosine distance alone. Turns memory from storage into learning |
| **I12** | **Priority + fairness in claims** | A charging-critical robot at 4% battery should preempt a routine delivery. Introduces priority into the claim protocol without losing the exactly-one guarantee |
| **I13** | **Real ROS 2 bridge** | A thin `rclpy` node publishing/subscribing real topics would move this from "simulated fleet" to "drop-in for an actual ROS stack" — the single biggest credibility jump available |
| **I14** | **Deadlock detection across claims** | Two robots each holding what the other needs. Classic, and a graph query over `resource_claims` detects it |

---

## Weaknesses I would raise if I were judging

1. **It is a simulation, not real robots.** Honest, but a judge may discount it. *I13 is the
   antidote*; failing that, say plainly on camera that the memory layer is real and only the
   robot bodies are simulated.
2. **Claims never expire** (I8). A crashed robot holds a dock forever. For a project whose
   pitch is resilience, this is the sharpest inconsistency.
3. **The local embedder is weak.** Matches shared words, not meaning. Fine as a fallback,
   poor as the thing shown on camera (I2 fixes).
4. **Single region.** The "globally distributed" claim is currently untested here (I9).
5. **No load evidence.** "At real scale" appears in the criteria; nobody has run 500 robots.
   A short load script producing a claims/sec number would be cheap and quotable.

---

## If I had exactly one more hour

**I8 (claim leases) + I3 (node-kill footage).**

Leases close the most obvious production hole and take ~45 minutes; the node-kill shot
delivers the thesis — *memory that never goes down* — as something a judge can watch rather
than read. Together they raise Production Readiness, which is the criterion where a
simulation is otherwise most vulnerable.

## If I had exactly one more day

**I13 (ROS 2 bridge) + I10 (time-travel audit) + I6 (consolidation).**
The first makes it real, the second makes it auditable, the third makes it novel.
