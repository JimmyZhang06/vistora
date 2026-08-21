from __future__ import annotations

import io
import unittest
from unittest.mock import Mock, patch

from framefactory.worker.__main__ import _build_pool, main
from framefactory.worker.config import WorkerSettings


class WorkerCliTests(unittest.TestCase):
    @patch("framefactory.worker.__main__.build_service")
    @patch("framefactory.worker.__main__.WorkerSettings.from_environment")
    def test_healthcheck_checks_dependencies_and_closes(self, settings, build) -> None:
        service = Mock()
        settings.return_value = Mock()
        build.return_value = service
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["healthcheck"])
        self.assertEqual(0, result)
        service.healthcheck.assert_called_once_with()
        service.close.assert_called_once_with()
        self.assertIn('"status": "ok"', output.getvalue())

    @patch("framefactory.worker.__main__.WorkerSettings.from_environment")
    def test_invalid_configuration_exits_before_connecting(self, settings) -> None:
        settings.side_effect = ValueError("missing database")
        error = io.StringIO()
        with patch("sys.stderr", error):
            result = main(["run"])
        self.assertEqual(2, result)
        self.assertIn("missing database", error.getvalue())

    @patch("framefactory.worker.__main__.build_service")
    def test_pool_builds_independent_workers_with_unique_ids(self, build) -> None:
        build.side_effect = [Mock(), Mock(), Mock()]
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
            worker_concurrency=3,
        )
        services = _build_pool(settings)
        self.assertEqual(3, len(services))
        self.assertEqual(
            ["worker-test-01", "worker-test-02", "worker-test-03"],
            [call.args[0].worker_id for call in build.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
