from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

RELEASE_TOOLS = Path(__file__).resolve().parents[3] / "tools" / "release"
sys.path.insert(0, str(RELEASE_TOOLS))

from api_e2e_gate import _review_step_path  # noqa: E402
from release_gate_common import GateFailure  # noqa: E402


@dataclass
class _Response:
    payload: dict

    def json(self) -> dict:
        return self.payload


class _Client:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.path: str | None = None

    def expect(self, method: str, path: str, statuses: set[int]) -> _Response:
        assert method == "GET"
        assert statuses == {200}
        self.path = path
        return _Response(
            {"data": self.steps, "page": {"limit": 100, "has_more": False}}
        )


def test_release_gate_addresses_the_concrete_review_step() -> None:
    client = _Client(
        [
            {"id": "run:render", "status": "succeeded"},
            {"id": "run:quality", "status": "awaiting_review"},
        ]
    )

    assert _review_step_path(client, "run") == "/v1/steps/run:quality/review"
    assert client.path == "/v1/steps?run_id=run&limit=100"


def test_release_gate_rejects_ambiguous_review_steps() -> None:
    client = _Client(
        [
            {"id": "run:q1", "status": "awaiting_review"},
            {"id": "run:q2", "status": "awaiting_review"},
        ]
    )

    with pytest.raises(GateFailure, match="exactly one"):
        _review_step_path(client, "run")
