"""Embedding providers.

Primary: Amazon Bedrock Titan Text Embeddings V2 (1024-dim).
Fallback: a local, deterministic hashed-ngram embedder.

The fallback exists so a judge with no AWS account can still run the project, and so
development was never blocked on credentials. It is NOT semantically equivalent to Titan:
it matches on shared words and character trigrams, not on paraphrase. Every row records
which provider produced its vector in `fleet_memory.provider`, because silently mixing
vectors from two different embedding spaces would make recall quietly meaningless — the
distances would still compute, and the answers would still look plausible.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from typing import Protocol

from .config import CONFIG
from .errors import EmbeddingUnavailableError

log = logging.getLogger("fleetmem.embeddings")
DIMS = CONFIG.embed_dims


class Embedder(Protocol):
    name: str
    def embed(self, text: str) -> list[float]: ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        # A zero vector has undefined cosine distance; return a fixed unit vector instead
        # of NaNs that would silently poison every comparison.
        out = [0.0] * DIMS
        out[0] = 1.0
        return out
    return [v / norm for v in vec]


class LocalHashEmbedder:
    """Deterministic bag-of-features embedder. No network, no credentials, no model."""

    name = "local-hash-v1"

    def _features(self, text: str) -> list[str]:
        text = text.lower()
        words = re.findall(r"[a-z0-9]+", text)
        feats = list(words)
        feats += [f"{a}_{b}" for a, b in zip(words, words[1:])]      # bigrams
        packed = " ".join(words)
        feats += [packed[i:i + 3] for i in range(max(0, len(packed) - 2))]  # char trigrams
        return feats

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIMS
        for feat in self._features(text):
            digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % DIMS
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        return _normalise(vec)


class BedrockTitanEmbedder:
    """Amazon Bedrock Titan Text Embeddings V2."""

    name = "bedrock-titan-v2"

    def __init__(self):
        import boto3  # imported lazily so the package works without boto3 configured
        self._client = boto3.client("bedrock-runtime", region_name=CONFIG.aws_region)

    def embed(self, text: str) -> list[float]:
        response = self._client.invoke_model(
            modelId=CONFIG.embed_model,
            body=json.dumps({"inputText": text, "dimensions": DIMS, "normalize": True}),
        )
        payload = json.loads(response["body"].read())
        vec = payload.get("embedding")
        # Validate the CONTENT, not just that the call returned 200. A malformed body must
        # resolve to "unavailable", never to a half-length vector inserted as if it were real.
        if not isinstance(vec, list) or len(vec) != DIMS:
            raise EmbeddingUnavailableError(
                f"Titan returned {type(vec).__name__} of length "
                f"{len(vec) if isinstance(vec, list) else 'n/a'}, expected {DIMS} floats")
        return vec


_ACTIVE: Embedder | None = None


def get_embedder() -> Embedder:
    """Resolve an embedder once, preferring Bedrock, and say clearly which one won."""
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE
    try:
        embedder = BedrockTitanEmbedder()
        embedder.embed("connectivity probe")   # prove invoke works, not merely that it constructs
        log.info("embeddings: using Amazon Bedrock %s (%d dims)", CONFIG.embed_model, DIMS)
        _ACTIVE = embedder
    except Exception as exc:
        if CONFIG.strict:
            raise EmbeddingUnavailableError(f"Bedrock unavailable and strict mode is on: {exc}")
        log.warning("embeddings: Bedrock unavailable (%s) -- falling back to %s. "
                    "Recall will match shared wording, not paraphrase.",
                    type(exc).__name__, LocalHashEmbedder.name)
        _ACTIVE = LocalHashEmbedder()
    return _ACTIVE


def embed(text: str) -> tuple[list[float], str]:
    """Return (vector, provider_name). The provider is stored alongside every vector."""
    embedder = get_embedder()
    return embedder.embed(text), embedder.name


def to_pgvector(vec: list[float]) -> str:
    """CockroachDB accepts the pgvector text form: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
