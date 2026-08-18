#!/usr/bin/env bash
# End-to-end verification of the FleetMem ROS 2 bridge.
#
# Two rclpy robots converge on ONE dock. CockroachDB's partial unique index decides which
# of them may enter, and the loser's wheels stop. This script proves that from BOTH sides:
#
#   DB side   : a single live row in resource_claims during contention.
#   ROS side  : `ros2 topic echo` -- an INDEPENDENT subscriber, not the bridge's own log --
#               shows a zero Twist on the denied robot's /cmd_vel, and the fake robot's
#               reported pose stops changing.
#
# Safety: this NEVER touches the live CockroachDB Cloud cluster. FLEETMEM_DSN is pinned to
# the local docker node by IP. That matters because fleetmem.config only honours an
# explicit DSN when it does not contain the substring "localhost" -- otherwise it falls
# through to the CRDB_* component variables in .env, which point at the London cluster.
# Every run also uses a fresh timestamped fleet_id, so it cannot collide with demo data.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-fleetmem-ros:jazzy}"
CRDB_CONTAINER="${CRDB_CONTAINER:-crdb}"
CRDB_HOST_IP="${CRDB_HOST_IP:-127.0.0.1}"
DSN="postgresql://root@${CRDB_HOST_IP}:26257/fleet_ros?sslmode=disable"
FLEET_NAME="${FLEET_NAME:-ros2-demo-$(date +%s)}"   # resolved to a UUID by ensure_fleet
DOCK="${DOCK:-ros-dock-1}"
DURATION="${DURATION:-45}"
OUT="$REPO/.tmp/ros2_evidence"
RUNNER="fleetmem-ros-run-$$"

mkdir -p "$OUT"
rm -f "$OUT"/*.log "$OUT"/*.txt 2>/dev/null

crdb_sql() { docker exec -i "$CRDB_CONTAINER" ./cockroach sql --insecure --database=fleet_ros "$@"; }

echo "=============================================================="
echo " FleetMem ROS 2 bridge -- end-to-end verification"
echo "=============================================================="
echo "  image     : $IMAGE"
echo "  fleet     : $FLEET_NAME   (fresh: cannot collide with demo data)"
echo "  dock      : $DOCK"
echo "  database  : $DSN  (LOCAL docker node, never the Cloud cluster)"
echo "  duration  : ${DURATION}s"
echo

# --- preflight: the DB must be reachable before we blame DDS for anything --------------
if ! crdb_sql -e "SELECT 1;" >/dev/null 2>&1; then
  echo "FAIL: local CockroachDB container '$CRDB_CONTAINER' is not reachable."
  echo "      start one with:"
  echo "      docker run -d --name crdb -p 26257:26257 cockroachdb/cockroach:latest \\"
  echo "        start-single-node --insecure"
  exit 1
fi
echo "[preflight] local CockroachDB reachable"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[preflight] building $IMAGE ..."
  docker build -t "$IMAGE" -f "$REPO/ros2/Dockerfile" "$REPO/ros2" || exit 1
fi
echo "[preflight] image present"
echo

# --- run both nodes in ONE container --------------------------------------------------
# One container means DDS discovery never crosses a network boundary, which removes the
# single highest-risk failure mode. --net=host additionally lets the bridge reach the
# CockroachDB port published on the host. ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST keeps
# our traffic off any other ROS graph sharing the host network.
docker run --rm --name "$RUNNER" --net=host \
  -v "$REPO":/repo -w /repo \
  -e FLEETMEM_DSN="$DSN" \
  -e FLEETMEM_REPO=/repo \
  -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}" \
  -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" bash -c "
source /opt/ros/jazzy/setup.bash
mkdir -p /repo/.tmp/ros2_evidence
cd /repo

python3 ros2/fake_robot.py --robots R1:-6.0:0.0,R2:6.0:0.0 --rate 10 \
    > /repo/.tmp/ros2_evidence/fake_robot.log 2>&1 &
FAKE=\$!
sleep 3

python3 ros2/fleetmem_bridge.py --robots R1,R2 --dock '$DOCK' --fleet '$FLEET_NAME' \
    --dock-x 0 --dock-y 0 --claim-radius 3.0 --dwell 8 --speed 0.6 --tick-hz 5 \
    --fleet-id-out /repo/.tmp/ros2_evidence/fleet_id.txt \
    > /repo/.tmp/ros2_evidence/bridge.log 2>&1 &
BRIDGE=\$!
sleep 2
if ! kill -0 \$BRIDGE 2>/dev/null; then
  echo 'BRIDGE DIED AT STARTUP -- see bridge.log'; cat /repo/.tmp/ros2_evidence/bridge.log
  kill \$FAKE 2>/dev/null; exit 3
fi

# Independent observers on the wire. These are separate ROS 2 processes subscribing to
# the same topics -- they are NOT the bridge reporting on itself.
ros2 topic echo /R1/cmd_vel geometry_msgs/msg/Twist \
    > /repo/.tmp/ros2_evidence/R1_cmd_vel.txt 2>&1 &
E1=\$!
ros2 topic echo /R2/cmd_vel geometry_msgs/msg/Twist \
    > /repo/.tmp/ros2_evidence/R2_cmd_vel.txt 2>&1 &
E2=\$!

ros2 topic list > /repo/.tmp/ros2_evidence/topic_list.txt 2>&1
ros2 topic info /R1/cmd_vel --verbose > /repo/.tmp/ros2_evidence/qos_R1_cmd_vel.txt 2>&1
ros2 topic info /R1/odom --verbose >> /repo/.tmp/ros2_evidence/qos_R1_cmd_vel.txt 2>&1

sleep $DURATION
kill \$E1 \$E2 \$BRIDGE \$FAKE 2>/dev/null
sleep 2
" > "$OUT/container.log" 2>&1 &
RUNPID=$!

# --- resolve the run's fleet UUID (written by the bridge at startup) -------------------
FLEET_ID=""
for _ in $(seq 1 30); do
  sleep 1
  [ -s "$OUT/fleet_id.txt" ] && { FLEET_ID="$(cat "$OUT/fleet_id.txt")"; break; }
done
if [ -z "$FLEET_ID" ]; then
  echo "FAIL: the bridge never reported a fleet UUID -- it did not start. bridge.log:"
  cat "$OUT/bridge.log" 2>/dev/null | tail -20; cat "$OUT/container.log" 2>/dev/null | tail -10
  docker rm -f "$RUNNER" >/dev/null 2>&1; exit 4
fi
echo "[run] fleet_id = $FLEET_ID"

# --- mid-run snapshot: the contended instant ------------------------------------------
echo "[run] nodes started; sampling the database while the robots contend..."
SNAP="$OUT/db_during_contention.txt"
: > "$SNAP"
for t in 14 20 26; do
  sleep 6
  {
    echo "----- t=${t}s : live claims on $DOCK -----"
    crdb_sql -e "SELECT resource_id, robot_id, epoch, released_at
                 FROM resource_claims
                 WHERE fleet_id = '$FLEET_ID' AND released_at IS NULL;" 2>&1
  } >> "$SNAP"
  LIVE=$(crdb_sql --format=csv -e "SELECT count(*) FROM resource_claims
           WHERE fleet_id = '$FLEET_ID' AND resource_id = '$DOCK'
             AND released_at IS NULL;" 2>/dev/null | tail -1)
  echo "  t=${t}s  live claims on $DOCK = ${LIVE:-?}"
done

wait $RUNPID
docker rm -f "$RUNNER" >/dev/null 2>&1
echo

# --- evidence -------------------------------------------------------------------------
echo "=============================================================="
echo " EVIDENCE 1 -- CockroachDB: who held the dock"
echo "=============================================================="
crdb_sql -e "SELECT resource_id, robot_id, epoch, claimed_at, released_at
             FROM resource_claims WHERE fleet_id = '$FLEET_ID' ORDER BY claimed_at;"

echo
echo " THE INVARIANT -- peak simultaneous holders of $DOCK over the whole run (must be 1):"
# Counting rows that are still live AT THE END is not an invariant check -- by then every
# claim is released, so it reports 0 and can never fail. The real question is whether any
# two claims on this resource OVERLAPPED IN TIME. Note the partial unique index does not
# forbid two overlapping RELEASED rows (its predicate is `released_at IS NULL`), so this
# query is genuinely capable of returning 2 -- verified against a hand-built overlapping
# fixture, which it scores 2 while a strictly sequential one scores 1.
PEAK=$(crdb_sql --format=csv -e "
  SELECT max(n) FROM (
    SELECT (SELECT count(*) FROM resource_claims b
            WHERE b.fleet_id = a.fleet_id AND b.resource_id = a.resource_id
              AND b.claimed_at <= coalesce(a.released_at, now())
              AND coalesce(b.released_at, now()) >= a.claimed_at) AS n
    FROM resource_claims a
    WHERE a.fleet_id = '$FLEET_ID' AND a.resource_id = '$DOCK');" 2>/dev/null | tail -1)
echo "   peak_simultaneous_holders = ${PEAK:-?}"
if [ "${PEAK:-}" = "1" ]; then
  echo "   PASS -- two robots contended for one dock and never held it at the same time."
else
  echo "   FAIL -- expected exactly 1 (got '${PEAK:-none}')."
fi

echo
echo " Audit trail (agent_events):"
crdb_sql -e "SELECT robot_id, kind, detail FROM agent_events
             WHERE fleet_id = '$FLEET_ID' ORDER BY created_at;" 2>&1 | head -30

echo
echo "=============================================================="
echo " EVIDENCE 2 -- ROS 2: grants, denials and stop commands"
echo "=============================================================="
grep -E "CLAIM GRANTED|CLAIM DENIED|FENCED|RELEASED|STOP commanded" \
     "$OUT/bridge.log" 2>/dev/null | head -25

echo
echo " Independent subscriber on the DENIED robot's /cmd_vel (zeros = stopped):"
for R in R1 R2; do
  if [ -f "$OUT/${R}_cmd_vel.txt" ]; then
    Z=$(grep -c "^  x: 0.0$" "$OUT/${R}_cmd_vel.txt" 2>/dev/null || echo 0)
    # Honest label: this total counts EVERY zero Twist, which includes the legitimate
    # stop while a robot dwells in the dock -- not only the denial stops. The evidence
    # that isolates denial is the pose freeze below, cross-referenced with the DB times.
    echo "   $R: $Z zero-velocity Twist messages on /$R/cmd_vel (denial stops AND dwell stops)"
  fi
done

echo
echo " Ground-truth pose (a denied robot's x/y stops changing):"
grep "POSE" "$OUT/fake_robot.log" 2>/dev/null | head -30

echo
echo "Full logs: $OUT"
