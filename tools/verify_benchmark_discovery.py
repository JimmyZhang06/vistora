"""Opt-in, low-frequency real provider verification; never creates paid jobs.

Run with this checkout's apps/api/src on PYTHONPATH. Supply canonical profile
URLs explicitly; this tool does not select additional accounts or retain pages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from framefactory_api.benchmark_accounts import (
    BenchmarkAccountError,
    BenchmarkAccountGateway,
)
from framefactory_api.benchmark_note_sources import XiaohongshuAuthenticatedNoteProvider


async def verify(profiles: list[str], browser_origin: str, details: bool) -> list[dict]:
    gateway = BenchmarkAccountGateway(managed_browser_base_url=browser_origin)
    source = XiaohongshuAuthenticatedNoteProvider(
        managed_browser_base_url=browser_origin
    )
    records = []
    for url in profiles:
        record = {"checked_at": datetime.now(UTC).isoformat()}
        try:
            snapshot = await gateway.collect(
                "xiaohongshu", url, refresh_note_identity=True
            )
            record.update(
                {
                    "profile_user_id": snapshot.profile.user_id,
                    "discovery": "passed"
                    if snapshot.acquisition.unresolved_note_count == 0
                    else "partial",
                    "acquisition_method": snapshot.acquisition.method,
                    "identified": snapshot.acquisition.identified_note_count,
                    "unresolved": snapshot.acquisition.unresolved_note_count,
                    "video": snapshot.analysis.video_count,
                    "image": snapshot.analysis.image_count,
                    "unknown": snapshot.analysis.unknown_count,
                    "identity_error_code": snapshot.acquisition.identity_error_code,
                }
            )
            encoded = snapshot.model_dump_json().lower()
            assert not any(
                marker in encoded for marker in ("xsec_token=", "xhscdn", "cookie=")
            )
            record["credential_scan"] = "passed"
            record["details"] = []
            if details:
                for kind in ("video", "image"):
                    note = next(
                        (n for n in snapshot.notes if n.note_id and n.format == kind),
                        None,
                    )
                    if note is None:
                        record["details"].append(
                            {"kind": kind, "status": "skipped_no_sample"}
                        )
                        continue
                    try:
                        evidence = await asyncio.to_thread(
                            source.collect,
                            snapshot.profile.profile_url,
                            note.note_id,
                        )
                        assert evidence.profile_user_id == snapshot.profile.user_id
                        assert evidence.note_id == note.note_id
                        assert evidence.media.kind == kind
                        encoded = evidence.model_dump_json().lower()
                        assert not any(
                            x in encoded for x in ("xsec_token=", "xhscdn", "cookie=")
                        )
                        record["details"].append(
                            {
                                "kind": kind,
                                "status": "passed",
                                "note_id": evidence.note_id,
                                "duration_ms": evidence.media.duration_ms,
                                "image_count": evidence.media.image_count,
                                "trusted_media_origin": evidence.media.trusted_media_origin,
                                "video_available": evidence.media.video_available,
                            }
                        )
                    except BenchmarkAccountError as error:
                        record["details"].append(
                            {"kind": kind, "status": "blocked", "code": error.code}
                        )
        except BenchmarkAccountError as error:
            record.update({"discovery": "blocked", "code": error.code})
        records.append(record)
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--browser-origin", required=True)
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(verify(args.profile, args.browser_origin, args.details)),
            indent=2,
        )
    )
