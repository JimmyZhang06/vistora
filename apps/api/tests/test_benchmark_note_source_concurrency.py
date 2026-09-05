"""Real threads exercise cancellation and bounded queuing, without a browser provider."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from framefactory_api import benchmark_note_sources
from framefactory_api.benchmark_accounts import BenchmarkAccountError, metric_from_display
from framefactory_api.benchmark_note_sources import (
    BenchmarkMediaProbe,
    BenchmarkNoteSourceEvidence,
    BenchmarkNoteSourceGateway,
)

PROFILE_ID = "a" * 24
PROFILE = f"https://www.xiaohongshu.com/user/profile/{PROFILE_ID}"
NOTE = "b" * 24


class BlockingNoteProvider:
    platform = "xiaohongshu"

    def __init__(self, *, fail_first: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.calls = 0
        self.active = 0
        self.maximum = 0
        self.fail_first = fail_first

    def collect(self, profile_url: str, note_id: str) -> BenchmarkNoteSourceEvidence:
        assert profile_url == PROFILE
        with self.guard:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        self.started.set()
        try:
            if not self.release.wait(3):
                raise TimeoutError("Test did not release the collection thread")
            if self.fail_first and call == 1:
                raise ValueError("Explicit collection failure")
            return BenchmarkNoteSourceEvidence(
                profile_user_id=PROFILE_ID, note_id=note_id,
                canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                captured_at=datetime.now(UTC), title="Thread fixture",
                likes=metric_from_display("1"), collects=metric_from_display("1"),
                comments=metric_from_display("1"),
                media=BenchmarkMediaProbe(
                    kind="image", video_available=False, image_count=1,
                    trusted_media_origin=True,
                ),
                limitations=["Thread fixture; no platform request"],
            )
        finally:
            with self.guard:
                self.active -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_repeated_cancellation_keeps_thread_serialized_until_success_or_failure(fail_first):
    provider = BlockingNoteProvider(fail_first=fail_first)
    gateway = BenchmarkNoteSourceGateway(provider)
    first = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
    second = None
    try:
        assert await asyncio.to_thread(provider.started.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
        await asyncio.sleep(0.05)
        assert not first.done()
        assert not second.done()
        assert provider.calls == 1
        provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second).note_id == NOTE
        assert provider.maximum == 1
        assert provider.active == 0
    finally:
        provider.release.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_is_bounded_and_cancelled_waiters_reclaim_capacity():
    provider = BlockingNoteProvider()
    gateway = BenchmarkNoteSourceGateway(provider)
    first = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
    waiters = []
    try:
        assert await asyncio.to_thread(provider.started.wait, 1)
        waiters = [asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
                   for _ in range(31)]
        await asyncio.sleep(0)
        with pytest.raises(BenchmarkAccountError) as busy:
            await gateway.collect("xiaohongshu", PROFILE, NOTE)
        assert busy.value.code == "BENCHMARK_PROVIDER_BUSY"
        assert busy.value.retryable is True
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        replacement = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
        waiters.append(replacement)
        await asyncio.sleep(0)
        assert not replacement.done()
        assert provider.calls == 1
        provider.release.set()
        assert (await replacement).note_id == NOTE
        assert provider.maximum == 1
    finally:
        provider.release.set()
        await asyncio.gather(first, *waiters, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_request_times_out_without_starting_another_provider(monkeypatch):
    monkeypatch.setattr(benchmark_note_sources, "_NOTE_QUEUE_TIMEOUT_SECONDS", 0.02)
    provider = BlockingNoteProvider()
    gateway = BenchmarkNoteSourceGateway(provider)
    first = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
    try:
        assert await asyncio.to_thread(provider.started.wait, 1)
        with pytest.raises(BenchmarkAccountError) as busy:
            await gateway.collect("xiaohongshu", PROFILE, NOTE)
        assert busy.value.code == "BENCHMARK_PROVIDER_BUSY"
        assert provider.calls == 1
        provider.release.set()
        await first
        assert (await gateway.collect("xiaohongshu", PROFILE, NOTE)).note_id == NOTE
        assert provider.maximum == 1
    finally:
        provider.release.set()
        await asyncio.gather(first, return_exceptions=True)
