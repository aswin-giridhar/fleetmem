# FleetMem — What's Good, What Could Be Better

Round 2, written 2026-08-18 18:15 UTC, after leases, node-kill, live AWS and the 3D view.
Round 1's assessment is superseded; where an item has since been *done* it is marked.

---

## What is genuinely good

### 1. The core claim is proven, including the negative case ✅
Two barrier-synchronised agents race for one dock; exactly one wins. **Dropping the index
reproduces the collision** (2 holders). A gate nothing can fail is a bug in a safety
costume — this one demonstrably separates.

### 2. It reframes a database property as an agent-safety mechanism
> For an agent, memory is the input to an **action**. A lost or racy write doesn't show a
> stale page — it makes the agent do the irreversible thing **twice**.

Serializable isolation stops being "a database feature" and becomes "the reason two robots
don't collide". That is the answer to *"insight into what makes agentic systems different"*.

### 3. `23505` vs `40001` — and now leases ✅
A unique violation is **deterministic** (someone holds it, and will until release); a
serialization failure is **transient** (retry). Most code lumps both into "DB error, retry"
and spins forever. FleetMem re-routes on the former, retries the latter.

Leases complete the picture: the unique index predicate *cannot* test expiry, because
`now()` is not immutable and cannot appear in an index predicate. So expired claims are
reaped **inside the same serializable transaction** as the next claim. Reap-then-claim is
atomic, and `verify_leases.py` proves the hard case — two robots racing for a **crashed**
robot's dock still yield exactly one winner.

### 4. No dual-write
Lesson row and embedding commit together. Every Pinecone-plus-Postgres competitor has a
window where one exists without the other, and the drift is **silent**.

### 5. Resilience is measured, not claimed ✅
80 operations, 56 after `docker kill`, **0 failures**. And it kills a node the client is
*not* connected to and says so — the honest framing, not the flattering one.

### 6. The system does not report falsely ✅
Three separate false-reporting bugs were found and fixed:
- the header showed `reasoning: bedrock` while running the local fallback;
- the probe then "verified" Bedrock by calling **STS**, which succeeds with credentials that
  have no Bedrock access at all — so it lied more convincingly;
- a race that failed entirely returned **HTTP 200 with an empty list**, because exceptions
  in worker threads never reach the caller.

All three shared one root cause: *checking something adjacent to the thing being claimed.*
The probe now performs a real inference call.

### 7. Provider-agnostic reasoning ✅
Anthropic inference profiles turned out to be unavailable in this AWS account. Because the
reasoner moved to the **Converse API**, the fix was a config value, not a rewrite — and
`FALLBACK_MODELS` degrades to another provider rather than to no agent.

---

## What could be improved — ranked by value per hour

### 🔴 Blocking the submission
| # | Item | Note |
|---|---|---|
| **B1** | **Demo URL** | Needs one IAM policy (`AmazonEC2FullAccess`). `infra/deploy_ec2.sh` is written and waiting. |
| **B2** | **Video < 3 min** | Everything it must show now exists and works. |
| **B3** | **Read-only MCP service account** | Console action; makes "used safely" concrete. |

### 🟡 High value if time remains
| # | Idea | Why |
|---|---|---|
| **I1** | **Time-travel audit** via `AS OF SYSTEM TIME` | *"What did the fleet believe at 14:32, and why did R3 act on it?"* CockroachDB gives this nearly free and it is a superb answer to agent auditability. ~45 min. |
| **I2** | **Load evidence** | The criteria say "at real scale" and nobody has run 500 robots. A short script producing a claims/sec figure is cheap and quotable. ~30 min. |
| **I3** | **Contradiction detection** | Two robots report *opposite* lessons about one location. Which wins — recency, corroboration count, measured outcome? A real agentic-memory problem with no standard answer. |
| **I4** | **Memory consolidation / decay** | Agent memory grows unboundedly and retrieval quality collapses. Merging near-duplicates in one transaction is genuinely novel — nobody demos *forgetting*. |
| **I5** | **Multi-region** | "Globally distributed" is CockroachDB's headline and the demo is single-region. |

### 🟢 Bigger, but the most valuable direction
| # | Idea | Why |
|---|---|---|
| **I6** | **Real ROS 2 bridge** | A thin `rclpy` node on real topics turns "simulated fleet" into "drop-in for an actual ROS stack" — the single biggest credibility jump available, and it directly addresses the main weakness below. |
| **I7** | **Learned-from-outcome memory** | Lessons are currently *asserted*. Record whether acting on one actually helped, and weight recall by measured usefulness rather than cosine distance alone. Turns memory from storage into learning. |
| **I8** | **Priority / preemption** | A robot at 4% battery should preempt a routine delivery for a charger, without losing the exactly-one guarantee. |

---

## Weaknesses I would raise if I were judging

1. **It is a simulation, not real robots.** The honest mitigation is to say so plainly on
   camera — the *memory layer* is real, running on managed CockroachDB, and only the robot
   bodies are simulated. I6 is the real fix.
2. **Single region.** The global-distribution claim is currently untested here.
3. **No load evidence.** "At real scale" is in the criteria and unaddressed (I2).
4. **The agent's reasoning is shallow.** Nova picks a dock and a speed. Genuinely
   interesting agentic behaviour — negotiation, preemption, planning around *predicted*
   contention — is not there yet.
5. ~~Claims never expire~~ ✅ fixed by leases.
6. ~~AWS is stubbed~~ ✅ fixed — Titan, Nova and S3 all live.

---

## If I had one more hour
**I2 (load evidence) + B3 (read-only MCP account).** Both are quick, and each directly
answers a criterion currently supported by assertion rather than measurement.

## If I had one more day
**I6 (ROS 2 bridge) + I1 (time-travel audit) + I7 (outcome-weighted memory).**
The first makes it real, the second makes it auditable, the third makes it actually learn.

## The one-sentence version
The memory layer is genuinely production-shaped and the safety argument is proven rather
than asserted; the remaining weakness is that it is a *simulated* fleet, and the highest-value
next step is making it drive a real one.
