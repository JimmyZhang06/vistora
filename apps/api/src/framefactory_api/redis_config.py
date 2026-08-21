from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import isfinite
from urllib.parse import urlsplit

from .environment import environment_value

_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True, slots=True)
class RedisQueueSettings:
    url: str
    namespace: str = "framefactory"
    socket_timeout_seconds: float = 5.0
    idempotency_ttl_seconds: int = 7 * 24 * 60 * 60
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 300.0
    recovery_batch_size: int = 100
    claim_scan_size: int = 100

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"redis", "rediss", "unix"}:
            raise ValueError("Redis URL must use redis://, rediss://, or unix://")
        if parsed.scheme != "unix" and not parsed.hostname:
            raise ValueError("Redis URL must include a host")
        if not _NAMESPACE_RE.fullmatch(self.namespace):
            raise ValueError("Redis namespace may contain only letters, digits, '.', '_' and '-'")
        if (
            isinstance(self.socket_timeout_seconds, bool)
            or not isfinite(self.socket_timeout_seconds)
            or self.socket_timeout_seconds <= 0
        ):
            raise ValueError("Redis socket timeout must be positive")
        if (
            isinstance(self.idempotency_ttl_seconds, bool)
            or not isinstance(self.idempotency_ttl_seconds, int)
            or self.idempotency_ttl_seconds <= 0
        ):
            raise ValueError("Redis idempotency TTL must be positive")
        if (
            isinstance(self.retry_base_seconds, bool)
            or isinstance(self.retry_max_seconds, bool)
            or not isfinite(self.retry_base_seconds)
            or not isfinite(self.retry_max_seconds)
            or self.retry_base_seconds <= 0
            or self.retry_max_seconds < self.retry_base_seconds
        ):
            raise ValueError("Redis retry delays must satisfy 0 < base <= maximum")
        if (
            isinstance(self.recovery_batch_size, bool)
            or isinstance(self.claim_scan_size, bool)
            or not isinstance(self.recovery_batch_size, int)
            or not isinstance(self.claim_scan_size, int)
            or self.recovery_batch_size <= 0
            or self.claim_scan_size <= 0
        ):
            raise ValueError("Redis queue batch sizes must be positive")

    @classmethod
    def from_environment(cls) -> RedisQueueSettings:
        url = environment_value("FRAMEFACTORY_REDIS_URL")
        if not url:
            raise ValueError("FRAMEFACTORY_REDIS_URL is required for the Redis queue adapter")
        return cls(
            url=url,
            namespace=os.getenv("FRAMEFACTORY_REDIS_NAMESPACE", "framefactory"),
            socket_timeout_seconds=float(
                os.getenv("FRAMEFACTORY_REDIS_SOCKET_TIMEOUT_SECONDS", "5")
            ),
            idempotency_ttl_seconds=int(
                os.getenv("FRAMEFACTORY_REDIS_IDEMPOTENCY_TTL_SECONDS", str(7 * 24 * 60 * 60))
            ),
            retry_base_seconds=float(
                os.getenv("FRAMEFACTORY_REDIS_RETRY_BASE_SECONDS", "1")
            ),
            retry_max_seconds=float(
                os.getenv("FRAMEFACTORY_REDIS_RETRY_MAX_SECONDS", "300")
            ),
            recovery_batch_size=int(
                os.getenv("FRAMEFACTORY_REDIS_RECOVERY_BATCH_SIZE", "100")
            ),
            claim_scan_size=int(os.getenv("FRAMEFACTORY_REDIS_CLAIM_SCAN_SIZE", "100")),
        )

