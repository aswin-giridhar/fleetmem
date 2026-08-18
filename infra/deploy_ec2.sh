#!/usr/bin/env bash
# Deploy FleetMem to a single EC2 instance and print the public demo URL.
#
# Requires the IAM user to have EC2 permissions (AmazonEC2FullAccess is sufficient).
# Deliberately avoids an instance profile so no iam:PassRole is needed — credentials are
# delivered through user-data instead, which keeps the permission surface to one policy.
#
# The instance must stay alive until judging ends (Sept 15), so this uses on-demand
# t3.small rather than spot: a spot reclaim would silently kill the demo link.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
NAME="fleetmem-demo"
TYPE="${INSTANCE_TYPE:-t3.small}"
REPO="https://github.com/aswin-giridhar/fleetmem.git"

command -v aws >/dev/null || { echo "aws CLI required"; exit 1; }
[ -f .env ] || { echo ".env required (CockroachDB + AWS settings)"; exit 1; }

echo "==> resolving latest Amazon Linux 2023 AMI in $REGION"
AMI=$(aws ssm get-parameters --region "$REGION" \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameters[0].Value' --output text)
echo "    $AMI"

echo "==> security group"
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 describe-security-groups --region "$REGION" \
     --filters Name=group-name,Values=$NAME Name=vpc-id,Values=$VPC \
     --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$SG" = "None" ] || [ -z "$SG" ]; then
  SG=$(aws ec2 create-security-group --region "$REGION" --group-name "$NAME" \
       --description "FleetMem demo" --vpc-id "$VPC" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
fi
echo "    $SG"

# user-data: install, fetch the app, inject config, run behind port 80
BOOT=$(mktemp)
{
  echo '#!/bin/bash'
  echo 'set -x'
  echo 'dnf install -y python3.11 python3.11-pip git >/dev/null 2>&1'
  echo "git clone --depth 1 $REPO /opt/fleetmem"
  echo 'cd /opt/fleetmem'
  echo 'cat > .env <<"ENVEOF"'
  # ship the working configuration, minus anything host-specific
  grep -vE '^(CRDB_SSLROOTCERT|FLEETMEM_DSN)=' .env
  echo 'CRDB_SSLROOTCERT=/opt/fleetmem/certs/root.crt'
  echo 'ENVEOF'
  echo 'mkdir -p certs'
  echo "curl -sSL -o certs/root.crt https://cockroachlabs.cloud/clusters/$(grep '^CRDB_CLUSTER_ID=' .env | cut -d= -f2)/cert"
  echo 'python3.11 -m pip install --quiet -e . >/dev/null 2>&1'
  echo 'cat > /etc/systemd/system/fleetmem.service <<"SVCEOF"'
  echo '[Unit]'
  echo 'Description=FleetMem'
  echo 'After=network-online.target'
  echo '[Service]'
  echo 'WorkingDirectory=/opt/fleetmem'
  echo 'ExecStart=/usr/bin/python3.11 -m uvicorn fleetmem.api:app --host 0.0.0.0 --port 80'
  echo 'Restart=always'
  echo 'RestartSec=5'
  echo '[Install]'
  echo 'WantedBy=multi-user.target'
  echo 'SVCEOF'
  echo 'systemctl daemon-reload && systemctl enable --now fleetmem'
} > "$BOOT"

echo "==> launching $TYPE"
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" \
  --security-group-ids "$SG" --user-data "file://$BOOT" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --query 'Instances[0].InstanceId' --output text)
rm -f "$BOOT"
echo "    $IID"

aws ec2 wait instance-running --region "$REGION" --instance-ids "$IID"
IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$IID" \
     --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo
echo "instance : $IID"
echo "DEMO URL : http://$IP/"
echo
echo "First boot installs dependencies; allow ~3 minutes. Poll until healthy:"
echo "  until curl -sf http://$IP/healthz; do sleep 10; done"
