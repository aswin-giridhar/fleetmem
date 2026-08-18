# FleetMem ↔ ROS 2 bridge

**The database decides whether a physical actuator moves.**

The rest of FleetMem argues that a shared memory layer keeps two robots from taking the
same irreversible action. This directory stops arguing and wires it to the interfaces a
real warehouse AMR already exposes:

```
/<robot>/odom  →  [ CockroachDB: claim() · act() · renew() ]  →  /<robot>/cmd_vel
   nav_msgs/Odometry                                              geometry_msgs/Twist
```

A robot approaching a dock does not decide it may enter. It asks the fleet's memory, and a
`23505` from the partial unique index `one_holder_per_resource` becomes a **zero-velocity
Twist on the wire**. The losing robot is not merely told "no" in a log line — its wheels
stop.

This is not a simulation of ROS. It is `rclpy`, real DDS discovery, real
`nav_msgs/Odometry` and `geometry_msgs/Twist`, running on `ros:jazzy-ros-base`.

---

## Files

| File | What it is |
|---|---|
| `fleetmem_bridge.py` | The bridge node. Subscribes `/<robot>/odom`, publishes `/<robot>/cmd_vel`, with `FleetMemory.claim / act / renew / release` between them. |
| `fake_robot.py` | Closed-loop synthetic AMRs. They **integrate the `cmd_vel` they receive** and publish the resulting `odom`, so a denied robot's position physically stops changing. |
| `Dockerfile` | `ros:jazzy-ros-base` + `psycopg`. `rclpy` is not pip-installable standalone — it needs the DDS middleware — so the container is the runtime. |
| `verify_ros_bridge.sh` | Runs both nodes, captures evidence from the DB **and** from independent ROS subscribers. |

## Three different database questions gate motion

They are deliberately **not** the same check:

- **`claim()`** — *may this robot enter the dock at all?* A denial is deterministic (someone
  else holds it and will until they release), so the bridge **re-routes / stops** rather
  than spinning on retry. `23505` is not `40001`.
- **`act(dock, robot, epoch)`** — *may this robot move **right now**, on the token it was
  granted?* Checked on **every tick that commands motion**, not once at claim time. A robot
  paused by GC or a network partition wakes still believing it holds the dock; the fencing
  token is what stops its wheels. A `StaleFenceError` sends a zero Twist.
- **`renew()`** — heartbeat. Without it a long dwell would let the 90-second lease lapse and
  `act()` would (correctly) fence the robot mid-dock.

## QoS

| Topic | Profile | Why |
|---|---|---|
| `/<robot>/odom` | `qos_profile_sensor_data` (BEST_EFFORT, KEEP_LAST 5) on **both** sides | A BEST_EFFORT *subscription* is the permissive side of the requested-vs-offered contract, so it also accepts a real robot publishing RELIABLE odom. The reverse — a RELIABLE subscriber against a BEST_EFFORT publisher — discovers the topic, receives **nothing**, and raises no error anywhere. That silent mismatch is the classic ROS 2 QoS trap. |
| `/<robot>/cmd_vel` | RELIABLE, KEEP_LAST 10 (default) on **both** sides | A dropped stop command is not acceptable; a dropped odom sample is. |

Concurrency: psycopg calls block, so **all** database work happens in one timer callback on
the default single-threaded executor. A slow query delays the next control tick instead of
interleaving with itself. Odometry callbacks do no I/O at all — they only stash the pose.

---

## Run it

Prerequisites: Docker, and a local CockroachDB.

```bash
# 1. a local CockroachDB node (skip if you already have one named `crdb`)
docker run -d --name crdb -p 26257:26257 -p 8080:8080 \
  cockroachdb/cockroach:latest start-single-node --insecure

# 2. an isolated database with the FleetMem schema
docker exec crdb ./cockroach sql --insecure -e "CREATE DATABASE IF NOT EXISTS fleet_ros;"
docker exec -i crdb ./cockroach sql --insecure --database=fleet_ros < fleetmem/schema.sql

# 3. build the ROS 2 runtime
docker build -t fleetmem-ros:jazzy -f ros2/Dockerfile ros2/

# 4. run the whole thing and print the evidence
./ros2/verify_ros_bridge.sh
```

### Why a separate `fleet_ros` database

Two reasons, both about not breaking things that already work:

1. **It never touches the live Cloud cluster.** `fleetmem/config.py` only honours an explicit
   `FLEETMEM_DSN` when it does **not** contain the substring `localhost`; otherwise it falls
   through to the `CRDB_*` component variables in `.env`, which point at the deployed
   London cluster. The scripts here therefore pin the DSN to `127.0.0.1` **by IP**, which
   wins outright. The bridge logs its resolved (redacted) DSN at startup so you can see
   which database it actually hit rather than trusting a comment.
2. `scripts/reset_db.py` is not used anywhere here — nothing is dropped.

Each run also uses a **fresh timestamped fleet name**, resolved to a UUID by `ensure_fleet`.
Since the unique index is on `(fleet_id, resource_id)`, a run is fully isolated from any
other data in the database.

### Running the nodes by hand

```bash
docker run --rm --net=host -v "$PWD":/repo -w /repo \
  -e FLEETMEM_DSN="postgresql://root@127.0.0.1:26257/fleet_ros?sslmode=disable" \
  -e ROS_DOMAIN_ID=42 -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  fleetmem-ros:jazzy bash -c '
    source /opt/ros/jazzy/setup.bash
    python3 ros2/fake_robot.py --robots R1:-6.0:0.0,R2:6.0:0.0 &
    python3 ros2/fleetmem_bridge.py --robots R1,R2 --dock ros-dock-1 --fleet demo'
```

No colcon package is needed — the nodes are plain rclpy scripts run against
`/opt/ros/jazzy`. Note that ROS 2's `setup.bash` references unbound variables, so a
containing script must **not** use `set -u`.

---

## What was actually verified

Everything below was **observed**, on 2026-08-18, on `ros:jazzy-ros-base` against a local
CockroachDB v26.2.5 node. Nothing in this section is inferred.

Two robots start 6.0 m either side of `ros-dock-1` and converge at equal speed, so neither
wins by being closer.

**Bridge log — the claim race:**

```
[INFO] [fleetmem_bridge]: [R1] CLAIM GRANTED on ros-dock-1 epoch=11 -- actuator authorised
[WARN] [fleetmem_bridge]: [R2] CLAIM DENIED on ros-dock-1 -- held by R1; publishing zero Twist to /R2/cmd_vel
[WARN] [fleetmem_bridge]: [R2] STOP commanded (denied, holder=R1)
[WARN] [fleetmem_bridge]: [R2] STOP commanded (dock ros-dock-1 held by R1)
[INFO] [fleetmem_bridge]: [R1] RELEASED ros-dock-1 after 8.0s dwell -- dock is now claimable
[INFO] [fleetmem_bridge]: [R2] CLAIM GRANTED on ros-dock-1 epoch=14 -- actuator authorised
```

**CockroachDB — `resource_claims` for that run.** One live claim at a time; R2 only gets the
dock after R1's `released_at` is set, and at a strictly higher fencing epoch:

```
resource_id   robot_id  epoch  claimed_at                      released_at
ros-dock-1    R1        11     2026-08-18 19:57:11.837395+00   2026-08-18 19:57:20.042524+00
ros-dock-1    R2        14     2026-08-18 19:57:22.037710+00   2026-08-18 19:57:30.651313+00
```

Sampled live, *while* the robots were contending — not only at the end:

```
  t=14s  live claims on ros-dock-1 = 1
  t=20s  live claims on ros-dock-1 = 1
  t=26s  live claims on ros-dock-1 = 0     (R1 released, R2 not yet re-claimed)

 THE INVARIANT -- peak simultaneous holders of ros-dock-1 over the whole run (must be 1):
   peak_simultaneous_holders = 1
   PASS -- two robots contended for one dock and never held it at the same time.
```

**A note on that invariant check, because the first version of it was wrong.** Counting
rows still live at the *end* of the run reports `0` — every claim has been released by then
— and it can never fail, which makes it a gate wearing a safety costume rather than a
safety mechanism. The query now asks whether any two claims on the resource **overlapped in
time**. It is capable of returning 2: the partial unique index does not forbid two
overlapping *released* rows, since its predicate is `released_at IS NULL`. That was checked
against a hand-built fixture of two deliberately overlapping closed claims, which the query
scores **2**, versus **1** for a strictly sequential pair.

**The audit trail CockroachDB recorded independently of the ROS logs:**

```
robot_id  kind             detail
R1        claim_granted    {"resource_id": "ros-dock-1"}
R2        claim_denied     {"holder": "R1", "resource_id": "ros-dock-1"}
R2        claim_denied     {"holder": "R1", "resource_id": "ros-dock-1"}
R1        claim_released   {"resource_id": "ros-dock-1"}
R2        claim_granted    {"resource_id": "ros-dock-1"}
R2        claim_released   {"resource_id": "ros-dock-1"}
```

**The physical consequence — `fake_robot`'s integrated pose** (excerpt from an
equivalent run of the same script; the freeze reproduces on every run): R2's position freezes at
`+2.88` for twelve seconds while R1 drives into the dock, then resumes the instant the
database grants it the claim:

```
POSE  R1 pos=(-3.18,+0.00) v=0.60  |  R2 pos=(+3.18,+0.00) v=0.60   <- both approaching
POSE  R1 pos=(-2.58,+0.00) v=0.60  |  R2 pos=(+2.88,+0.00) v=0.00   <- R2 DENIED, stopped
POSE  R1 pos=(-0.78,+0.00) v=0.60  |  R2 pos=(+2.88,+0.00) v=0.00
POSE  R1 pos=(-0.14,+0.00) v=0.00  |  R2 pos=(+2.88,+0.00) v=0.00   <- R1 docked, dwelling
POSE  R1 pos=(-0.14,+0.00) v=0.00  |  R2 pos=(+2.58,+0.00) v=0.60   <- released -> R2 moves
```

That freeze is the point of the whole project: **a unique index in a distributed database
was the only thing standing between two robots and one dock**, and it was enforced at the
actuator, not in a log.

The velocities were also read off `/R1/cmd_vel` and `/R2/cmd_vel` by `ros2 topic echo` —
**independent subscriber processes**, not the bridge reporting on itself.

## What was NOT verified — read this before believing anything else

- **Cross-container and cross-host DDS discovery.** Both nodes run in **one** container, so
  DDS never crosses a network boundary. This was a deliberate choice to remove the highest
  risk item, and it means the multi-machine case a real fleet needs is **untested here**. On
  separate hosts expect to need a matching `ROS_DOMAIN_ID`, a widened
  `ROS_AUTOMATIC_DISCOVERY_RANGE`, or a discovery server.
- **No real robot, and no Gazebo.** `fake_robot.py` is a kinematic fixture: it treats
  `linear.x` as a speed and `angular.z` as a heading in the odom frame (a holonomic base).
  There are no differential-drive constraints, no acceleration limits, no sensor noise, and
  no TF tree.
- **The bridge drives the robot end to end.** In a real stack it would sit **between** nav2
  and the base as a gate (twist_mux style), passing through or zeroing the planner's Twist.
  Trajectory generation here is a straight-line bearing and is not the interesting part.
- **The `StaleFenceError` path is implemented and reachable but was not exercised in this
  run** — no robot paused past its lease during a 45-second run. The equivalent case is
  covered by `scripts/verify_fencing.py` at the memory-layer level, not through ROS.
- **Not tested against CockroachDB Cloud.** Deliberately: the live cluster serves the
  deployed demo, and polluting it with test claims before judging would be reckless. Only a
  local node was used.
- **Killing the container mid-dwell leaves the last claim un-released** (a hard kill bypasses
  the shutdown handler). Its 90-second lease then lapses and the dock is reclaimed — exactly
  the crashed-robot path `verify_leases.py` covers — but it does mean an interrupted run can
  end with one live row. A run allowed to finish releases cleanly, as the table above shows.
- **The `/cmd_vel` zero-Twist totals printed by the script do not isolate denial.** They count
  every zero Twist, including the legitimate stop while a robot dwells in the dock. The
  evidence that isolates denial is the pose freeze, cross-referenced against the claim
  timestamps.

## Status

This lives on the `ros2-bridge` branch and is **additive only** — no existing file's
behaviour was modified.
