from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkAccountSeed:
    key: str
    platform: str
    profile_url: str
    expected_nickname: str
    expected_red_id: str


BAIZHOU_XIAOXIONG = BenchmarkAccountSeed(
    key="baizhou-xiaoxiong",
    platform="xiaohongshu",
    profile_url="https://www.xiaohongshu.com/user/profile/5a8cf39111be10466d285d6b",
    expected_nickname="白昼小熊",
    expected_red_id="X20010906",
)

DEFAULT_BENCHMARK_ACCOUNT_SEEDS = (BAIZHOU_XIAOXIONG,)
