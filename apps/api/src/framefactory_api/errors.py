from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class NotFoundError(ApiError):
    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            404,
            "RESOURCE_NOT_FOUND",
            f"{resource} was not found",
            details={"resource": resource, "resource_id": resource_id},
        )


class ConflictError(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(409, code, message, details=details)


class PreconditionFailedError(ApiError):
    def __init__(self, current_revision: int, expected_revision: int) -> None:
        super().__init__(
            412,
            "REVISION_CONFLICT",
            "The resource changed after it was read",
            details={
                "current_revision": current_revision,
                "expected_revision": expected_revision,
            },
        )


class ValidationError(ApiError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(422, "CONTRACT_VALIDATION_FAILED", message, details=details)

