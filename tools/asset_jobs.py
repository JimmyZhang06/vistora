"""Dry-run-first canary control for the recoverable asset analysis worker.

This tool never downloads media from the internet.  It reads only immutable
objects already registered in ``asset_files``.  Mutating commands are locked to
database names containing ``test`` so this repository task cannot process the
user's live 1,247 quarantined assets.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = PROJECT_ROOT / "services" / "worker"
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

from framefactory.worker.adapters.asset_analysis import (
    ClamAVScanner,
    CommandVisionAnalyzer,
    LocalAssetStageProcessor,
    S3MediaObjectStore,
)
from framefactory.worker.adapters.postgres_asset_jobs import (
    PostgresAssetJobRepository,
)
from framefactory.worker.asset_pipeline import AssetAnalysisRunner


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="asset-jobs")
    value.add_argument(
        "--database-url",
        default=os.getenv("FRAMEFACTORY_DATABASE_URL", ""),
        help="PostgreSQL URL; mutating commands accept test databases only",
    )
    commands = value.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="read-only dry-run candidate preview")
    _selection_arguments(plan)

    create = commands.add_parser("create", help="create a persisted test-data canary batch")
    _selection_arguments(create)
    create.add_argument("--idempotency-key", required=True)
    create.add_argument("--rate-limit", type=int, default=30)
    create.add_argument("--max-attempts", type=int, default=3)
    create.add_argument("--confirm-test-data", action="store_true")

    run = commands.add_parser("run", help="run/recover one persisted test-data batch")
    run.add_argument("batch_id")
    run.add_argument("--maximum-items", type=int)
    run.add_argument("--rate-limit", type=int, default=30)
    run.add_argument("--lease-seconds", type=float, default=300.0)
    run.add_argument("--confirm-test-data", action="store_true")

    status = commands.add_parser("status", help="refresh and print durable counters")
    status.add_argument("batch_id")

    cancel = commands.add_parser("cancel", help="cancel pending and cooperatively stop running items")
    cancel.add_argument("batch_id")
    cancel.add_argument("--confirm-test-data", action="store_true")

    retry = commands.add_parser("retry", help="retry failed items with attempts remaining")
    retry.add_argument("batch_id")
    retry.add_argument("--confirm-test-data", action="store_true")

    review = commands.add_parser("review", help="record one explicit asset review decision")
    review.add_argument("item_id")
    review.add_argument("--decision", choices=("approve", "reject", "request_changes"), required=True)
    review.add_argument("--actor-id")
    review.add_argument("--comment", default="")
    review.add_argument("--confirm-test-data", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if not arguments.database_url:
        return _error("FRAMEFACTORY_DATABASE_URL or --database-url is required")
    repository = PostgresAssetJobRepository(arguments.database_url)
    try:
        if arguments.command == "plan":
            candidates = repository.plan(
                arguments.workspace_id,
                limit=arguments.limit,
                library_id=arguments.library_id,
            )
            _print({
                "mode": "dry-run",
                "candidate_count": len(candidates),
                "limit": arguments.limit,
                "candidates": candidates,
                "writes_performed": False,
            })
            return 0
        if arguments.command == "status":
            _print(asdict(repository.progress(arguments.batch_id)))
            return 0
        _require_test_data(arguments.database_url, arguments.confirm_test_data)
        if arguments.command == "create":
            batch_id = repository.create_batch(
                arguments.workspace_id,
                idempotency_key=arguments.idempotency_key,
                limit=arguments.limit,
                rate_limit_per_minute=arguments.rate_limit,
                library_id=arguments.library_id,
                max_attempts=arguments.max_attempts,
            )
            _print({"batch_id": batch_id, "mode": "test-data-canary", "auto_ready": False})
            return 0
        if arguments.command == "cancel":
            repository.request_cancel(arguments.batch_id)
            _print({"batch_id": arguments.batch_id, "cancel_requested": True})
            return 0
        if arguments.command == "retry":
            _print({"batch_id": arguments.batch_id, "retried": repository.retry_failed(arguments.batch_id)})
            return 0
        if arguments.command == "review":
            repository.review(
                arguments.item_id,
                decision=arguments.decision,
                actor_id=arguments.actor_id,
                comment=arguments.comment,
            )
            _print({"item_id": arguments.item_id, "decision": arguments.decision})
            return 0
        if arguments.command == "run":
            processor = _processor()
            try:
                runner = AssetAnalysisRunner(
                    repository,
                    processor,
                    os.getenv("FRAMEFACTORY_ASSET_JOB_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}"),
                    lease_seconds=arguments.lease_seconds,
                    rate_limit_per_minute=arguments.rate_limit,
                )
                progress = runner.run_batch(arguments.batch_id, maximum_items=arguments.maximum_items)
            finally:
                processor.close()
            _print(asdict(progress))
            return 0
    except (KeyError, RuntimeError, ValueError) as exc:
        return _error(str(exc))
    return _error("unsupported command")


def _processor() -> LocalAssetStageProcessor:
    vision_command = os.getenv("FRAMEFACTORY_ASSET_VISION_COMMAND", "").strip()
    bucket = os.getenv("FRAMEFACTORY_S3_BUCKET", "").strip()
    if not vision_command or not bucket:
        raise ValueError("run requires FRAMEFACTORY_ASSET_VISION_COMMAND and FRAMEFACTORY_S3_BUCKET")
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("FRAMEFACTORY_S3_ENDPOINT_URL") or None,
        region_name=os.getenv("FRAMEFACTORY_S3_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("FRAMEFACTORY_S3_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.getenv("FRAMEFACTORY_S3_SECRET_ACCESS_KEY") or None,
    )
    return LocalAssetStageProcessor(
        S3MediaObjectStore(client),
        ClamAVScanner(os.getenv("FRAMEFACTORY_MALWARE_SCAN_COMMAND", "clamscan")),
        CommandVisionAnalyzer(vision_command),
        ffprobe_command=os.getenv("FRAMEFACTORY_FFPROBE_COMMAND", "ffprobe"),
        ffmpeg_command=os.getenv("FRAMEFACTORY_FFMPEG_COMMAND", "ffmpeg"),
    )


def _selection_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--workspace-id", required=True)
    command.add_argument("--library-id")
    command.add_argument("--limit", type=int, default=10, choices=range(1, 101))


def _require_test_data(database_url: str, confirmed: bool) -> None:
    database = urlsplit(database_url).path.rsplit("/", 1)[-1].lower()
    if not confirmed or "test" not in database:
        raise ValueError(
            "mutating asset jobs require --confirm-test-data and a database name containing 'test'"
        )


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def _error(message: str) -> int:
    print(json.dumps({"status": "error", "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
