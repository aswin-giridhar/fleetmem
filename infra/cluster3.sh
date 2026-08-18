#!/usr/bin/env bash
# Bring up a local 3-node CockroachDB cluster for the resilience demonstration.
#
# This is deliberately LOCAL and separate from the CockroachDB Cloud cluster the app
# normally uses: a managed Basic cluster does not let you kill a node, and pretending
# otherwise on camera would be dishonest. The point being demonstrated — the fleet's memory
# survives losing a node — is a property of CockroachDB's replication, and it is the same
# property whether the cluster is self-hosted or managed.
set -euo pipefail
IMG=cockroachdb/cockroach:latest
NET=fleetmem-net

case "${1:-up}" in
up)
  docker network create "$NET" >/dev/null 2>&1 || true
  for i in 1 2 3; do
    docker rm -f "crdb$i" >/dev/null 2>&1 || true
    docker run -d --name "crdb$i" --net "$NET" \
      -p "$((26260+i)):26257" -p "$((8090+i)):8080" \
      "$IMG" start --insecure \
        --join=crdb1:26257,crdb2:26257,crdb3:26257 \
        --listen-addr="crdb$i:26257" --advertise-addr="crdb$i:26257" \
        --http-addr=0.0.0.0:8080 >/dev/null
  done
  sleep 4
  docker exec crdb1 ./cockroach init --insecure --host=crdb1:26257 >/dev/null 2>&1 || true
  until docker exec crdb1 ./cockroach sql --insecure --host=crdb1:26257 \
        -e "SELECT 1" >/dev/null 2>&1; do sleep 2; done
  # 3 replicas of every range, so the cluster tolerates losing exactly one node
  docker exec crdb1 ./cockroach sql --insecure --host=crdb1:26257 -e \
    "ALTER RANGE default CONFIGURE ZONE USING num_replicas = 3;
     CREATE DATABASE IF NOT EXISTS fleet;" >/dev/null 2>&1 || true
  echo "3-node cluster up:  postgresql://root@localhost:26261/fleet?sslmode=disable"
  docker exec crdb1 ./cockroach node status --insecure --host=crdb1:26257 \
    --format=table 2>/dev/null | awk 'NR<=5{print "  " $0}'
  ;;
down)
  for i in 1 2 3; do docker rm -f "crdb$i" >/dev/null 2>&1 || true; done
  docker network rm "$NET" >/dev/null 2>&1 || true
  echo "3-node cluster removed"
  ;;
status)
  docker exec crdb1 ./cockroach node status --insecure --host=crdb1:26257 --format=table
  ;;
*) echo "usage: $0 {up|down|status}"; exit 1 ;;
esac
