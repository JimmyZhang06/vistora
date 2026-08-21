from __future__ import annotations

from typing import Any

from .errors import ConflictError

ASSET_STATUSES = {
    "processing",
    "quarantined",
    "awaiting_review",
    "ready",
    "disabled",
    "deleted",
}
_ALLOWED_TRANSITIONS = {
    "processing": {"quarantined", "ready", "disabled", "deleted"},
    "quarantined": {"processing", "ready", "disabled", "deleted"},
    # Analysis uses this durable state when an automatically acquired candidate
    # fails a quality gate. It must still be possible to quarantine or retire
    # that candidate through the audited control API.
    "awaiting_review": {"processing", "quarantined", "ready", "disabled", "deleted"},
    "ready": {"processing", "quarantined", "disabled", "deleted"},
    "disabled": {"processing", "quarantined", "ready", "deleted"},
    "deleted": {"processing", "quarantined", "ready", "disabled"},
}


def ready_blockers(asset: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if asset.get("copyright_status") not in {"owned", "licensed", "public_domain"}:
        blockers.append("copyright_status")
    if (asset.get("file") or {}).get("scan_status") != "clean":
        blockers.append("scan_status")
    if asset.get("analysis_status") != "completed":
        blockers.append("analysis_status")
    return blockers


def validate_asset_transition(asset: dict[str, Any], target_status: str) -> None:
    current = str(asset.get("status"))
    if target_status not in ASSET_STATUSES:
        raise ConflictError(
            "ASSET_STATUS_INVALID",
            "The requested asset status is not supported",
            target_status=target_status,
        )
    if target_status == current:
        return
    if target_status not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ConflictError(
            "ASSET_STATUS_TRANSITION_INVALID",
            "The requested asset status transition is not allowed",
            current_status=current,
            target_status=target_status,
        )
    if target_status == "ready":
        blockers = ready_blockers(asset)
        if blockers:
            raise ConflictError(
                "ASSET_NOT_READY",
                "The asset cannot become ready until every safety gate passes",
                blockers=blockers,
            )


def normalized_tags(asset: dict[str, Any]) -> list[str]:
    metadata = asset.get("metadata") or {}
    values = asset.get("tags", metadata.get("tags", []))
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
