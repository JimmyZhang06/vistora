"""Authoritative operation-to-queue routing for isolated browser execution."""

from __future__ import annotations

BROWSER_CAPTURE_QUEUE = "browser-capture"
DEFAULT_STEP_QUEUE = "run-steps"

BROWSER_CAPTURE_OPERATIONS = frozenset(
    {
        "web.capture.validate",
        "web.capture.screenshot",
        "web.site.discover",
        "web.page.capture_batch",
    }
)


def operation_queue_name(operation: str) -> str:
    return (
        BROWSER_CAPTURE_QUEUE
        if operation in BROWSER_CAPTURE_OPERATIONS
        else DEFAULT_STEP_QUEUE
    )


__all__ = [
    "BROWSER_CAPTURE_OPERATIONS",
    "BROWSER_CAPTURE_QUEUE",
    "DEFAULT_STEP_QUEUE",
    "operation_queue_name",
]
