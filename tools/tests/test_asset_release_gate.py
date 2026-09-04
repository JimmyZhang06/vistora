from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

RELEASE_TOOLS = Path(__file__).resolve().parents[1] / "release"
sys.path.insert(0, str(RELEASE_TOOLS))

from asset_contract_gate import evaluate_contract
from asset_e2e_gate import walk_asset_pages
from asset_integrity_audit import evaluate_inventory
from asset_load_gate import (
    QueueObservation,
    _assert_fixture_records,
    assert_backpressure,
)
from release_gate_common import GateFailure


@dataclass
class _Response:
    payload: dict[str, Any]
    status: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload


class _PagedClient:
    def __init__(self, size: int, *, loop_cursor: bool = False) -> None:
        self.records = [
            {
                "id": f"asset-{index:05d}",
                "kind": "image" if index % 2 == 0 else "video",
                "status": "ready" if index % 5 else "quarantined",
                "copyright_status": "owned" if index % 7 else "unknown",
            }
            for index in range(size)
        ]
        self.loop_cursor = loop_cursor
        self.calls = 0

    def expect(self, method: str, path: str, statuses: set[int]) -> _Response:
        assert method == "GET"
        assert statuses == {200}
        self.calls += 1
        query = parse_qs(urlparse(path).query)
        limit = int(query["limit"][0])
        offset = int(query.get("cursor", ["0"])[0])
        values = self.records[offset : offset + limit]
        next_offset = offset + len(values)
        has_more = next_offset < len(self.records)
        cursor = str(offset if self.loop_cursor else next_offset) if has_more else None
        return _Response(
            {
                "data": values,
                "total_count": len(self.records),
                "page": {
                    "limit": limit,
                    "has_more": has_more,
                    "next_cursor": cursor,
                },
            }
        )


@pytest.mark.parametrize(("size", "pages"), [(1000, 10), (5000, 50)])
def test_repeatable_scale_fixture_walks_every_record_once(size: int, pages: int) -> None:
    client = _PagedClient(size)

    records = walk_asset_pages(
        client, query="/v1/assets?status=ready", expected_total=size, page_size=100
    )

    assert len(records) == size
    assert len({record["id"] for record in records}) == size
    assert client.calls == pages


def test_pagination_gate_rejects_cursor_loops() -> None:
    with pytest.raises(GateFailure, match="cursor"):
        walk_asset_pages(
            _PagedClient(1000, loop_cursor=True),
            query="/v1/assets",
            expected_total=1000,
        )


def test_5k_queue_fixture_proves_high_low_watermark_backpressure() -> None:
    observations = [QueueObservation(202, depth) for depth in range(1, 1001)]
    observations.extend(QueueObservation(429, 1000, "2") for _ in range(4000))
    observations.extend(
        (
            QueueObservation(200, 750, operation="status"),
            QueueObservation(202, 751),
        )
    )

    assert_backpressure(observations, high_watermark=1000, low_watermark=750)


def test_backpressure_gate_rejects_unbounded_queue() -> None:
    with pytest.raises(GateFailure, match="high watermark"):
        assert_backpressure(
            [QueueObservation(202, depth) for depth in range(1, 1002)],
            high_watermark=1000,
            low_watermark=750,
        )


def test_filter_gate_rejects_records_outside_fixture_or_filter() -> None:
    records = [
        {
            "id": "asset-1",
            "workspace_id": "workspace",
            "status": "quarantined",
            "metadata": {"release_fixture_id": "fixture-1"},
        }
    ]

    with pytest.raises(GateFailure, match="filter"):
        _assert_fixture_records(
            records,
            workspace_id="workspace",
            fixture_id="fixture-1",
            filters={"release_fixture_id": "fixture-1", "status": "ready"},
        )


def _complete_schema() -> dict[str, set[str]]:
    return {
        "assets": {"review_revision", "quarantine_reason", "deleted_at"},
        "asset_review_actions": {
            "workspace_id",
            "asset_id",
            "action",
            "review_revision",
            "analysis_id",
            "file_content_hash",
            "library_id",
            "copyright_status",
            "created_at",
        },
        "run_asset_snapshots": {
            "id",
            "workspace_id",
            "run_id",
            "asset_id",
            "file_id",
            "artifact_id",
            "library_id",
            "bucket",
            "object_key",
            "byte_size",
            "content_hash",
            "media_type",
            "copyright_status",
            "analysis_id",
            "analysis_version",
            "review_id",
            "review_revision",
        },
        "asset_segments": {
            "representative_frame_key",
            "representative_frame_bucket",
            "representative_frame_content_hash",
            "representative_frame_byte_size",
            "representative_frame_media_type",
        },
    }


def test_1247_quarantine_sentinel_and_locator_hashes_pass() -> None:
    locator = {
        "kind": "asset_file",
        "id": "file-1",
        "bucket": "framefactory",
        "object_key": "workspaces/ws/objects/one",
        "byte_size": 12,
        "content_hash": "a" * 64,
        "media_type": "image/png",
        "workspace_id": "ws",
    }
    inventory = {
        "counts": {
            "assets": 1248,
            "ready": 1,
            "quarantine": 1247,
            "unsafe_ready": 0,
            "completed_visual_analyses_missing_frames": 0,
            "worker_eligible_quarantine_sentinels": 0,
            "run_snapshot_quarantine_references": 0,
            "run_snapshot_semantic_mismatches": 0,
        },
        "locators": [locator],
    }
    heads = {
        locator["object_key"]: {
            "ContentLength": 12,
            "ContentType": "image/png",
            "Metadata": {"sha256": "a" * 64, "workspace-id": "ws"},
        }
    }

    assert evaluate_inventory(
        inventory,
        schema=_complete_schema(),
        expected_quarantine_count=1247,
        s3_keys=[locator["object_key"]],
        heads=heads,
    ) == []


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ({"quarantine": 1246, "unsafe_ready": 0}, "quarantine inventory mismatch"),
        ({"quarantine": 1247, "unsafe_ready": 1}, "unsafe ready assets"),
    ],
)
def test_quarantine_sentinel_fails_closed(counts: dict[str, int], message: str) -> None:
    findings = evaluate_inventory(
        {"counts": counts, "locators": []},
        schema=_complete_schema(),
        expected_quarantine_count=1247,
        s3_keys=[],
        heads={},
    )

    assert any(message in finding for finding in findings)


def test_quarantine_fixed_set_detects_one_for_one_replacement() -> None:
    findings = evaluate_inventory(
        {
            "counts": {
                "quarantine": 1247,
                "unsafe_ready": 0,
                "completed_visual_analyses_missing_frames": 0,
            },
            "locators": [],
            "quarantine_entries": [
                {"asset_id": "replacement", "file_id": "file-1", "content_hash": "a" * 64}
            ],
        },
        schema=_complete_schema(),
        expected_quarantine_count=1247,
        expected_quarantine_entries={("sentinel", "file-1", "a" * 64)},
        s3_keys=[],
        heads={},
    )

    assert any("sentinel ID/file/hash set mismatch" in finding for finding in findings)


def test_audit_rejects_rls_that_is_not_enabled_and_forced() -> None:
    findings = evaluate_inventory(
        {
            "counts": {
                "quarantine": 1247,
                "unsafe_ready": 0,
                "completed_visual_analyses_missing_frames": 0,
            },
            "locators": [],
        },
        schema=_complete_schema(),
        expected_quarantine_count=1247,
        s3_keys=[],
        heads={},
        rls_state={table: {"enabled": True, "forced": False} for table in _complete_schema()},
    )

    assert any("RLS is not enabled and forced" in finding for finding in findings)


def test_contract_evaluator_reports_api_database_worker_and_web_gaps() -> None:
    spec = {
        "api": {
            "required_operations": [
                {"path": "/v1/assets", "method": "get", "parameters": ["cursor"]}
            ],
            "required_tokens": ["awaiting_review"],
        },
        "database": {
            "required_tokens": ["asset_review_actions"],
            "forbidden_patterns": ["DISABLE ROW LEVEL SECURITY"],
        },
        "worker": {"required_tokens": ["asset_snapshot"]},
        "web": {"required_tokens": ["reviewAsset"]},
    }

    findings = evaluate_contract(
        spec,
        openapi={"paths": {}},
        openapi_text="",
        database_text="ALTER TABLE assets DISABLE ROW LEVEL SECURITY;",
        worker_text="",
        web_text="",
    )

    assert {finding.component for finding in findings} == {"api", "database", "worker", "web"}
