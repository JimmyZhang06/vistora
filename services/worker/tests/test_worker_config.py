from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from framefactory.worker.config import WorkerSettings


class WorkerSettingsTests(unittest.TestCase):
    def test_required_infrastructure_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "FRAMEFACTORY_DATABASE_URL"):
            WorkerSettings.from_environment({})

    def test_production_rejects_plaintext_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "production PostgreSQL requires"):
            WorkerSettings.from_environment(
                {
                    "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
                    "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
                }
            )

    def test_development_accepts_local_dependencies_and_shared_api_namespace(self) -> None:
        settings = WorkerSettings.from_environment(
            {
                "FRAMEFACTORY_ENV": "development",
                "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
                "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
                "FRAMEFACTORY_WORKER_ID": "worker-test",
            }
        )
        self.assertEqual("framefactory", settings.redis_namespace)
        self.assertEqual("runs", settings.intake_queue_name)
        self.assertEqual("run-steps", settings.queue_name)
        self.assertEqual(1, settings.worker_concurrency)

    def test_non_finite_and_invalid_queue_values_are_rejected(self) -> None:
        base = {
            "FRAMEFACTORY_ENV": "test",
            "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
            "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
            "FRAMEFACTORY_WORKER_ID": "worker-test",
        }
        with self.assertRaisesRegex(ValueError, "positive finite"):
            WorkerSettings.from_environment(
                {**base, "FRAMEFACTORY_WORKER_LEASE_SECONDS": "nan"}
            )
        with self.assertRaisesRegex(ValueError, "FRAMEFACTORY_RUN_QUEUE"):
            WorkerSettings.from_environment({**base, "FRAMEFACTORY_RUN_QUEUE": "bad queue"})
        with self.assertRaisesRegex(ValueError, "between 1 and 32"):
            WorkerSettings.from_environment(
                {**base, "FRAMEFACTORY_WORKER_CONCURRENCY": "33"}
            )

    def test_worker_concurrency_is_explicit_and_bounded(self) -> None:
        settings = WorkerSettings.from_environment(
            {
                "FRAMEFACTORY_ENV": "test",
                "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
                "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
                "FRAMEFACTORY_WORKER_ID": "worker-test",
                "FRAMEFACTORY_WORKER_CONCURRENCY": "8",
            }
        )
        self.assertEqual(8, settings.worker_concurrency)

    def test_provider_configuration_is_complete_and_secret_safe(self) -> None:
        base = {
            "FRAMEFACTORY_ENV": "test",
            "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
            "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
            "FRAMEFACTORY_WORKER_ID": "worker-test",
            "FRAMEFACTORY_OPENAI_BASE_URL": "https://models.example/v1",
            "FRAMEFACTORY_OPENAI_API_KEY": "do-not-print-this",
            "FRAMEFACTORY_OPENAI_RESEARCH_MODEL": "research",
            "FRAMEFACTORY_OPENAI_WRITING_MODEL": "writing",
            "FRAMEFACTORY_OPENAI_QUALITY_MODEL": "quality",
        }
        with self.assertRaisesRegex(ValueError, "S3_BUCKET"):
            WorkerSettings.from_environment(base)

        settings = WorkerSettings.from_environment(
            {**base, "FRAMEFACTORY_S3_BUCKET": "artifacts"}
        )
        self.assertIsNotNone(settings.openai_compatible)
        self.assertNotIn("do-not-print-this", repr(settings))

    def test_provider_secrets_support_files_and_reject_ambiguous_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / "model-key"
            secret_file.write_text("file-secret\n", encoding="utf-8")
            base = {
                "FRAMEFACTORY_ENV": "test",
                "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
                "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
                "FRAMEFACTORY_WORKER_ID": "worker-test",
                "FRAMEFACTORY_OPENAI_BASE_URL": "https://models.example/v1",
                "FRAMEFACTORY_OPENAI_API_KEY_FILE": str(secret_file),
                "FRAMEFACTORY_OPENAI_RESEARCH_MODEL": "research",
                "FRAMEFACTORY_OPENAI_WRITING_MODEL": "writing",
                "FRAMEFACTORY_OPENAI_QUALITY_MODEL": "quality",
                "FRAMEFACTORY_S3_BUCKET": "artifacts",
            }
            settings = WorkerSettings.from_environment(base)
            self.assertEqual("file-secret", settings.openai_compatible.api_key)
            with self.assertRaisesRegex(ValueError, "only one"):
                WorkerSettings.from_environment(
                    {**base, "FRAMEFACTORY_OPENAI_API_KEY": "direct-secret"}
                )

    def test_asset_asr_configuration_is_complete_and_secret_safe(self) -> None:
        base = {
            "FRAMEFACTORY_ENV": "test",
            "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
            "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
            "FRAMEFACTORY_WORKER_ID": "worker-test",
            "FRAMEFACTORY_S3_BUCKET": "artifacts",
            "FRAMEFACTORY_ASSET_VISION_BASE_URL": "https://vision.example/v1",
            "FRAMEFACTORY_ASSET_VISION_API_KEY": "dummy-vision-key",
            "FRAMEFACTORY_ASSET_VISION_MODEL": "vision-model",
            "FRAMEFACTORY_ASR_BASE_URL": "https://speech.example/v1",
            "FRAMEFACTORY_ASR_API_KEY": "dummy-asr-key",
            "FRAMEFACTORY_ASR_MODEL": "word-timestamp-model",
        }
        settings = WorkerSettings.from_environment(base)
        self.assertIsNotNone(settings.asset_analysis)
        self.assertEqual("word-timestamp-model", settings.asset_analysis.asr_model)
        self.assertEqual("word", settings.asset_analysis.asr_timestamp_mode)
        self.assertNotIn("dummy-asr-key", repr(settings))
        with self.assertRaisesRegex(ValueError, "must be configured together"):
            WorkerSettings.from_environment({**base, "FRAMEFACTORY_ASR_MODEL": ""})

        text_only = WorkerSettings.from_environment(
            {
                **base,
                "FRAMEFACTORY_ASR_RESPONSE_FORMAT": "json",
                "FRAMEFACTORY_ASR_TIMESTAMP_MODE": "none",
            }
        )
        self.assertEqual("none", text_only.asset_analysis.asr_timestamp_mode)
        with self.assertRaisesRegex(ValueError, "cannot promise timestamp"):
            WorkerSettings.from_environment(
                {**base, "FRAMEFACTORY_ASR_RESPONSE_FORMAT": "json"}
            )


if __name__ == "__main__":
    unittest.main()
