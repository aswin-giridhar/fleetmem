"""Configuration, loaded from environment with explicit, visible defaults."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus


def _load_dotenv() -> None:
    """Minimal .env loader. Real environment always wins over the file."""
    for candidate in (Path.cwd() / ".env",
                      Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
        return


def _normalise_aws_env() -> None:
    """Map common AWS key spellings onto the exact names boto3 reads.

    boto3 looks up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY case-sensitively, and reports
    NoCredentialsError for anything else — indistinguishable from having no credentials at
    all. Accepting the spellings people actually write removes a whole class of confusing
    failure for a few lines.
    """
    aliases = {
        "AWS_ACCESS_KEY_ID": ("AWS_Access_key", "AWS_ACCESS_KEY", "AWS_access_key_id"),
        "AWS_SECRET_ACCESS_KEY": ("AWS_Secret_access_key", "AWS_SECRET_KEY",
                                  "AWS_secret_access_key"),
        "AWS_SESSION_TOKEN": ("AWS_Session_token", "AWS_session_token"),
    }
    for canonical, variants in aliases.items():
        if os.environ.get(canonical):
            continue
        for variant in variants:
            value = os.environ.get(variant, "").strip()
            if value:
                os.environ[canonical] = value
                break


_load_dotenv()
_normalise_aws_env()

DEFAULT_DSN = "postgresql://root@localhost:26257/fleet?sslmode=disable"


def _first(*names: str) -> str:
    """First non-empty environment variable among names."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _resolve_dsn() -> str:
    """Build the CockroachDB DSN.

    Precedence:
      1. FLEETMEM_DSN / DATABASE_URL — a complete non-local connection string wins outright.
      2. Component variables (CRDB_* or CLUSTER_*) — assembled into a Cloud DSN.
      3. Local Docker default.

    Component form exists because CockroachDB Cloud hands you host, user and password
    separately, and building the URL means URL-encoding the password correctly. Do that
    once here rather than in every README instruction.
    """
    explicit = _first("FLEETMEM_DSN", "DATABASE_URL")
    if explicit and "localhost" not in explicit:
        return explicit

    host = _first("CRDB_HOST", "CLUSTER_HOST")
    user = _first("CRDB_USER", "CLUSTER_USER")
    password = _first("CRDB_PASSWORD", "CLUSTER_PASSWORD")

    if host and user and password:
        port = _first("CRDB_PORT") or "26257"
        database = _first("CRDB_DATABASE", "CLUSTER_DATABASE") or "defaultdb"
        sslmode = _first("CRDB_SSLMODE") or "verify-full"
        dsn = (f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
               f"@{host}:{port}/{database}?sslmode={sslmode}")
        cert = _first("CRDB_SSLROOTCERT")
        if not cert:
            for candidate in (Path.home() / ".postgresql" / "root.crt",
                              Path(__file__).resolve().parent.parent / "certs" / "root.crt"):
                if candidate.is_file():
                    cert = str(candidate)
                    break
        if cert:
            dsn += f"&sslrootcert={quote_plus(cert)}"
        return dsn

    return explicit or DEFAULT_DSN


def redact(dsn: str) -> str:
    """Safe-to-log form of a DSN. Never print a raw DSN — it carries the password."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


@dataclass(frozen=True)
class Config:
    dsn: str = _resolve_dsn()
    aws_region: str = os.environ.get("AWS_REGION", "us-west-2")
    embed_model: str = os.environ.get("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    chat_model: str = os.environ.get(
        "BEDROCK_CHAT_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    s3_bucket: str = os.environ.get("FLEETMEM_S3_BUCKET", "")
    # When strict, a missing AWS dependency raises instead of degrading to a local fallback.
    # Off by default so the project is runnable by a judge with no AWS account.
    strict: bool = os.environ.get("FLEETMEM_STRICT", "0") == "1"
    # Embedding width. Titan v2 is configurable; 1024 is its default and ours.
    # Changing this requires re-seeding: the VECTOR(n) column is fixed-width.
    embed_dims: int = int(os.environ.get("FLEETMEM_EMBED_DIMS", "1024"))


CONFIG = Config()


def describe() -> dict:
    """Non-secret summary of the active configuration, for /healthz and startup logs."""
    return {
        "dsn": redact(CONFIG.dsn),
        "target": "CockroachDB Cloud" if "cockroachlabs.cloud" in CONFIG.dsn else "local",
        "aws_region": CONFIG.aws_region,
        "embed_model": CONFIG.embed_model,
        "chat_model": CONFIG.chat_model,
        "embed_dims": CONFIG.embed_dims,
        "s3_bucket": CONFIG.s3_bucket or None,
        "strict": CONFIG.strict,
    }
