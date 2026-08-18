#!/usr/bin/env bash
# Exercise the StaleFenceError path END TO END through ROS 2.
#
# The bridge calls act(dock, robot, epoch) before every commanded motion. This test starts
# the stack, waits for a robot to be granted the dock, then STEALS the claim from outside
# the fleet — exactly what a paused robot would experience on waking. The bridge's next
# act() must fail, and the robot's wheels must stop.
set -euo pipefail
cd "$(dirname "$0")/.."
DSN="postgresql://root@127.0.0.1:26257/fleet_ros?sslmode=disable"
LOG=.tmp/fence_ros.log
mkdir -p .tmp; rm -f "$LOG"

echo "[1] starting the ROS 2 stack in the background"
docker run --rm --name fleetmem-fence --net=host \
  -v "$PWD":/repo -w /repo \
  -e FLEETMEM_DSN="$DSN" -e FLEETMEM_REPO=/repo -e ROS_DOMAIN_ID=43 \
  -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST -e PYTHONUNBUFFERED=1 \
  fleetmem-ros:jazzy bash -c "
source /opt/ros/jazzy/setup.bash
cd /repo
python3 ros2/fake_robot.py --robots R1:-6.0:0.0,R2:6.0:0.0 --rate 10 &
sleep 3
python3 ros2/fleetmem_bridge.py --robots R1,R2 --dock fence-dock --fleet fence-fleet \
  --dock-x 0 --dock-y 0 --claim-radius 3.0 --dwell 40 --speed 0.6 --tick-hz 5
" > "$LOG" 2>&1 &

echo "[2] waiting for a CLAIM GRANTED ..."
for i in $(seq 1 40); do grep -q "CLAIM GRANTED" "$LOG" 2>/dev/null && break; sleep 1; done
grep -m1 "CLAIM GRANTED" "$LOG" | sed 's/^/    /'
HOLDER=$(grep -m1 -oE "\[R[0-9]+\] CLAIM GRANTED" "$LOG" | grep -oE "R[0-9]+")
echo "    holder is $HOLDER"

echo "[3] STEALING the claim from outside — the robot keeps its now-stale epoch"
docker exec crdb ./cockroach sql --insecure -d fleet_ros -e "
  UPDATE resource_claims SET released_at = now()
    WHERE released_at IS NULL AND robot_id = '$HOLDER';
  INSERT INTO resource_claims (fleet_id, resource_id, robot_id, purpose, epoch, expires_at)
    SELECT fleet_id, resource_id, 'GHOST', 'stolen', nextval('fleetmem_epoch'),
           now() + INTERVAL '120 second'
      FROM resource_claims WHERE robot_id = '$HOLDER' ORDER BY claimed_at DESC LIMIT 1;
" >/dev/null 2>&1
echo "    claim reassigned to GHOST at a higher epoch"

echo "[4] waiting for the bridge to fence the actuator ..."
FENCED=0
for i in $(seq 1 25); do
  if grep -q "FENCED" "$LOG" 2>/dev/null; then FENCED=1; break; fi
  sleep 1
done

docker rm -f fleetmem-fence >/dev/null 2>&1 || true
echo
echo "=============================== RESULT ==============================="
if [ "$FENCED" = "1" ]; then
  grep -m2 "FENCED" "$LOG" | sed 's/^/  /'
  echo "  Robot pose after fencing (velocity must be 0.00):"
  grep "POSE" "$LOG" | tail -2 | sed 's/^/  /'
  echo
  echo "  PASS - a stale fencing token blocked the actuator through the real ROS 2 path"
else
  echo "  FAIL - no FENCED line appeared; the path was NOT exercised"
  tail -12 "$LOG" | sed 's/^/  /'
  exit 1
fi
