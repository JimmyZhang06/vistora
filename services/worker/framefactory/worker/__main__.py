from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Sequence
from dataclasses import replace

from .config import WorkerSettings
from .poster_backfill import PosterBackfill, result_json
from .service import WorkerService, build_service


def _build_pool(settings: WorkerSettings) -> list[WorkerService]:
    services: list[WorkerService] = []
    try:
        for index in range(settings.worker_concurrency):
            member = replace(
                settings,
                worker_id=(
                    settings.worker_id
                    if settings.worker_concurrency == 1
                    else f"{settings.worker_id}-{index + 1:02d}"
                ),
            )
            services.append(build_service(member))
        return services
    except Exception:
        for service in services:
            service.close()
        raise


async def _run_pool(services: list[WorkerService]) -> None:
    await asyncio.gather(*(service.run_forever() for service in services))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="framefactory-worker")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("run", help="run until SIGINT or SIGTERM")
    subcommands.add_parser("healthcheck", help="validate config and both infrastructure dependencies")
    posters = subcommands.add_parser(
        "backfill-posters",
        help="scan historical assets and generate missing representative posters",
    )
    posters.add_argument("--limit", type=int, default=100)
    posters.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    posters.add_argument("--apply", action="store_true", help="perform writes; otherwise plan only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = WorkerSettings.from_environment()
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    services: list[WorkerService] = []
    try:
        if arguments.command == "backfill-posters":
            backfill = PosterBackfill.from_settings(settings)
            if not arguments.apply:
                candidates = backfill.plan(limit=arguments.limit)
                print(json.dumps({"mode": "plan", "selected": len(candidates)}))
                return 0
            print(result_json(backfill.run(limit=arguments.limit, workers=arguments.workers)))
            return 0
        if arguments.command == "healthcheck":
            service = build_service(settings)
            services.append(service)
            service.healthcheck()
            print(json.dumps({"status": "ok", "service": "framefactory-worker"}))
            return 0
        services = _build_pool(settings)

        def stop_pool(*_: object) -> None:
            for worker_service in services:
                worker_service.stop()

        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop_pool)
        logging.getLogger("framefactory.worker").info(
            "starting worker pool concurrency=%s", len(services)
        )
        asyncio.run(_run_pool(services))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger("framefactory.worker").exception("worker terminated")
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        for service in services:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
