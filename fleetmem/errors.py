"""Typed errors.

The point of this module: at any I/O boundary, "the thing is absent" and "the system is
broken" must never collapse into the same value. A caller that cannot tell those apart
will render an empty fleet during an outage and call it success.
"""
from __future__ import annotations


class FleetMemError(Exception):
    """Base class."""


class MemoryBackendError(FleetMemError):
    """The database is unreachable or failing. NOT the same as 'no rows matched'."""


class ResourceHeldError(FleetMemError):
    """A resource is already claimed by another robot. Expected, actionable, not a fault."""

    def __init__(self, resource_id: str, holder: str | None):
        self.resource_id = resource_id
        self.holder = holder
        super().__init__(f"{resource_id} is held by {holder or 'another robot'}")


class EmbeddingUnavailableError(FleetMemError):
    """No embedding provider could produce a vector. Only raised when strict."""


class ReasoningUnavailableError(FleetMemError):
    """No reasoning provider available. Only raised when strict."""
