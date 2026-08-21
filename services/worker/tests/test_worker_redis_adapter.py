from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from framefactory.ports import DeliveryLeaseLostError, QueueMessage
from framefactory.worker.adapters.redis_queue import (
    ACK,
    CLAIM,
    ENQUEUE,
    RELEASE,
    RENEW,
    RedisQueue,
)


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.responses: list[object] = []

    def eval(self, *arguments: object) -> object:
        self.calls.append(arguments)
        return self.responses.pop(0)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class RedisQueueAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        self.queue = RedisQueue(self.redis, namespace="framefactory")
        self.now = datetime.now(UTC)

    def test_enqueue_uses_control_api_keyspace_and_payload_shape(self) -> None:
        self.redis.responses.append([1, "job-1"])
        saved = self.queue.enqueue(
            QueueMessage(
                workspace_id="workspace",
                run_id="run",
                step_id="run:research",
                queue_name="run-steps",
                idempotency_key="workspace:run:research:1",
            ),
            self.now,
        )
        call = self.redis.calls[0]
        self.assertEqual(ENQUEUE, call[0])
        self.assertIn("framefactory:{jobs}:ready:run-steps", call)
        payload = json.loads(next(value for value in call if isinstance(value, str) and value.startswith("{")))
        self.assertEqual("run:research", payload["step_id"])
        self.assertEqual("job-1", saved.message_id)

    def test_claim_accepts_api_run_payload_without_step_id(self) -> None:
        expires = round((self.now + timedelta(seconds=30)).timestamp() * 1000)
        self.redis.responses.append(
            [
                "id", "api-job", "queue_name", "runs",
                "payload", '{"workspace_id":"workspace","run_id":"run"}',
                "lease_token", "receipt", "lease_owner", "worker",
                "attempt_count", "1", "lease_expires_at", str(expires),
            ]
        )
        delivery = self.queue.reserve("runs", "worker", self.now)
        assert delivery is not None
        self.assertEqual(CLAIM, self.redis.calls[0][0])
        self.assertEqual("__initialize__", delivery.message.step_id)
        self.assertEqual("api-job", delivery.message.message_id)

    def test_ack_and_release_are_fenced_by_redis_lease(self) -> None:
        expires = round((self.now + timedelta(seconds=30)).timestamp() * 1000)
        self.redis.responses.append(
            [
                "id", "job", "queue_name", "run-steps",
                "payload", '{"workspace_id":"w","run_id":"r","step_id":"r:s"}',
                "lease_token", "receipt", "lease_owner", "worker",
                "attempt_count", "1", "lease_expires_at", str(expires),
            ]
        )
        delivery = self.queue.reserve("run-steps", "worker", self.now)
        assert delivery is not None
        self.redis.responses.extend([1, -1])
        self.queue.ack(delivery)
        self.assertEqual(ACK, self.redis.calls[1][0])
        with self.assertRaises(DeliveryLeaseLostError):
            self.queue.release(delivery, self.now + timedelta(seconds=1))
        self.assertEqual(RELEASE, self.redis.calls[2][0])

    def test_payload_claim_preserves_asset_analysis_identifiers(self) -> None:
        expires = round((self.now + timedelta(seconds=30)).timestamp() * 1000)
        self.redis.responses.append(
            [
                "id", "analysis-job", "queue_name", "asset-analysis",
                "payload", '{"workspace_id":"w","asset_id":"a","job_id":"j"}',
                "lease_token", "receipt", "lease_owner", "worker",
                "attempt_count", "1", "lease_expires_at", str(expires),
            ]
        )

        delivery = self.queue.reserve_payload("asset-analysis", "worker", self.now)

        assert delivery is not None
        self.assertEqual("a", delivery.payload["asset_id"])
        self.redis.responses.append(1)
        self.queue.renew_payload(delivery, self.now, lease_seconds=60)
        self.assertEqual(RENEW, self.redis.calls[1][0])
        self.redis.responses.append(1)
        self.queue.ack_payload(delivery)
        self.assertEqual(ACK, self.redis.calls[2][0])


if __name__ == "__main__":
    unittest.main()
