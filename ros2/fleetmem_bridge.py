#!/usr/bin/env python3
"""FleetMem <-> ROS 2 bridge: the database decides whether the actuator moves.

This is the node that makes FleetMem a drop-in for a real ROS 2 stack rather than a
simulation. It speaks the ordinary interfaces a warehouse AMR already exposes --
`nav_msgs/Odometry` in, `geometry_msgs/Twist` out -- and inserts CockroachDB between the
two:

    /<robot>/odom  ->  [ claim / fence check against CockroachDB ]  ->  /<robot>/cmd_vel

A robot approaching a dock does not get to decide it may enter. It asks the shared memory
layer, and a `23505` from the partial unique index `one_holder_per_resource` becomes a
zero-velocity Twist on the wire. That is the whole thesis made physical: the losing robot
is not merely told "no" in a log line, its wheels stop.

Three distinct database facts gate motion, and they are NOT the same check:

  * `claim()`   -- may this robot enter the dock at all? Denial is deterministic (someone
                   else holds it), so the robot re-routes rather than retrying.
  * `act()`     -- may this robot move RIGHT NOW, on the token it was granted? A robot
                   paused by GC or a network partition can wake after its lease lapsed and
                   still be physically moving. The fencing token is what catches that, and
                   it is checked on EVERY tick that commands motion, not once at claim time.
  * `renew()`   -- heartbeat. A live robot keeps its lease; a crashed one lets it lapse so
                   the dock is reclaimable.

Threading / executor note: psycopg calls block. All database work happens in one timer
callback on a single-threaded executor, so a slow query delays the next control tick
rather than interleaving with itself. Odometry callbacks do no I/O at all -- they only
stash the latest pose -- so the subscription never blocks on the network.

Run (see ros2/README.md for the container invocation):
    python3 ros2/fleetmem_bridge.py --robots R1,R2 --dock ros-dock-1 --fleet ros2-demo
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

sys.path.insert(0, os.environ.get("FLEETMEM_REPO", "/repo"))

from fleetmem.errors import MemoryBackendError, ResourceHeldError  # noqa: E402
from fleetmem.memory import FleetMemory, StaleFenceError  # noqa: E402


def _redact(dsn: str) -> str:
    return re.sub(r"://[^@/]*@", "://<redacted>@", dsn)


class RobotState:
    """Per-robot control state. Deliberately dumb: the authority lives in the database."""

    APPROACHING = "APPROACHING"   # driving toward the dock, no claim needed yet
    CLAIMED = "CLAIMED"           # holds the dock, authorised to enter
    DENIED = "DENIED"             # another robot holds it -> stopped / re-routing
    DEPARTED = "DEPARTED"         # finished, claim released

    def __init__(self, name: str):
        self.name = name
        self.x: float | None = None
        self.y: float | None = None
        self.state = self.APPROACHING
        self.epoch: int | None = None
        self.holder: str | None = None
        self.last_renew = 0.0
        self.dwell_started: float | None = None
        self.stop_ticks = 0


class FleetMemBridge(Node):
    def __init__(self, robots: list[str], dock: str, dock_xy: tuple[float, float],
                 fleet_id: str, claim_radius: float, release_radius: float,
                 dwell_seconds: float, speed: float, tick_hz: float):
        super().__init__("fleetmem_bridge")
        self.dock = dock
        self.dock_x, self.dock_y = dock_xy
        self.claim_radius = claim_radius
        self.release_radius = release_radius
        self.dwell_seconds = dwell_seconds
        self.speed = speed

        # ---- fail loudly and immediately if the memory layer is unreachable ----------
        # The connection pool blocks for ~25s on an unreachable DSN. Inside a timer
        # callback that is indistinguishable from a DDS discovery failure, which is the
        # single most expensive thing to misdiagnose here. So we probe once, up front.
        self.memory = FleetMemory(fleet_id)
        dsn = _redact(self.memory.db.dsn)
        self.get_logger().info(f"fleet_id={fleet_id} dock={dock} dsn={dsn}")
        try:
            holder = self.memory.holder_of(dock)
        except MemoryBackendError as exc:
            self.get_logger().fatal(f"CockroachDB unreachable at startup: {exc}")
            raise SystemExit(2)
        self.get_logger().info(
            f"memory layer reachable; current holder of {dock}: {holder or 'none'}")

        self.robots = {name: RobotState(name) for name in robots}
        self.cmd_pubs: dict[str, object] = {}
        for name in robots:
            # Odometry: BEST_EFFORT / KEEP_LAST(5), the standard sensor profile. A
            # BEST_EFFORT subscription is the permissive side of the requested-vs-offered
            # contract -- it also accepts a real robot publishing RELIABLE odom, whereas a
            # RELIABLE subscription would silently receive NOTHING from a BEST_EFFORT
            # publisher: topics discover each other, no message ever arrives, and no error
            # is raised anywhere. That silent mismatch is the classic ROS 2 QoS trap.
            self.create_subscription(
                Odometry, f"/{name}/odom",
                lambda msg, n=name: self._on_odom(n, msg),
                qos_profile_sensor_data)
            # Commands: RELIABLE / KEEP_LAST(10), the default for cmd_vel. A dropped stop
            # command is not acceptable; a dropped odom sample is.
            self.cmd_pubs[name] = self.create_publisher(Twist, f"/{name}/cmd_vel", 10)

        self.timer = self.create_timer(1.0 / tick_hz, self._tick)
        self.get_logger().info(
            f"bridge up: robots={robots} claim_radius={claim_radius}m "
            f"release_radius={release_radius}m tick={tick_hz}Hz")

    # ------------------------------------------------------------------ subscriptions
    def _on_odom(self, name: str, msg: Odometry) -> None:
        """Pose only. No database I/O here -- this callback must never block."""
        st = self.robots[name]
        st.x = msg.pose.pose.position.x
        st.y = msg.pose.pose.position.y

    # ------------------------------------------------------------------- publishing
    def _publish(self, name: str, lin: float, ang: float) -> None:
        twist = Twist()
        twist.linear.x = float(lin)
        twist.angular.z = float(ang)
        self.cmd_pubs[name].publish(twist)

    def _drive_toward_dock(self, st: RobotState) -> None:
        """Command motion along the bearing to the dock, in the odom frame.

        Simplification made explicit: this publishes a velocity whose direction is taken
        from the straight-line bearing, which suits the holonomic fake robot used for
        verification. On a differential-drive base this stage would be nav2's local
        planner; the bridge's job is the GATE, not the trajectory.
        """
        dx, dy = self.dock_x - st.x, self.dock_y - st.y
        dist = math.hypot(dx, dy)
        self._publish(st.name, min(self.speed, dist), math.atan2(dy, dx))

    def _stop(self, st: RobotState, reason: str) -> None:
        self._publish(st.name, 0.0, 0.0)
        st.stop_ticks += 1
        if st.stop_ticks in (1, 20) or st.stop_ticks % 40 == 0:
            self.get_logger().warn(f"[{st.name}] STOP commanded ({reason})")

    # ------------------------------------------------------------------- control loop
    def _tick(self) -> None:
        for st in self.robots.values():
            try:
                self._step(st)
            except MemoryBackendError as exc:
                # Absent != broken. If the memory layer is unreachable we cannot know who
                # holds the dock, so the only safe command is stop. Never assume "free".
                self._stop(st, f"memory layer unreachable: {exc}")

    def _step(self, st: RobotState) -> None:
        if st.x is None:
            return  # no odometry yet -- command nothing rather than guessing a pose
        dist = math.hypot(self.dock_x - st.x, self.dock_y - st.y)

        if st.state == RobotState.DEPARTED:
            self._publish(st.name, 0.0, 0.0)
            return

        # ---------------------------------------------------------------- denied
        if st.state == RobotState.DENIED:
            # Deterministic rejection: the holder will keep the dock until it releases, so
            # retrying in a tight loop is pointless. Stop, and re-check only occasionally
            # so the robot can proceed once the dock genuinely frees.
            self._stop(st, f"dock {self.dock} held by {st.holder}")
            if st.stop_ticks % 20 == 0:
                st.state = RobotState.APPROACHING
            return

        # ---------------------------------------------------------------- approaching
        if st.state == RobotState.APPROACHING:
            if dist > self.claim_radius:
                self._drive_toward_dock(st)   # outside the contested zone, free to move
                return
            try:
                grant = self.memory.claim(
                    self.dock, st.name, purpose=f"ros2 bridge approach ({dist:.2f}m)")
            except ResourceHeldError as exc:
                st.holder = exc.holder
                st.state = RobotState.DENIED
                st.stop_ticks = 0
                self.get_logger().warn(
                    f"[{st.name}] CLAIM DENIED on {self.dock} -- held by {exc.holder}; "
                    f"publishing zero Twist to /{st.name}/cmd_vel")
                self._stop(st, f"denied, holder={exc.holder}")
                return
            st.epoch = grant["epoch"]
            st.state = RobotState.CLAIMED
            st.last_renew = time.time()
            st.dwell_started = time.time()
            self.get_logger().info(
                f"[{st.name}] CLAIM GRANTED on {self.dock} epoch={st.epoch} -- "
                f"actuator authorised")

        # ---------------------------------------------------------------- claimed
        if st.state == RobotState.CLAIMED:
            # Fencing check BEFORE every commanded motion, not once at claim time. A robot
            # that paused past its lease still believes it holds the dock; the token is
            # what stops its wheels.
            try:
                self.memory.act(self.dock, st.name, st.epoch)
            except StaleFenceError as exc:
                self.get_logger().error(
                    f"[{st.name}] FENCED: {exc} -- actuator blocked")
                st.state = RobotState.DENIED
                st.stop_ticks = 0
                st.holder = "superseded"
                self._stop(st, "stale fencing token")
                return

            # Heartbeat. A lease bounds the claim; without renewal a long dwell would let
            # it lapse and act() would then (correctly) fence this robot out mid-dock.
            now = time.time()
            if now - st.last_renew > 10.0:
                self.memory.renew(self.dock, st.name)
                st.last_renew = now

            if dist > 0.15:
                self._drive_toward_dock(st)       # authorised: enter the dock
                return

            self._publish(st.name, 0.0, 0.0)      # arrived: dwell (loading)
            if now - st.dwell_started >= self.dwell_seconds:
                self.memory.release(self.dock, st.name)
                st.state = RobotState.DEPARTED
                self.get_logger().info(
                    f"[{st.name}] RELEASED {self.dock} after {self.dwell_seconds}s dwell "
                    f"-- dock is now claimable by another robot")

    # ------------------------------------------------------------------- shutdown
    def release_all(self) -> None:
        """Give the dock back on exit so a re-run starts from a clean state."""
        for st in self.robots.values():
            if st.state == RobotState.CLAIMED:
                try:
                    self.memory.release(self.dock, st.name)
                    self.get_logger().info(f"[{st.name}] released {self.dock} on shutdown")
                except Exception as exc:                      # noqa: BLE001
                    self.get_logger().error(f"release on shutdown failed: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--robots", default="R1,R2")
    ap.add_argument("--dock", default="ros-dock-1")
    ap.add_argument("--fleet", default="ros2-demo")
    ap.add_argument("--dock-x", type=float, default=0.0)
    ap.add_argument("--dock-y", type=float, default=0.0)
    ap.add_argument("--claim-radius", type=float, default=3.0)
    ap.add_argument("--release-radius", type=float, default=5.0)
    ap.add_argument("--dwell", type=float, default=8.0)
    ap.add_argument("--speed", type=float, default=0.6)
    ap.add_argument("--tick-hz", type=float, default=5.0)
    args, _ = ap.parse_known_args()

    rclpy.init()
    node = FleetMemBridge(
        robots=[r.strip() for r in args.robots.split(",") if r.strip()],
        dock=args.dock, dock_xy=(args.dock_x, args.dock_y), fleet_id=args.fleet,
        claim_radius=args.claim_radius, release_radius=args.release_radius,
        dwell_seconds=args.dwell, speed=args.speed, tick_hz=args.tick_hz)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.release_all()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
