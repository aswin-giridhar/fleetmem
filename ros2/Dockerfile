# FleetMem ROS 2 bridge runtime.
#
# ros-base gives us rclpy + the standard message packages (nav_msgs, geometry_msgs)
# already built against the DDS middleware; rclpy is not pip-installable standalone.
# We add only psycopg so the bridge can talk to CockroachDB directly.
FROM ros:jazzy-ros-base

# Ubuntu 24.04 marks the system Python as externally managed (PEP 668). We are inside a
# throwaway container whose only Python consumer is ROS, so installing into the system
# interpreter is the right call here -- a venv would hide rclpy from the bridge.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --no-cache-dir --break-system-packages 'psycopg[binary]' psycopg_pool

# Fail the BUILD, not the 20:25 demo run, if an import is missing.
RUN python3 -c "import psycopg, psycopg_pool; print('psycopg ok', psycopg.__version__)" \
 && bash -c 'source /opt/ros/jazzy/setup.bash && python3 -c "import rclpy, nav_msgs.msg, geometry_msgs.msg; print(\"rclpy ok\")"'

WORKDIR /repo
ENV PYTHONPATH=/repo
ENV PYTHONUNBUFFERED=1
