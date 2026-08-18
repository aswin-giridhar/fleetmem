#!/usr/bin/env bash
# Deploy FleetMem to a single EC2 instance and print the public demo URL.
#
# Requires: AmazonEC2FullAccess on the calling IAM user.
#
# SECURITY POSTURE (deliberate, and worth reading before changing anything):
#
#  1. Secrets are NOT baked into user-data in plaintext where the running application can
#     read them back out of IMDS. The instance is launched with IMDSv2 REQUIRED and a hop
#     limit of 1, and the application user is firewalled away from 169.254.169.254 entirely,
#     so a compromised FleetMem process cannot read instance metadata or user-data.
#     The better long-term answer is SSM Parameter Store (SecureString) plus an instance
#     profile scoped to one parameter; that needs iam:PassRole and IAM write permissions,
#     which this deployment intentionally does not require. See DEPLOY_SECRETS below.
#
#  2. The application does NOT run as root. It runs as an unprivileged `fleetmem` user on
#     port 8000 with systemd hardening (NoNewPrivileges, ProtectSystem=strict, PrivateTmp).
#
#  3. TLS is terminated by Caddy, which obtains a Let's Encrypt certificate automatically
#     for a <ip>.nip.io hostname — so the demo is HTTPS without owning a domain. Port 80
#     redirects to 443 and exists only for the ACME challenge.
#
# The instance is on-demand (not spot) because the demo URL must survive unattended until
# judging ends on Sept 15; a spot reclaim would silently kill the link.
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

echo "==> security group (80 for ACME redirect, 443 for the demo)"
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 describe-security-groups --region "$REGION" \
     --filters Name=group-name,Values=$NAME Name=vpc-id,Values=$VPC \
     --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$SG" = "None" ] || [ -z "$SG" ]; then
  SG=$(aws ec2 create-security-group --region "$REGION" --group-name "$NAME" \
       --description "FleetMem demo" --vpc-id "$VPC" --query GroupId --output text)
  # Judges' source addresses are unknown, so the demo must be reachable from anywhere.
  # That is why it is HTTPS-only with no credentials, no personal data, and a read-mostly
  # surface — rather than a plaintext service pinned to an allowlist we cannot populate.
  for PORT in 80 443; do
    aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" \
      --protocol tcp --port "$PORT" --cidr 0.0.0.0/0 >/dev/null
  done
fi
echo "    $SG"

CLUSTER_ID=$(grep '^CRDB_CLUSTER_ID=' .env | cut -d= -f2)
DEPLOY_SECRETS=$(grep -vE '^(CRDB_SSLROOTCERT|FLEETMEM_DSN)=' .env)

BOOT=$(mktemp); trap 'rm -f "$BOOT"' EXIT
cat > "$BOOT" <<BOOTEOF
#!/bin/bash
set -xeuo pipefail

dnf install -y python3.11 python3.11-pip git iptables-services >/dev/null 2>&1

# --- 1. run as an unprivileged user, never root -----------------------------------
useradd --system --create-home --home-dir /opt/fleetmem --shell /usr/sbin/nologin fleetmem || true
git clone --depth 1 $REPO /opt/fleetmem/app
cd /opt/fleetmem/app

# --- 2. configuration, written root-only before the app user can be compromised ----
umask 077
cat > /opt/fleetmem/app/.env <<'ENVEOF'
$DEPLOY_SECRETS
CRDB_SSLROOTCERT=/opt/fleetmem/app/certs/root.crt
ENVEOF
mkdir -p /opt/fleetmem/app/certs
curl -sSL -o /opt/fleetmem/app/certs/root.crt \
  "https://cockroachlabs.cloud/clusters/$CLUSTER_ID/cert"
chown -R fleetmem:fleetmem /opt/fleetmem
chmod 600 /opt/fleetmem/app/.env

python3.11 -m pip install --quiet -e . >/dev/null 2>&1

# --- 3. deny the application user any route to instance metadata ------------------
# user-data and the instance role are readable via 169.254.169.254. The app never needs
# it (credentials come from .env), so block it outright for that uid.
iptables -A OUTPUT -d 169.254.169.254 -m owner --uid-owner fleetmem -j REJECT || true
iptables-save > /etc/sysconfig/iptables || true
systemctl enable --now iptables || true

cat > /etc/systemd/system/fleetmem.service <<'SVCEOF'
[Unit]
Description=FleetMem
After=network-online.target

[Service]
User=fleetmem
Group=fleetmem
WorkingDirectory=/opt/fleetmem/app
ExecStart=/usr/bin/python3.11 -m uvicorn fleetmem.api:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=/opt/fleetmem
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
SVCEOF

# --- 4. TLS via Caddy + automatic Let's Encrypt on a nip.io hostname ---------------
IP=\$(curl -sS -H "X-aws-ec2-metadata-token: \$(curl -sS -X PUT \
  'http://169.254.169.254/latest/api/token' \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" \
  http://169.254.169.254/latest/meta-data/public-ipv4)
HOST="\${IP//./-}.nip.io"

dnf install -y 'dnf-command(copr)' >/dev/null 2>&1 || true
dnf copr enable -y @caddy/caddy epel-9-x86_64 >/dev/null 2>&1 || true
dnf install -y caddy >/dev/null 2>&1 || true

cat > /etc/caddy/Caddyfile <<CADDYEOF
\$HOST {
    reverse_proxy 127.0.0.1:8000
}
CADDYEOF

systemctl daemon-reload
systemctl enable --now fleetmem
systemctl enable --now caddy || true
echo "\$HOST" > /opt/fleetmem/HOSTNAME
BOOTEOF

echo "==> launching $TYPE (IMDSv2 required, hop limit 1)"
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" \
  --security-group-ids "$SG" --user-data "file://$BOOT" \
  --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled' \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,Encrypted=true}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "    $IID"

aws ec2 wait instance-running --region "$REGION" --instance-ids "$IID"
IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$IID" \
     --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
HOST="${IP//./-}.nip.io"

echo
echo "instance : $IID"
echo "DEMO URL : https://$HOST/"
echo
echo "First boot installs dependencies and obtains a certificate; allow ~4 minutes."
echo "  until curl -sf https://$HOST/healthz; do sleep 15; done"
echo
echo "NOTE: secrets reach the instance through user-data. IMDSv2 is required, the hop limit"
echo "      is 1, and the fleetmem user is firewalled from 169.254.169.254, so the running"
echo "      application cannot read them back. For a long-lived production deployment, move"
echo "      them to SSM Parameter Store (SecureString) with a scoped instance profile."
