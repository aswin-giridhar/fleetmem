# Source this to put the .env AWS credentials into the environment the aws CLI reads.
#   source infra/aws-env.sh
# Needed because the CLI only honours AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY exactly,
# while .env may spell them differently. fleetmem/config.py does the same normalisation
# for the Python side.
set -a
export AWS_ACCESS_KEY_ID="$(grep -iE '^AWS_Access_key=|^AWS_ACCESS_KEY_ID=' .env | head -1 | cut -d= -f2-)"
export AWS_SECRET_ACCESS_KEY="$(grep -iE '^AWS_Secret_access_key=|^AWS_SECRET_ACCESS_KEY=' .env | head -1 | cut -d= -f2-)"
export AWS_REGION="$(grep -E '^AWS_REGION=' .env | head -1 | cut -d= -f2-)"
export AWS_DEFAULT_REGION="$AWS_REGION"
set +a
