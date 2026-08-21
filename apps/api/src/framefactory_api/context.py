from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .settings import Settings

DEFAULT_OWNER_PERMISSIONS = frozenset(
    {"assets:read", "assets:write", "assets:review", "assets:download"}
)


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """The authorization context passed to every repository operation."""

    user_id: UUID
    workspace_id: UUID
    workspace_name: str
    permissions: frozenset[str] = DEFAULT_OWNER_PERMISSIONS


class WorkspaceContextProvider(Protocol):
    """Replace this seam when authentication and workspace selection are enabled."""

    def resolve(self, requested_workspace_id: UUID | None = None) -> WorkspaceContext: ...


class DefaultWorkspaceContextProvider:
    """Single-user provider used by the current product scope.

    ``requested_workspace_id`` is deliberately ignored. The header remains a compatibility
    hint only and is not an authorization or routing boundary in single-workspace mode.
    """

    def __init__(self, settings: Settings) -> None:
        self._context = WorkspaceContext(
            user_id=settings.default_user_id,
            workspace_id=settings.default_workspace_id,
            workspace_name=settings.default_workspace_name,
        )

    def resolve(self, requested_workspace_id: UUID | None = None) -> WorkspaceContext:
        del requested_workspace_id
        return self._context

