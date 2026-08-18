#!/usr/bin/env python3
"""Synthetic AMRs: closed-loop stand-ins so the bridge can be exercised without hardware.

Each robot subscribes to `/<robot>/cmd_vel`, integrates it, and publishes the result on
`/<robot>/odom`. That closes the loop, which is the point: the robot's POSITION is driven
by the Twist the bridge publishes, so when CockroachDB denies a claim and the bridge sends
a zero Twist, the robot physically stops moving. The evidence is a frozen `x`/`y` in the
odometry stream, not a log line asserting a stop.

Two robots start equidistant from one dock, on different bearings, and converge on it
simultaneously -- the contended case the partial unique index exists to arbitrate.

Deliberately NOT modelled: differential-drive constraints, acceleration limits, sensor
noise. `linear.x` is treated as a speed and `angular.z` as a heading in the odom frame
(i.e. a holonomic base). This node is a test fixture for the bridge, not a physics
simulator -- Gazebo is what you would swap in, and the bridge would not change.
"""
from __future__ import annotations

import argparse
import math

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data


class FakeRobot:
    def __init__(self, name: str, x: float, y: float):
        self.name = name
        self.x, self.y = x, y
        self.speed = 0.0
        self.heading = 0.0


class FakeFleet(Node):
    def __init__(self, specs: list[tuple[str, float, float]], rate_hz: float):
        super().__init__("fake_robot_fleet")
        self.dt = 1.0 / rate_hz
        self.robots: dict[str, FakeRobot] = {}
        self.odom_pubs: dict[str, object] = {}
        for name, x, y in specs:
            self.robots[name] = FakeRobot(name, x, y)
            # Same explicit profile the bridge subscribes with. Publisher and subscriber
            # must agree: a RELIABLE subscriber receives NOTHING from a BEST_EFFORT
            # publisher, silently, with no error on either side.
            self.odom_pubs[name] = self.create_publisher(
                Odometry, f"/{name}/odom", qos_profile_sensor_data)
            self.create_subscription(
                Twist, f"/{name}/cmd_vel",
                lambda msg, n=name: self._on_cmd(n, msg), 10)
            self.get_logger().info(f"spawned {name} at ({x:.2f}, {y:.2f})")

        self.create_timer(self.dt, self._step)
        self.create_timer(1.0, self._report)

    def _on_cmd(self, name: str, msg: Twist) -> None:
        r = self.robots[name]
        r.speed = msg.linear.x
        r.heading = msg.angular.z

    def _step(self) -> None:
        for r in self.robots.values():
            r.x += r.speed * math.cos(r.heading) * self.dt
            r.y += r.speed * math.sin(r.heading) * self.dt
            odom = Odometry()
            odom.header.stamp = self.get_clock().now().to_msg()
            odom.header.frame_id = "odom"
            odom.child_frame_id = f"{r.name}/base_link"
            odom.pose.pose.position.x = r.x
            odom.pose.pose.position.y = r.y
            # Yaw as a quaternion about z, so the message is a valid Odometry rather than
            # one with an all-zero (invalid, non-unit) orientation.
            odom.pose.pose.orientation.z = math.sin(r.heading / 2.0)
            odom.pose.pose.orientation.w = math.cos(r.heading / 2.0)
            odom.twist.twist.linear.x = r.speed
            self.odom_pubs[r.name].publish(odom)

    def _report(self) -> None:
        """One line per second of ground truth: a stopped robot's x/y stops changing."""
        parts = [f"{r.name} pos=({r.x:+.2f},{r.y:+.2f}) v={r.speed:.2f}"
                 for r in self.robots.values()]
        self.get_logger().info("POSE  " + "  |  ".join(parts))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    # "name:x:y" triples. The defaults put R1 and R2 exactly 6m from a dock at the origin,
    # approaching from opposite sides, so neither wins by being closer.
    ap.add_argument("--robots", default="R1:-6.0:0.0,R2:6.0:0.0")
    ap.add_argument("--rate", type=float, default=10.0)
    args, _ = ap.parse_known_args()

    specs = []
    for chunk in args.robots.split(","):
        name, x, y = chunk.split(":")
        specs.append((name.strip(), float(x), float(y)))

    rclpy.init()
    node = FakeFleet(specs, args.rate)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
