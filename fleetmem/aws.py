"""AWS integration: S3 artifact store for fleet incident reports.

Role in the architecture: a robot that hits a problem writes a full incident report — an
artifact too large and too rarely read to belong in the operational database. S3 holds the
artifact; CockroachDB holds the *memory* (the distilled lesson, its embedding, and the S3
URI that points back to the evidence). The two are written in one transaction, so a lesson
can never reference an artifact that was never stored.

That split is the point: object storage for bulk evidence, CockroachDB for the searchable,
transactional memory that agents actually act on.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import CONFIG
from .errors import FleetMemError

log = logging.getLogger("fleetmem.aws")


class ArtifactStoreError(FleetMemError):
    """S3 is unreachable or refused the operation. Distinct from 'no artifact exists'."""


@dataclass
class Artifact:
    uri: str
    key: str
    bucket: str
    size: int


class S3ArtifactStore:
    """Stores incident reports in S3 and returns a URI to record alongside the memory."""

    def __init__(self, bucket: str | None = None, region: str | None = None):
        self.bucket = bucket or CONFIG.s3_bucket
        self.region = region or CONFIG.aws_region
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def ensure_bucket(self) -> bool:
        """Create the bucket if absent. Returns True if the bucket is usable."""
        if not self.enabled:
            return False
        client = self._get_client()
        try:
            client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            pass
        try:
            kwargs = {"Bucket": self.bucket}
            # us-east-1 rejects an explicit LocationConstraint; every other region needs it.
            if self.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            client.create_bucket(**kwargs)
            log.info("created S3 bucket %s in %s", self.bucket, self.region)
            return True
        except Exception as exc:
            raise ArtifactStoreError(f"cannot create bucket {self.bucket}: {exc}") from exc

    def put_incident_report(self, fleet_id: str, robot_id: str, report: dict) -> Artifact:
        """Store a full incident report. Returns the artifact reference to record in memory."""
        if not self.enabled:
            raise ArtifactStoreError("no S3 bucket configured (FLEETMEM_S3_BUCKET)")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        key = f"incidents/{fleet_id}/{robot_id}/{stamp}.json"
        body = json.dumps(report, indent=2, default=str).encode()
        try:
            self._get_client().put_object(
                Bucket=self.bucket, Key=key, Body=body,
                ContentType="application/json",
                # Evidence is immutable once written; encrypt at rest by default.
                ServerSideEncryption="AES256",
            )
        except Exception as exc:
            # Never swallow this into a success with an empty URI — a memory pointing at a
            # non-existent artifact is worse than no memory at all.
            raise ArtifactStoreError(f"failed to store artifact {key}: {exc}") from exc
        log.info("stored incident artifact s3://%s/%s (%d bytes)", self.bucket, key, len(body))
        return Artifact(uri=f"s3://{self.bucket}/{key}", key=key,
                        bucket=self.bucket, size=len(body))

    def get_incident_report(self, key: str) -> dict:
        """Fetch a stored report. Raises rather than returning {} when S3 is broken."""
        try:
            obj = self._get_client().get_object(Bucket=self.bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise ArtifactStoreError(f"artifact {key} does not exist") from exc
            raise ArtifactStoreError(f"S3 read failed for {key}: {exc}") from exc

    def status(self) -> dict:
        """Honest report of whether the artifact store is actually usable right now."""
        if not self.enabled:
            return {"enabled": False, "reason": "FLEETMEM_S3_BUCKET not set"}
        try:
            self._get_client().head_bucket(Bucket=self.bucket)
            return {"enabled": True, "bucket": self.bucket, "region": self.region}
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", str(exc)[:40])
            return {"enabled": False, "bucket": self.bucket, "reason": f"unreachable: {code}"}


STORE = S3ArtifactStore()
