"""Redis leased queue compatible with the control API RedisJobQueue layout."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from typing import Any
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError

from framefactory.ports import DeliveryLeaseLostError, QueueDelivery, QueueMessage


@dataclass(frozen=True, slots=True)
class PayloadDelivery:
    message_id: str
    queue_name: str
    payload: dict[str, Any]
    receipt: str
    worker_id: str
    delivery_count: int
    lease_expires_at: datetime

ENQUEUE = r"""
local previous_id = redis.call('HGET', KEYS[3], 'job_id')
if previous_id then
  if redis.call('HGET', KEYS[3], 'fingerprint') ~= ARGV[2] then return {-1, previous_id} end
  return {0, previous_id}
end
if redis.call('EXISTS', KEYS[1]) == 1 then return {-2, ARGV[1]} end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local available_at = now + tonumber(ARGV[7])
redis.call('HSET', KEYS[1], 'id', ARGV[1], 'queue_name', ARGV[3],
  'payload', ARGV[4], 'status', 'queued', 'priority', ARGV[5],
  'attempt_count', '0', 'max_attempts', ARGV[6], 'available_at', available_at,
  'created_at', now, 'updated_at', now, 'lease_owner', '', 'lease_token', '',
  'lease_expires_at', '', 'heartbeat_at', '', 'completed_at', '', 'result', '',
  'error', '', 'cancellation_requested', '0')
redis.call('ZADD', KEYS[2], available_at, ARGV[1])
redis.call('HSET', KEYS[3], 'job_id', ARGV[1], 'fingerprint', ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[8])
return {1, ARGV[1]}
"""

CLAIM = r"""
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now, 'LIMIT', 0, ARGV[5])
for _, job_id in ipairs(expired) do
  local job_key = ARGV[1] .. ':job:' .. job_id
  local status = redis.call('HGET', job_key, 'status')
  local expires = tonumber(redis.call('HGET', job_key, 'lease_expires_at') or '0')
  if status == 'running' and expires <= now then
    local attempts = tonumber(redis.call('HGET', job_key, 'attempt_count') or '0')
    local maximum = tonumber(redis.call('HGET', job_key, 'max_attempts') or '0')
    local queue_name = redis.call('HGET', job_key, 'queue_name')
    if attempts >= maximum then
      redis.call('HSET', job_key, 'status', 'failed',
        'error', '{"code":"LEASE_EXPIRED","message":"Worker lease expired after final attempt"}',
        'completed_at', now, 'updated_at', now)
    else
      redis.call('HSET', job_key, 'status', 'retrying', 'available_at', now, 'updated_at', now)
      redis.call('ZADD', ARGV[1] .. ':ready:' .. queue_name, now, job_id)
    end
    redis.call('HSET', job_key, 'lease_owner', '', 'lease_token', '',
      'lease_expires_at', '', 'heartbeat_at', '')
  end
  redis.call('ZREM', KEYS[2], job_id)
end
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, ARGV[6])
local selected = nil
local selected_priority = -101
local selected_available = nil
local selected_created = nil
for _, job_id in ipairs(candidates) do
  local job_key = ARGV[1] .. ':job:' .. job_id
  local status = redis.call('HGET', job_key, 'status')
  if status ~= 'queued' and status ~= 'retrying' then
    redis.call('ZREM', KEYS[1], job_id)
  else
    local attempts = tonumber(redis.call('HGET', job_key, 'attempt_count') or '0')
    local maximum = tonumber(redis.call('HGET', job_key, 'max_attempts') or '0')
    if attempts >= maximum then
      redis.call('ZREM', KEYS[1], job_id)
      redis.call('HSET', job_key, 'status', 'failed',
        'error', '{"code":"ATTEMPTS_EXHAUSTED","message":"Job exhausted its attempts"}',
        'completed_at', now, 'updated_at', now)
    else
      local priority = tonumber(redis.call('HGET', job_key, 'priority') or '0')
      local available = tonumber(redis.call('HGET', job_key, 'available_at') or '0')
      local created = tonumber(redis.call('HGET', job_key, 'created_at') or '0')
      if not selected or priority > selected_priority or
        (priority == selected_priority and available < selected_available) or
        (priority == selected_priority and available == selected_available and created < selected_created) then
        selected, selected_priority = job_id, priority
        selected_available, selected_created = available, created
      end
    end
  end
end
if not selected then return {} end
local job_key = ARGV[1] .. ':job:' .. selected
local lease_expires = now + tonumber(ARGV[4])
redis.call('ZREM', KEYS[1], selected)
redis.call('HINCRBY', job_key, 'attempt_count', 1)
redis.call('HSET', job_key, 'status', 'running', 'lease_owner', ARGV[2],
  'lease_token', ARGV[3], 'lease_expires_at', lease_expires,
  'heartbeat_at', now, 'updated_at', now)
redis.call('ZADD', KEYS[2], lease_expires, selected)
return redis.call('HGETALL', job_key)
"""

ACK = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('HGET', KEYS[1], 'status') ~= 'running' or
   redis.call('HGET', KEYS[1], 'lease_token') ~= ARGV[1] then return -1 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if tonumber(redis.call('HGET', KEYS[1], 'lease_expires_at') or '0') <= now then return -1 end
redis.call('ZREM', KEYS[2], ARGV[2])
redis.call('HSET', KEYS[1], 'status', 'succeeded', 'result', ARGV[3],
  'completed_at', now, 'updated_at', now, 'lease_owner', '', 'lease_token', '',
  'lease_expires_at', '', 'heartbeat_at', '')
return 1
"""

RENEW = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('HGET', KEYS[1], 'status') ~= 'running' or
   redis.call('HGET', KEYS[1], 'lease_token') ~= ARGV[1] then return -1 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if tonumber(redis.call('HGET', KEYS[1], 'lease_expires_at') or '0') <= now then return -1 end
local lease_expires = now + tonumber(ARGV[3])
redis.call('HSET', KEYS[1], 'lease_expires_at', lease_expires,
  'heartbeat_at', now, 'updated_at', now)
redis.call('ZADD', KEYS[2], lease_expires, ARGV[2])
return 1
"""

RELEASE = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('HGET', KEYS[1], 'status') ~= 'running' or
   redis.call('HGET', KEYS[1], 'lease_token') ~= ARGV[1] then return -1 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if tonumber(redis.call('HGET', KEYS[1], 'lease_expires_at') or '0') <= now then return -1 end
redis.call('ZREM', KEYS[2], ARGV[2])
redis.call('HSET', KEYS[1], 'status', 'retrying', 'available_at', ARGV[3],
  'updated_at', now, 'lease_owner', '', 'lease_token', '',
  'lease_expires_at', '', 'heartbeat_at', '')
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[2])
return 1
"""


class RedisQueue:
    """At-least-once queue sharing the API's keys and job hash fields."""

    def __init__(self, client: Any, *, namespace: str, idempotency_ttl: int = 604800) -> None:
        self._client = client
        self._prefix = f"{namespace}:{{jobs}}"
        self._idempotency_ttl = idempotency_ttl

    @classmethod
    def connect(
        cls, redis_url: str, *, namespace: str, timeout_seconds: float = 5.0
    ) -> RedisQueue:
        return cls(
            Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=timeout_seconds,
                socket_timeout=timeout_seconds,
                health_check_interval=30,
            ),
            namespace=namespace,
        )

    def close(self) -> None:
        self._client.close()

    def healthcheck(self) -> None:
        try:
            if not self._client.ping() or int(self._client.eval("return 1", 0)) != 1:
                raise RuntimeError("Redis worker health check returned an invalid response")
        except RedisError as exc:
            raise RuntimeError(f"Redis worker health check failed: {exc}") from exc

    def enqueue(self, message: QueueMessage, available_at: datetime) -> QueueMessage:
        self._aware(available_at)
        payload = {
            "workspace_id": message.workspace_id,
            "run_id": message.run_id,
            "step_id": message.step_id,
        }
        encoded = self._json(payload)
        delay_ms = max(0, round((available_at - datetime.now(UTC)).total_seconds() * 1000))
        dedupe = message.idempotency_key or f"step:{message.workspace_id}:{message.step_id}"
        fingerprint = hashlib.sha256(
            self._json([message.queue_name, payload, 0, 100, delay_ms]).encode()
        ).hexdigest()
        digest = hashlib.sha256(dedupe.encode()).hexdigest()
        job_id = message.message_id or str(uuid4())
        response = self._client.eval(
            ENQUEUE, 3, self._job_key(job_id), self._ready_key(message.queue_name),
            self._idem_key(digest), job_id, fingerprint, message.queue_name, encoded,
            0, 100, delay_ms, self._idempotency_ttl,
        )
        code, resolved = int(response[0]), self._text(response[1])
        if code == -1:
            raise ValueError(f"queue idempotency key was reused with another payload: {dedupe}")
        if code == -2:
            raise RuntimeError("Redis queue job identifier collision")
        return replace(message, message_id=resolved)

    def reserve(
        self, queue_name: str, worker_id: str, now: datetime, lease_seconds: float = 30.0
    ) -> QueueDelivery | None:
        self._aware(now)
        receipt = str(uuid4())
        response = self._client.eval(
            CLAIM, 2, self._ready_key(queue_name), self._leases_key, self._prefix,
            worker_id, receipt, self._milliseconds(lease_seconds), 100, 100,
        )
        if not response:
            return None
        value = self._pairs(response)
        payload = json.loads(value["payload"])
        return QueueDelivery(
            message=QueueMessage(
                workspace_id=str(payload["workspace_id"]),
                run_id=str(payload["run_id"]),
                step_id=str(payload.get("step_id") or "__initialize__"),
                queue_name=value["queue_name"],
                message_id=value["id"],
            ),
            receipt=value["lease_token"],
            worker_id=value["lease_owner"],
            delivery_count=int(value["attempt_count"]),
            lease_expires_at=self._timestamp(value["lease_expires_at"]),
        )

    def reserve_payload(
        self, queue_name: str, worker_id: str, now: datetime, lease_seconds: float = 30.0
    ) -> PayloadDelivery | None:
        """Reserve a non-Run control-plane job without inventing run identifiers."""

        self._aware(now)
        receipt = str(uuid4())
        response = self._client.eval(
            CLAIM,
            2,
            self._ready_key(queue_name),
            self._leases_key,
            self._prefix,
            worker_id,
            receipt,
            self._milliseconds(lease_seconds),
            100,
            100,
        )
        if not response:
            return None
        value = self._pairs(response)
        payload = json.loads(value["payload"])
        if not isinstance(payload, dict):
            raise TypeError("queue payload must be an object")
        return PayloadDelivery(
            message_id=value["id"],
            queue_name=value["queue_name"],
            payload=payload,
            receipt=value["lease_token"],
            worker_id=value["lease_owner"],
            delivery_count=int(value["attempt_count"]),
            lease_expires_at=self._timestamp(value["lease_expires_at"]),
        )

    def ack_payload(self, delivery: PayloadDelivery) -> None:
        result = self._client.eval(
            ACK,
            2,
            self._job_key(delivery.message_id),
            self._leases_key,
            delivery.receipt,
            delivery.message_id,
            "{}",
        )
        if int(result) != 1:
            raise DeliveryLeaseLostError(
                f"Redis delivery lease is no longer active: {delivery.receipt}"
            )

    def renew_payload(
        self,
        delivery: PayloadDelivery,
        now: datetime,
        lease_seconds: float = 30.0,
    ) -> None:
        """Extend a generic payload lease while a long-running provider call is active."""

        self._aware(now)
        result = self._client.eval(
            RENEW,
            2,
            self._job_key(delivery.message_id),
            self._leases_key,
            delivery.receipt,
            delivery.message_id,
            self._milliseconds(lease_seconds),
        )
        if int(result) != 1:
            raise DeliveryLeaseLostError(
                f"Redis delivery lease is no longer active: {delivery.receipt}"
            )

    def release_payload(self, delivery: PayloadDelivery, available_at: datetime) -> None:
        self._aware(available_at)
        result = self._client.eval(
            RELEASE,
            3,
            self._job_key(delivery.message_id),
            self._leases_key,
            self._ready_key(delivery.queue_name),
            delivery.receipt,
            delivery.message_id,
            round(available_at.timestamp() * 1000),
        )
        if int(result) != 1:
            raise DeliveryLeaseLostError(
                f"Redis delivery lease is no longer active: {delivery.receipt}"
            )

    def ack(self, delivery: QueueDelivery) -> None:
        result = self._client.eval(
            ACK, 2, self._job_key(delivery.message.message_id), self._leases_key,
            delivery.receipt, delivery.message.message_id, "{}",
        )
        self._require_lease(result, delivery)

    def renew(
        self,
        delivery: QueueDelivery,
        now: datetime,
        lease_seconds: float = 30.0,
    ) -> None:
        self._aware(now)
        result = self._client.eval(
            RENEW,
            2,
            self._job_key(delivery.message.message_id),
            self._leases_key,
            delivery.receipt,
            delivery.message.message_id,
            self._milliseconds(lease_seconds),
        )
        self._require_lease(result, delivery)

    def release(self, delivery: QueueDelivery, available_at: datetime) -> None:
        self._aware(available_at)
        result = self._client.eval(
            RELEASE, 3, self._job_key(delivery.message.message_id), self._leases_key,
            self._ready_key(delivery.message.queue_name), delivery.receipt,
            delivery.message.message_id, round(available_at.timestamp() * 1000),
        )
        self._require_lease(result, delivery)

    @staticmethod
    def _require_lease(result: Any, delivery: QueueDelivery) -> None:
        if int(result) != 1:
            raise DeliveryLeaseLostError(
                f"Redis delivery lease is no longer active: {delivery.receipt}"
            )

    @staticmethod
    def _aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("queue timestamps must be timezone-aware")

    @staticmethod
    def _milliseconds(value: float) -> int:
        if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
            raise ValueError("lease duration must be a finite number")
        result = round(value * 1000)
        if result <= 0:
            raise ValueError("lease duration must be positive")
        return result

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    @classmethod
    def _pairs(cls, values: list[Any]) -> dict[str, str]:
        return {cls._text(values[i]): cls._text(values[i + 1])
                for i in range(0, len(values), 2)}

    @staticmethod
    def _timestamp(value: str) -> datetime:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)

    def _job_key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}"

    def _ready_key(self, queue_name: str) -> str:
        return f"{self._prefix}:ready:{queue_name}"

    def _idem_key(self, digest: str) -> str:
        return f"{self._prefix}:idempotency:{digest}"

    @property
    def _leases_key(self) -> str:
        return f"{self._prefix}:leases"
