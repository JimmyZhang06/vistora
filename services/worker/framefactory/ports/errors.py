"""Errors shared by infrastructure port implementations."""

from __future__ import annotations


class PortError(RuntimeError):
    """Base class for an infrastructure boundary failure."""


class RevisionConflictError(PortError):
    """A compare-and-swap write used a stale revision."""


class DeliveryLeaseLostError(PortError):
    """A queue delivery was acknowledged after its lease was lost."""


class ObjectConflictError(PortError):
    """An immutable object key already contains different bytes."""
