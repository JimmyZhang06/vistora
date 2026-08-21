from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from math import isfinite
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from .queue import (
    IdempotencyConflictError,
    JobNotFoundError,
    LeaseLostError,
    QueueConnectionError,
    QueueJob,
)
from .redis_config import RedisQueueSettings
from .redis_scripts import CLAIM, ENQUEUE

_QUEUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class RedisJobQueue:
    """Redis is the durable fact source; Lua/transactions fence every transition."""

    def __init__(self, redis: Redis, settings: RedisQueueSettings) -> None:
        self._redis, self._settings = redis, settings
        self._prefix = f"{settings.namespace}:{{jobs}}"

    @classmethod
    def from_settings(cls, settings: RedisQueueSettings) -> RedisJobQueue:
        return cls(
            Redis.from_url(
                settings.url,
                decode_responses=True,
                socket_connect_timeout=settings.socket_timeout_seconds,
                socket_timeout=settings.socket_timeout_seconds,
                health_check_interval=30,
            ),
            settings,
        )

    async def close(self) -> None:
        await self._redis.aclose()

    async def healthcheck(self) -> None:
        try:
            if not await self._redis.ping() or int(await self._redis.eval("return 1", 0)) != 1:
                raise QueueConnectionError("Redis queue health check returned an invalid response")
        except RedisError as exc:
            raise QueueConnectionError(f"Redis queue health check failed: {exc}") from exc

    async def enqueue(
        self, *, queue_name: str, payload: dict[str, Any], deduplication_key: str,
        priority: int = 0, max_attempts: int = 3, delay_seconds: float = 0,
    ) -> tuple[QueueJob, bool]:
        self._validate_queue(queue_name)
        if not 1 <= len(deduplication_key) <= 512:
            raise ValueError("deduplication_key must contain 1 to 512 characters")
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not -100 <= priority <= 100
            or max_attempts <= 0
        ):
            raise ValueError("priority must be -100..100 and max_attempts must be positive")
        delay_ms = self._milliseconds(delay_seconds, zero=True)
        encoded = self._json(payload, "payload")
        fingerprint = hashlib.sha256(self._json(
            [queue_name, payload, priority, max_attempts, delay_ms], "job"
        ).encode()).hexdigest()
        job_id = str(uuid4())
        digest = hashlib.sha256(deduplication_key.encode()).hexdigest()
        response = await self._eval(ENQUEUE, [
            self._job_key(job_id), self._ready_key(queue_name), self._idem_key(digest)
        ], [job_id, fingerprint, queue_name, encoded, priority, max_attempts, delay_ms,
            self._settings.idempotency_ttl_seconds])
        code, resolved = int(response[0]), self._text(response[1])
        if code == -1:
            raise IdempotencyConflictError(deduplication_key)
        if code == -2:
            raise QueueConnectionError("Redis generated queue job id already exists")
        return await self.get(resolved), code == 1

    async def claim(
        self, *, queue_name: str, worker_id: str, lease_seconds: float
    ) -> QueueJob | None:
        self._validate_queue(queue_name)
        if not 1 <= len(worker_id) <= 255:
            raise ValueError("worker_id must contain 1 to 255 characters")
        response = await self._eval(CLAIM, [self._ready_key(queue_name), self._leases_key], [
            self._prefix, worker_id, str(uuid4()), self._milliseconds(lease_seconds),
            self._settings.recovery_batch_size, self._settings.claim_scan_size,
        ])
        return self._decode(self._pairs(response)) if response else None

    async def renew(
        self, *, job_id: str, lease_token: str, lease_seconds: float
    ) -> QueueJob:
        lease_ms = self._milliseconds(lease_seconds)
        key = self._job_key(job_id)
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    value = await pipe.hgetall(key)
                    now = self._time_ms(await pipe.time())
                    self._require_lease(job_id, value, lease_token, now=now)
                    expires = now + lease_ms
                    pipe.multi()
                    pipe.hset(key, mapping={
                        "heartbeat_at": now, "lease_expires_at": expires, "updated_at": now,
                    })
                    pipe.zadd(self._leases_key, {job_id: expires})
                    await pipe.execute()
                    return await self.get(job_id)
                except WatchError:
                    continue
                except RedisError as exc:
                    raise QueueConnectionError(f"Redis queue renew failed: {exc}") from exc

    heartbeat = renew

    async def acknowledge(
        self, *, job_id: str, lease_token: str, result: dict[str, Any] | None = None
    ) -> QueueJob:
        key = self._job_key(job_id)
        encoded = self._json(result or {}, "result")
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    value = await pipe.hgetall(key)
                    now = self._time_ms(await pipe.time())
                    self._require_lease(job_id, value, lease_token, now=now)
                    pipe.multi()
                    pipe.zrem(self._leases_key, job_id)
                    pipe.hset(key, mapping=self._terminal("succeeded", now, result=encoded))
                    await pipe.execute()
                    return await self.get(job_id)
                except WatchError:
                    continue
                except RedisError as exc:
                    raise QueueConnectionError(f"Redis queue acknowledge failed: {exc}") from exc

    ack = acknowledge

    async def retry(
        self, *, job_id: str, lease_token: str, error: dict[str, Any],
        delay_seconds: float | None = None,
    ) -> QueueJob:
        key, encoded = self._job_key(job_id), self._json(error, "error")
        requested_ms = (
            None
            if delay_seconds is None
            else self._milliseconds(delay_seconds, zero=True)
        )
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    value = await pipe.hgetall(key)
                    now = self._time_ms(await pipe.time())
                    self._require_lease(job_id, value, lease_token, now=now)
                    attempts, maximum = int(value["attempt_count"]), int(value["max_attempts"])
                    pipe.multi()
                    pipe.zrem(self._leases_key, job_id)
                    if attempts >= maximum:
                        pipe.hset(key, mapping=self._terminal("failed", now, error=encoded))
                    else:
                        delay_ms = requested_ms
                        if delay_ms is None:
                            delay_ms = min(
                                round(self._settings.retry_base_seconds * 1000)
                                * 2 ** (attempts - 1),
                                round(self._settings.retry_max_seconds * 1000),
                            )
                        available = now + delay_ms
                        pipe.hset(key, mapping={
                            "status": "retrying", "error": encoded, "available_at": available,
                            "updated_at": now, **self._empty_lease(),
                        })
                        pipe.zadd(self._ready_key(value["queue_name"]), {job_id: available})
                    await pipe.execute()
                    return await self.get(job_id)
                except WatchError:
                    continue
                except RedisError as exc:
                    raise QueueConnectionError(f"Redis queue retry failed: {exc}") from exc

    async def cancel(self, job_id: str) -> QueueJob:
        key = self._job_key(job_id)
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    value = await pipe.hgetall(key)
                    if not value:
                        raise JobNotFoundError(job_id)
                    if value["status"] in {"succeeded", "failed", "cancelled"}:
                        return self._decode(value)
                    now = self._time_ms(await pipe.time())
                    pipe.multi()
                    pipe.zrem(self._leases_key, job_id)
                    pipe.zrem(self._ready_key(value["queue_name"]), job_id)
                    fields = self._terminal("cancelled", now)
                    fields["cancellation_requested"] = "1"
                    pipe.hset(key, mapping=fields)
                    await pipe.execute()
                    return await self.get(job_id)
                except WatchError:
                    continue
                except RedisError as exc:
                    raise QueueConnectionError(f"Redis queue cancel failed: {exc}") from exc

    async def get(self, job_id: str) -> QueueJob:
        try:
            value = await self._redis.hgetall(self._job_key(job_id))
        except RedisError as exc:
            raise QueueConnectionError(f"Redis queue read failed: {exc}") from exc
        if not value:
            raise JobNotFoundError(job_id)
        return self._decode({self._text(key): self._text(item) for key, item in value.items()})

    async def _eval(self, script: str, keys: list[str], arguments: list[Any]) -> Any:
        try:
            return await self._redis.eval(script, len(keys), *keys, *arguments)
        except RedisError as exc:
            raise QueueConnectionError(f"Redis queue command failed: {exc}") from exc

    @staticmethod
    def _require_lease(
        job_id: str,
        value: dict[str, str],
        token: str,
        *,
        now: int | None = None,
    ) -> None:
        if not value:
            raise JobNotFoundError(job_id)
        expires = value.get("lease_expires_at")
        if (
            not token
            or value.get("status") != "running"
            or value.get("lease_token") != token
            or (now is not None and (not expires or int(expires) <= now))
        ):
            raise LeaseLostError(job_id)

    @staticmethod
    def _empty_lease() -> dict[str, str]:
        return {"lease_owner": "", "lease_token": "", "lease_expires_at": "", "heartbeat_at": ""}

    @classmethod
    def _terminal(
        cls, status: str, now: int, *, result: str = "", error: str = ""
    ) -> dict[str, str | int]:
        return {
            "status": status, "result": result, "error": error,
            "completed_at": now, "updated_at": now, **cls._empty_lease(),
        }

    @staticmethod
    def _time_ms(value: tuple[int, int]) -> int:
        return int(value[0]) * 1000 + int(value[1]) // 1000

    def _decode(self, value: dict[str, str]) -> QueueJob:
        def optional_time(name: str) -> datetime | None:
            return self._datetime(value[name]) if value.get(name) else None

        return QueueJob(
            id=value["id"],
            queue_name=value["queue_name"],
            payload=json.loads(value["payload"]),
            status=value["status"],
            priority=int(value["priority"]),
            attempt_count=int(value["attempt_count"]),
            max_attempts=int(value["max_attempts"]),
            available_at=self._datetime(value["available_at"]),
            created_at=self._datetime(value["created_at"]),
            updated_at=self._datetime(value["updated_at"]),
            lease_owner=value.get("lease_owner") or None,
            lease_token=value.get("lease_token") or None,
            lease_expires_at=optional_time("lease_expires_at"),
            heartbeat_at=optional_time("heartbeat_at"),
            completed_at=optional_time("completed_at"),
            result=json.loads(value["result"]) if value.get("result") else None,
            error=json.loads(value["error"]) if value.get("error") else None,
            cancellation_requested=value.get("cancellation_requested") == "1",
        )

    @staticmethod
    def _pairs(result: list[Any]) -> dict[str, str]:
        return {RedisJobQueue._text(result[index]): RedisJobQueue._text(result[index + 1])
                for index in range(0, len(result), 2)}

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)

    @staticmethod
    def _json(value: Any, label: str) -> str:
        try:
            return json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Queue {label} must be JSON serializable") from exc

    @staticmethod
    def _milliseconds(value: float, *, zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
            raise ValueError("Queue duration must be a finite number")
        if value < 0 or (value == 0 and not zero):
            message = (
                "Queue duration must be non-negative"
                if zero
                else "Queue duration must be positive"
            )
            raise ValueError(message)
        milliseconds = round(value * 1000)
        if milliseconds == 0 and not zero:
            raise ValueError("Queue duration must be at least one millisecond")
        return milliseconds

    @staticmethod
    def _validate_queue(value: str) -> None:
        if not _QUEUE_RE.fullmatch(value):
            raise ValueError("queue_name contains unsupported characters or exceeds 80 characters")

    def _job_key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}"

    def _ready_key(self, queue_name: str) -> str:
        return f"{self._prefix}:ready:{queue_name}"

    def _idem_key(self, digest: str) -> str:
        return f"{self._prefix}:idempotency:{digest}"

    @property
    def _leases_key(self) -> str:
        return f"{self._prefix}:leases"
