from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from framefactory_api.queue import IdempotencyConflictError, LeaseLostError
from framefactory_api.redis_config import RedisQueueSettings
from framefactory_api.redis_queue import RedisJobQueue
from framefactory_api.redis_scripts import CLAIM, ENQUEUE


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def watch(self, *keys: str) -> None:
        del keys

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.client.hashes.get(key, {}))

    async def time(self) -> tuple[int, int]:
        return self.client.time()

    def multi(self) -> None:
        return None

    def hset(self, key: str, *, mapping: dict[str, Any]) -> FakePipeline:
        self.commands.append(("hset", (key,), {"mapping": mapping}))
        return self

    def zadd(self, key: str, mapping: dict[str, int]) -> FakePipeline:
        self.commands.append(("zadd", (key, mapping), {}))
        return self

    def zrem(self, key: str, member: str) -> FakePipeline:
        self.commands.append(("zrem", (key, member), {}))
        return self

    async def execute(self) -> list[int]:
        results: list[int] = []
        for name, args, kwargs in self.commands:
            if name == "hset":
                key = cast(str, args[0])
                mapping = cast(dict[str, Any], kwargs["mapping"])
                self.client.hashes.setdefault(key, {}).update(
                    {field: str(value) for field, value in mapping.items()}
                )
                results.append(len(mapping))
            elif name == "zadd":
                key = cast(str, args[0])
                mapping = cast(dict[str, int], args[1])
                self.client.zsets.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            else:
                key, member = cast(tuple[str, str], args)
                existed = member in self.client.zsets.setdefault(key, {})
                self.client.zsets[key].pop(member, None)
                results.append(int(existed))
        return results


class FakeRedis:
    """Small deterministic fake for the Redis commands used by RedisJobQueue."""

    def __init__(self, *, now_ms: int = 1_800_000_000_000) -> None:
        self.now_ms = now_ms
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, int]] = {}
        self.closed = False

    def time(self) -> tuple[int, int]:
        return self.now_ms // 1000, (self.now_ms % 1000) * 1000

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction is True
        return FakePipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def eval(self, script: str, key_count: int, *values: Any) -> Any:
        if script == "return 1":
            assert key_count == 0
            return 1

        keys = [str(value) for value in values[:key_count]]
        arguments = [str(value) for value in values[key_count:]]
        if script == ENQUEUE:
            return self._enqueue(keys, arguments)
        if script == CLAIM:
            return self._claim(keys, arguments)
        raise AssertionError("unexpected Lua script")

    def _enqueue(self, keys: list[str], args: list[str]) -> list[Any]:
        job_key, ready_key, idempotency_key = keys
        job_id, fingerprint, queue_name, payload = args[:4]
        previous = self.hashes.get(idempotency_key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                return [-1, previous["job_id"]]
            return [0, previous["job_id"]]

        available_at = self.now_ms + int(args[6])
        self.hashes[job_key] = {
            "id": job_id,
            "queue_name": queue_name,
            "payload": payload,
            "status": "queued",
            "priority": args[4],
            "attempt_count": "0",
            "max_attempts": args[5],
            "available_at": str(available_at),
            "created_at": str(self.now_ms),
            "updated_at": str(self.now_ms),
            "lease_owner": "",
            "lease_token": "",
            "lease_expires_at": "",
            "heartbeat_at": "",
            "completed_at": "",
            "result": "",
            "error": "",
            "cancellation_requested": "0",
        }
        self.zsets.setdefault(ready_key, {})[job_id] = available_at
        self.hashes[idempotency_key] = {"job_id": job_id, "fingerprint": fingerprint}
        return [1, job_id]

    def _claim(self, keys: list[str], args: list[str]) -> list[str]:
        ready_key, leases_key = keys
        prefix, worker_id, lease_token = args[:3]
        available = [
            job_id
            for job_id, score in self.zsets.setdefault(ready_key, {}).items()
            if score <= self.now_ms
        ]
        if not available:
            return []
        job_id = max(
            available,
            key=lambda candidate: (
                int(self.hashes[f"{prefix}:job:{candidate}"]["priority"]),
                -int(self.hashes[f"{prefix}:job:{candidate}"]["available_at"]),
            ),
        )
        job_key = f"{prefix}:job:{job_id}"
        job = self.hashes[job_key]
        expires_at = self.now_ms + int(args[3])
        job.update(
            {
                "status": "running",
                "attempt_count": str(int(job["attempt_count"]) + 1),
                "lease_owner": worker_id,
                "lease_token": lease_token,
                "lease_expires_at": str(expires_at),
                "heartbeat_at": str(self.now_ms),
                "updated_at": str(self.now_ms),
            }
        )
        self.zsets[ready_key].pop(job_id)
        self.zsets.setdefault(leases_key, {})[job_id] = expires_at
        return [part for item in job.items() for part in item]


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def queue(fake_redis: FakeRedis) -> RedisJobQueue:
    settings = RedisQueueSettings(
        url="redis://127.0.0.1:6379/15",
        retry_base_seconds=0.25,
        retry_max_seconds=1,
    )
    return RedisJobQueue(cast(Any, fake_redis), settings)


def test_redis_url_supports_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "redis-url"
    secret.write_text("redis://:password@redis:6379/0\n", encoding="utf-8")
    monkeypatch.setenv("FRAMEFACTORY_REDIS_URL_FILE", str(secret))

    settings = RedisQueueSettings.from_environment()

    assert settings.url == "redis://:password@redis:6379/0"


@pytest.mark.asyncio
async def test_enqueue_claim_retry_and_ack_are_deterministic(
    queue: RedisJobQueue, fake_redis: FakeRedis
) -> None:
    created, was_created = await queue.enqueue(
        queue_name="render",
        payload={"run_id": "run-1"},
        deduplication_key="run-1:render",
        max_attempts=2,
    )
    duplicate, duplicate_created = await queue.enqueue(
        queue_name="render",
        payload={"run_id": "run-1"},
        deduplication_key="run-1:render",
        max_attempts=2,
    )
    assert was_created is True
    assert duplicate_created is False
    assert duplicate.id == created.id

    first_claim = await queue.claim(
        queue_name="render", worker_id="worker-a", lease_seconds=10
    )
    assert first_claim is not None
    assert first_claim.status == "running"
    assert first_claim.attempt_count == 1

    retried = await queue.retry(
        job_id=first_claim.id,
        lease_token=cast(str, first_claim.lease_token),
        error={"code": "TEMPORARY"},
    )
    assert retried.status == "retrying"
    assert retried.available_at.timestamp() * 1000 == fake_redis.now_ms + 250

    assert await queue.claim(
        queue_name="render", worker_id="worker-b", lease_seconds=10
    ) is None
    fake_redis.now_ms += 250
    second_claim = await queue.claim(
        queue_name="render", worker_id="worker-b", lease_seconds=10
    )
    assert second_claim is not None
    assert second_claim.attempt_count == 2

    completed = await queue.acknowledge(
        job_id=second_claim.id,
        lease_token=cast(str, second_claim.lease_token),
        result={"artifact_id": "asset-1"},
    )
    assert completed.status == "succeeded"
    assert completed.result == {"artifact_id": "asset-1"}
    assert completed.lease_token is None


@pytest.mark.asyncio
async def test_deduplication_key_rejects_different_job_definition(
    queue: RedisJobQueue,
) -> None:
    await queue.enqueue(
        queue_name="render", payload={"version": 1}, deduplication_key="stable-key"
    )
    with pytest.raises(IdempotencyConflictError):
        await queue.enqueue(
            queue_name="render", payload={"version": 2}, deduplication_key="stable-key"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["renew", "acknowledge", "retry"])
async def test_expired_lease_cannot_mutate_job(
    queue: RedisJobQueue, fake_redis: FakeRedis, transition: str
) -> None:
    job, _ = await queue.enqueue(
        queue_name="render", payload={}, deduplication_key=f"expired:{transition}"
    )
    claimed = await queue.claim(
        queue_name="render", worker_id="worker-a", lease_seconds=1
    )
    assert claimed is not None and claimed.id == job.id and claimed.lease_token
    fake_redis.now_ms += 1_000

    with pytest.raises(LeaseLostError):
        if transition == "renew":
            await queue.renew(
                job_id=job.id,
                lease_token=cast(str, claimed.lease_token),
                lease_seconds=10,
            )
        elif transition == "acknowledge":
            await queue.acknowledge(
                job_id=job.id, lease_token=cast(str, claimed.lease_token)
            )
        else:
            await queue.retry(
                job_id=job.id,
                lease_token=cast(str, claimed.lease_token),
                error={"code": "LATE"},
            )


@pytest.mark.asyncio
async def test_cancel_removes_ready_job(queue: RedisJobQueue, fake_redis: FakeRedis) -> None:
    job, _ = await queue.enqueue(
        queue_name="render", payload={}, deduplication_key="cancel-me"
    )

    cancelled = await queue.cancel(job.id)

    assert cancelled.status == "cancelled"
    assert cancelled.cancellation_requested is True
    assert all(job.id not in members for members in fake_redis.zsets.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"delay_seconds": float("nan")}, "finite number"),
        ({"lease_seconds": float("inf")}, "finite number"),
    ],
)
async def test_queue_rejects_non_finite_durations(
    queue: RedisJobQueue, kwargs: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        if "delay_seconds" in kwargs:
            await queue.enqueue(
                queue_name="render",
                payload={},
                deduplication_key="invalid-duration",
                **kwargs,
            )
        else:
            await queue.claim(queue_name="render", worker_id="worker", **kwargs)


@pytest.mark.asyncio
async def test_queue_rejects_non_standard_json_numbers(queue: RedisJobQueue) -> None:
    with pytest.raises(ValueError, match="JSON serializable"):
        await queue.enqueue(
            queue_name="render",
            payload={"progress": float("nan")},
            deduplication_key="invalid-json",
        )
