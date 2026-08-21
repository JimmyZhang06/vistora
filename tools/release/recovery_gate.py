"""Prove a leased worker job survives a real worker process interruption."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from release_gate_common import (
    ApiClient,
    GateBlocked,
    GateFailure,
    gate_main,
    idempotency_key,
    load_json,
    page_items,
    require_env,
    wait_for_run_status,
)


def _compose_args(action: str) -> list[str]:
    compose_file = Path(require_env("FF_RELEASE_COMPOSE_FILE")).resolve()
    if not compose_file.is_file():
        raise GateBlocked(f"compose file does not exist: {compose_file}")
    command = ["docker", "compose", "-f", str(compose_file)]
    project = os.environ.get("FF_RELEASE_COMPOSE_PROJECT", "").strip()
    if project:
        command.extend(["--project-name", project])
    service = os.environ.get("FF_RELEASE_WORKER_SERVICE", "worker").strip()
    command.extend([action, service] if action == "stop" else ["up", "-d", service])
    return command


def _run_command(command: list[str]) -> None:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise GateBlocked(f"cannot execute worker control command {command[0]!r}: {exc}") from exc
    if result.returncode:
        raise GateBlocked(
            f"worker control command failed ({result.returncode}): "
            f"{result.stderr[-1000:].strip()}"
        )


def _list(client: ApiClient, path: str) -> list[dict[str, Any]]:
    return page_items(client.expect("GET", path, {200}).json())


def _attempts(steps: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for step in steps:
        step_id = step.get("id")
        attempts = step.get("attempt_count")
        if isinstance(step_id, str) and isinstance(attempts, int):
            result[step_id] = attempts
    if not result:
        raise GateFailure("steps do not expose id and attempt_count recovery evidence")
    return result


def run_gate() -> dict[str, Any]:
    client = ApiClient.from_environment()
    payload = load_json(require_env("FF_RELEASE_RECOVERY_RUN_PAYLOAD_FILE"))
    response = client.expect(
        "POST", "/v1/runs", {200, 201}, payload=payload, idempotency=idempotency_key("recovery")
    ).json()
    if not isinstance(response, dict) or not isinstance(response.get("id"), str):
        raise GateFailure("create recovery run response has no string id")
    run_id = response["id"]
    wait_for_run_status(
        client,
        run_id,
        {"running"},
        timeout=float(os.environ.get("FF_RELEASE_RECOVERY_START_TIMEOUT_SECONDS", "300")),
    )
    before = _attempts(_list(client, f"/v1/steps?run_id={run_id}&limit=100"))

    stopped = False
    try:
        _run_command(_compose_args("stop"))
        stopped = True
        time.sleep(float(os.environ.get("FF_RELEASE_LEASE_EXPIRY_WAIT_SECONDS", "35")))
    finally:
        if stopped:
            _run_command(_compose_args("start"))

    wait_for_run_status(
        client,
        run_id,
        {"succeeded"},
        timeout=float(os.environ.get("FF_RELEASE_RECOVERY_FINISH_TIMEOUT_SECONDS", "1800")),
    )
    after_steps = _list(client, f"/v1/steps?run_id={run_id}&limit=100")
    after = _attempts(after_steps)
    if not any(after.get(step_id, -1) > count for step_id, count in before.items()):
        events = _list(client, f"/v1/events?run_id={run_id}&limit=100")
        types = {event.get("type") or event.get("event_type") for event in events}
        if "step.retrying" not in types:
            raise GateFailure("no increased attempt_count or durable step.retrying recovery evidence")
    if any(step.get("status") not in {"succeeded", "cancelled"} for step in after_steps):
        raise GateFailure("recovered run finished with non-terminal steps")

    artifacts = _list(client, f"/v1/artifacts?run_id={run_id}&limit=100")
    keys = [artifact.get("object_key") for artifact in artifacts]
    if not keys or any(not isinstance(key, str) for key in keys):
        raise GateFailure("recovered run produced no object-backed artifact metadata")
    if len(keys) != len(set(keys)):
        raise GateFailure("worker recovery produced duplicate artifact object keys")
    if any(not artifact.get("content_hash") for artifact in artifacts):
        raise GateFailure("recovered run has artifacts without content hashes")

    return {
        "run_id": run_id,
        "worker_interruption": "verified",
        "steps": len(after_steps),
        "artifacts": len(artifacts),
    }


if __name__ == "__main__":
    gate_main(run_gate)
