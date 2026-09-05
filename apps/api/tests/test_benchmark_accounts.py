from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import SCHEMA_DIR
from fastapi.testclient import TestClient
from jsonschema.validators import validator_for
from referencing import Registry, Resource

from framefactory_api.benchmark_accounts import (
    BenchmarkAccountError,
    BenchmarkAccountGateway,
    XiaohongshuPublicProfileProvider,
    metric_from_display,
    parse_public_profile_html,
    resolve_xiaohongshu_profile,
)
from framefactory_api.benchmark_media_reports import (
    BenchmarkMediaEvidence,
    benchmark_deep_report_demo,
    build_benchmark_deep_note_report,
    timeline_from_worker_analysis,
)
from framefactory_api.benchmark_note_sources import (
    BenchmarkMediaProbe,
    BenchmarkNoteSourceEvidence,
    BenchmarkNoteSourceGateway,
    XiaohongshuAuthenticatedNoteProvider,
)
from framefactory_api.benchmark_reports import build_benchmark_account_report
from framefactory_api.benchmark_seeds import BAIZHOU_XIAOXIONG
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository


def _profile_html(*, user_id: str = "5a8cf39111be10466d285d6b") -> bytes:
    state = {
        "user": {
            "userPageData": {
                "basicInfo": {"nickname": "白昼小熊", "redId": "X20010906"},
                "interactions": [
                    {"type": "follows", "count": "220"},
                    {"type": "fans", "count": "419.1万"},
                    {"type": "interaction", "count": "999万+"},
                ],
                "tags": [{"name": "潮流博主"}, {"name": "时尚博主"}],
            },
            "notes": [
                [
                    {
                        "noteCard": {
                            "noteId": "111111111111111111111111",
                            "displayTitle": "1️⃣6️⃣个旅行转场",
                            "type": "video",
                            "time": 1786020664000,
                            "user": {"userId": user_id},
                            "interactInfo": {"likedCount": "10万+", "sticky": True},
                        }
                    },
                    {
                        "noteCard": {
                            "noteId": "222222222222222222222222",
                            "displayTitle": "晒晒清迈的阳光",
                            "type": "video",
                            "time": 1787972870000,
                            "user": {"userId": user_id},
                            "interactInfo": {"likedCount": "7.2万", "sticky": False},
                        }
                    },
                    {
                        "noteCard": {
                            "noteId": "333333333333333333333333",
                            "displayTitle": "只要我们还在感受自然",
                            "type": "normal",
                            "time": 1787539262000,
                            "user": {"userId": user_id},
                            "interactInfo": {"likedCount": "4.5万", "sticky": False},
                        }
                    },
                ]
            ],
            "noteQueries": [{"hasMore": True}],
        }
    }
    return (
        "<html><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + "</script></html>"
    ).encode()


def test_metric_parser_preserves_lower_bound_semantics() -> None:
    exact = metric_from_display("7.2万")
    bounded = metric_from_display("10万+")

    assert exact.lower_bound == 72_000
    assert exact.precision == "rounded"
    assert bounded.lower_bound == 100_000
    assert bounded.precision == "lower_bound"


def test_profile_parser_builds_an_honest_initial_page_analysis() -> None:
    captured_at = datetime(2026, 9, 5, tzinfo=UTC)
    snapshot = parse_public_profile_html(
        _profile_html(),
        profile_url=BAIZHOU_XIAOXIONG.profile_url,
        expected_user_id="5a8cf39111be10466d285d6b",
        captured_at=captured_at,
    )

    assert snapshot.profile.nickname == "白昼小熊"
    assert snapshot.notes[0].note_id == "111111111111111111111111"
    assert snapshot.profile.red_id == "X20010906"
    assert snapshot.profile.followers.lower_bound == 4_191_000
    assert snapshot.analysis.sample_size == 3
    assert snapshot.analysis.video_count == 2
    assert snapshot.analysis.image_count == 1
    assert snapshot.analysis.median_likes_lower_bound == 72_000
    assert snapshot.analysis.themes[0].theme in {"夏日与自然", "旅行与城市", "镜头与转场"}
    assert snapshot.acquisition.initial_page_has_more is True
    assert snapshot.acquisition.completeness == "initial_page_sample"


def test_report_engine_separates_observation_inference_and_limitations() -> None:
    snapshot = parse_public_profile_html(
        _profile_html(),
        profile_url=BAIZHOU_XIAOXIONG.profile_url,
        expected_user_id="5a8cf39111be10466d285d6b",
        captured_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    report = build_benchmark_account_report(snapshot)

    assert report.account_report.sample_size == 3
    assert report.account_report.evidence_depth == "public_metadata_only"
    assert report.account_report.top_candidate_note_indexes
    assert report.account_report.title_patterns
    assert report.note_reports[0].performance.tier == "top_candidate"
    assert report.note_reports[0].strategy_signals
    assert {item.level for item in report.note_reports[0].viral_mechanisms} >= {
        "derived",
        "inference",
        "limitation",
    }
    assert "ASR" in report.note_reports[0].limitations[1]


def test_profile_parser_accepts_javascript_undefined_without_rewriting_strings() -> None:
    payload = _profile_html().replace(
        b'"user": {',
        b'"global": {"missing": undefined, "literal": "undefined"}, "user": {',
        1,
    )
    snapshot = parse_public_profile_html(
        payload,
        profile_url=BAIZHOU_XIAOXIONG.profile_url,
        expected_user_id="5a8cf39111be10466d285d6b",
    )

    assert snapshot.profile.nickname == "白昼小熊"


def test_profile_parser_rejects_a_mismatched_account() -> None:
    with pytest.raises(BenchmarkAccountError) as error:
        parse_public_profile_html(
            _profile_html(user_id="000000000000000000000000"),
            profile_url=BAIZHOU_XIAOXIONG.profile_url,
            expected_user_id="5a8cf39111be10466d285d6b",
        )

    assert error.value.code == "BENCHMARK_PROFILE_MISMATCH"


@pytest.mark.asyncio
async def test_gateway_uses_a_short_lived_process_cache() -> None:
    calls = 0

    def fetcher(url: str, timeout_seconds: float, max_bytes: int) -> bytes:
        nonlocal calls
        calls += 1
        assert url == BAIZHOU_XIAOXIONG.profile_url
        assert timeout_seconds == 2
        assert max_bytes == 100_000
        return _profile_html()

    gateway = BenchmarkAccountGateway(
        fetcher=fetcher,
        timeout_seconds=2,
        max_response_bytes=100_000,
        cache_ttl_seconds=60,
    )
    first = await gateway.collect(
        "xiaohongshu",
        f"{BAIZHOU_XIAOXIONG.profile_url}?xsec_token=discard-me",
    )
    second = await gateway.collect("xiaohongshu", BAIZHOU_XIAOXIONG.profile_url)

    assert calls == 1
    assert first.acquisition.from_cache is False
    assert second.acquisition.from_cache is True


@pytest.mark.asyncio
async def test_gateway_falls_back_to_authenticated_browser_without_hiding_method() -> None:
    calls: list[str] = []

    def public(_url: str, _timeout: float, _limit: int) -> bytes:
        calls.append("public")
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_UNAVAILABLE",
            "source redirected to sign-in",
            retryable=True,
        )

    def authenticated(_url: str, _timeout: float, _limit: int) -> bytes:
        calls.append("authenticated")
        return _profile_html()

    gateway = BenchmarkAccountGateway(
        providers=(
            XiaohongshuPublicProfileProvider(
                fetcher=public,
                authenticated_fetcher=authenticated,
            ),
        )
    )
    snapshot = await gateway.collect("xiaohongshu", BAIZHOU_XIAOXIONG.profile_url)

    assert calls == ["public", "authenticated"]
    assert snapshot.profile.nickname == "白昼小熊"
    assert snapshot.acquisition.method == "authenticated_managed_browser"
    assert "已登录受管浏览器" in snapshot.acquisition.limitations[0]


@pytest.mark.parametrize(
    "value",
    (
        "https://127.0.0.1:5556",
        "http://localhost:5556",
        "http://127.0.0.1:5556/path",
        "http://user:pass@127.0.0.1:5556",
        "http://192.168.1.2:5556",
    ),
)
def test_managed_browser_provider_rejects_non_loopback_origins(value: str) -> None:
    with pytest.raises(ValueError, match=r"explicit http://127\.0\.0\.1"):
        BenchmarkAccountGateway(managed_browser_base_url=value)


@pytest.mark.asyncio
async def test_gateway_bounds_cache_cardinality() -> None:
    calls: list[str] = []

    def fetcher(url: str, _timeout_seconds: float, _max_bytes: int) -> bytes:
        calls.append(url)
        return _profile_html(user_id=url.rsplit("/", 1)[-1])

    first_url = "https://www.xiaohongshu.com/user/profile/111111111111111111111111"
    second_url = "https://www.xiaohongshu.com/user/profile/222222222222222222222222"
    gateway = BenchmarkAccountGateway(
        fetcher=fetcher,
        cache_ttl_seconds=60,
        cache_max_entries=1,
    )

    await gateway.collect("xiaohongshu", first_url)
    await gateway.collect("xiaohongshu", second_url)
    third = await gateway.collect("xiaohongshu", first_url)

    assert calls == [first_url, second_url, first_url]
    assert third.acquisition.from_cache is False


def test_profile_resolver_canonicalizes_share_parameters_and_blocks_ssrf_shapes() -> None:
    identity = resolve_xiaohongshu_profile(
        f"{BAIZHOU_XIAOXIONG.profile_url}?xsec_token=ephemeral&xsec_source=pc_search"
    )
    assert identity.profile_url == BAIZHOU_XIAOXIONG.profile_url
    assert identity.external_user_id == "5a8cf39111be10466d285d6b"

    invalid_urls = (
        "http://www.xiaohongshu.com/user/profile/5a8cf39111be10466d285d6b",
        "https://www.xiaohongshu.com.evil.example/user/profile/5a8cf39111be10466d285d6b",
        "https://user:pass@www.xiaohongshu.com/user/profile/5a8cf39111be10466d285d6b",
        "https://www.xiaohongshu.com:443/user/profile/5a8cf39111be10466d285d6b",
        "https://www.xiaohongshu.com/explore/5a8cf39111be10466d285d6b",
    )
    for value in invalid_urls:
        with pytest.raises(BenchmarkAccountError) as error:
            resolve_xiaohongshu_profile(value)
        assert error.value.code == "BENCHMARK_PROFILE_INVALID"


def test_demo_endpoint_returns_snapshot_without_persisting_it() -> None:
    repository = InMemoryControlRepository()
    gateway = BenchmarkAccountGateway(fetcher=lambda _url, _timeout, _limit: _profile_html())
    with TestClient(
        create_app(repository=repository, benchmark_account_gateway=gateway)
    ) as client:
        response = client.get("/v1/benchmark-accounts/demo")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["profile"]["user_id"] == "5a8cf39111be10466d285d6b"
    assert response.json()["analysis"]["sample_size"] == 3
    assert repository._channels == {}

    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_DIR.glob("*.schema.json")
    ]
    schema = next(
        item for item in schemas if item["$id"].endswith("benchmark-account-snapshot.schema.json")
    )
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    registry = Registry().with_resources(
        (item["$id"], Resource.from_contents(item)) for item in schemas
    )
    validator_class(schema, registry=registry).validate(response.json())


def test_preview_endpoint_accepts_another_valid_profile_and_returns_canonical_url() -> None:
    second_user_id = "111111111111111111111111"

    def fetch(url: str, _timeout: float, _limit: int) -> bytes:
        assert url == f"https://www.xiaohongshu.com/user/profile/{second_user_id}"
        return _profile_html(user_id=second_user_id)

    gateway = BenchmarkAccountGateway(fetcher=fetch)
    with TestClient(
        create_app(repository=InMemoryControlRepository(), benchmark_account_gateway=gateway)
    ) as client:
        response = client.post(
            "/v1/benchmark-accounts/preview",
            json={
                "platform": "xiaohongshu",
                "profile_url": (
                    f"https://www.xiaohongshu.com/user/profile/{second_user_id}"
                    "?xsec_token=discard-me"
                ),
            },
        )

    assert response.status_code == 200
    assert response.json()["profile"]["user_id"] == second_user_id
    assert response.json()["profile"]["profile_url"] == (
        f"https://www.xiaohongshu.com/user/profile/{second_user_id}"
    )


def test_report_endpoint_returns_account_and_single_note_reports() -> None:
    gateway = BenchmarkAccountGateway(
        fetcher=lambda _url, _timeout, _limit: _profile_html()
    )
    with TestClient(
        create_app(
            repository=InMemoryControlRepository(),
            benchmark_account_gateway=gateway,
        )
    ) as client:
        response = client.post(
            "/v1/benchmark-accounts/report",
            json={
                "platform": "xiaohongshu",
                "profile_url": BAIZHOU_XIAOXIONG.profile_url,
            },
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.json()
    assert body["account_report"]["nickname"] == "白昼小熊"
    assert len(body["note_reports"]) == body["snapshot"]["analysis"]["sample_size"]
    assert body["note_reports"][0]["evidence_depth"] == "public_metadata_only"

    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_DIR.glob("*.schema.json")
    ]
    schema = next(
        item
        for item in schemas
        if item["$id"].endswith("benchmark-account-report.schema.json")
    )
    registry = Registry().with_resources(
        (item["$id"], Resource.from_contents(item)) for item in schemas
    )
    validator_for(schema)(schema, registry=registry).validate(body)


def test_deep_report_derives_timeline_metrics_without_claiming_source_video() -> None:
    report = benchmark_deep_report_demo()

    assert report.source_kind == "synthetic_demo"
    assert report.evidence_depth == "multimodal_timeline_v1"
    assert report.status == "ready"
    assert len(report.timeline) == 5
    assert report.metrics[0].key == "shot_pace"
    assert {item.category for item in report.findings} >= {
        "hook",
        "visual",
        "narration",
        "rhythm",
    }
    assert "不是目标博主的真实视频结论" in report.limitations[0]


def test_asr_candidates_do_not_assert_voiceover_structure() -> None:
    evidence = BenchmarkMediaEvidence.model_validate({
        "source_kind": "worker_asset_analysis", "source_label": "music-with-vocals",
        "title": "Background vocal sample", "duration_ms": 5000, "has_audio": True,
        "speech_status": "present", "transcript_text": "I like to eat",
        "segments": [{"start_ms": 0, "end_ms": 5000, "description": "Travel montage",
                      "transcript": "I like to eat", "audio_events": ["music"]}],
    })
    report = build_benchmark_deep_note_report(evidence)
    narration = next(item for item in report.findings if item.category == "narration")
    assert narration.confidence == "low"
    assert "未分类" in narration.claim
    assert "唱词" in narration.claim
    assert not any("陈述式推进" in item for item in narration.evidence)
    assert any("不证明口播策略" in item for item in report.limitations)


def test_deep_report_truthfully_degrades_when_asr_and_ocr_are_missing() -> None:
    evidence = BenchmarkMediaEvidence.model_validate(
        {
            "source_kind": "worker_asset_analysis",
            "source_label": "asset-1",
            "title": "无字幕样片",
            "duration_ms": 5000,
            "has_audio": True,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 5000,
                    "description": "固定机位人物中景",
                    "confidence": 0.88,
                }
            ],
        }
    )

    report = build_benchmark_deep_note_report(evidence)

    assert report.status == "partial"
    assert any("ASR" in item for item in report.limitations)
    assert any("OCR" in item for item in report.limitations)
    assert report.timeline[0].evidence_types == ["frame"]


def test_deep_report_distinguishes_verified_no_speech_from_unconfigured_asr() -> None:
    evidence = BenchmarkMediaEvidence.model_validate(
        {
            "source_kind": "worker_asset_analysis",
            "source_label": "asset-music-only",
            "title": "无口播转场片",
            "duration_ms": 6000,
            "has_audio": True,
            "speech_status": "absent",
            "speech_detection_method": "faster-whisper small + VAD",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 6000,
                    "description": "人物通过色块完成匹配转场",
                    "ocr_text": ["想看薄荷绿色 可以吗"],
                    "audio_events": ["连续配乐"],
                }
            ],
        }
    )

    report = build_benchmark_deep_note_report(evidence)

    assert report.status == "ready"
    assert report.metrics[2].value == "未检测到口播"
    assert any(item.category == "narration" for item in report.findings)
    assert not any("ASR 文本" in item for item in report.limitations)


def test_worker_asset_analysis_can_be_adapted_into_deep_report_evidence() -> None:
    evidence = timeline_from_worker_analysis(
        title="Worker 视频",
        source_label="asset-analysis-v3:asset-1",
        analysis={
            "technical": {"duration_ms": 4000, "width": 1080, "height": 1920, "has_audio": True},
            "temporal": {"silences": [{"start_ms": 1800, "end_ms": 2100}]},
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 1800,
                    "description": "人物近景开场",
                    "shot_type": "近景",
                    "transcript": "你也不知道怎么拍吗?",
                    "ocr_text": ["三秒学会"],
                    "audio_events": ["音乐进入"],
                    "confidence": 0.91,
                },
                {
                    "start_ms": 1800,
                    "end_ms": 4000,
                    "description": "结果画面揭晓",
                    "shot_type": "全景",
                    "transcript": "试试这个方法。",
                    "confidence": 0.89,
                },
            ],
        },
    )

    report = build_benchmark_deep_note_report(evidence)

    assert evidence.source_kind == "worker_asset_analysis"
    assert report.status == "ready"
    assert report.timeline[0].evidence_types == ["frame", "ocr", "asr", "audio"]
    assert report.metrics[2].value.endswith("字/分钟")


def test_deep_report_endpoints_return_no_store_and_validate_timeline() -> None:
    with TestClient(create_app(repository=InMemoryControlRepository())) as client:
        demo = client.get("/v1/benchmark-notes/deep-report/demo")
        invalid = client.post(
            "/v1/benchmark-notes/deep-report",
            json={
                "source_kind": "worker_asset_analysis",
                "source_label": "asset-invalid",
                "title": "重叠时间轴",
                "duration_ms": 5000,
                "has_audio": False,
                "segments": [
                    {"start_ms": 0, "end_ms": 3000, "description": "第一镜"},
                    {"start_ms": 2000, "end_ms": 5000, "description": "第二镜"},
                ],
            },
        )

    assert demo.status_code == 200
    assert demo.headers["Cache-Control"] == "private, no-store"
    assert demo.json()["source_kind"] == "synthetic_demo"
    assert invalid.status_code == 422

    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_DIR.glob("*.schema.json")
    ]
    schema = next(
        item
        for item in schemas
        if item["$id"].endswith("benchmark-note-deep-report.schema.json")
    )
    registry = Registry().with_resources(
        (item["$id"], Resource.from_contents(item)) for item in schemas
    )
    validator_for(schema)(schema, registry=registry).validate(demo.json())


def test_latest_deep_report_is_workspace_scoped_and_never_falls_back_to_demo() -> None:
    with TestClient(create_app(repository=InMemoryControlRepository())) as client:
        missing = client.get("/v1/benchmark-notes/deep-report/latest")
        synthetic = client.post(
            "/v1/benchmark-notes/deep-report",
            json={
                "source_kind": "synthetic_demo",
                "source_label": "fake",
                "title": "不能覆盖真实报告",
                "duration_ms": 1000,
                "has_audio": False,
                "segments": [
                    {"start_ms": 0, "end_ms": 1000, "description": "fake"}
                ],
            },
        )
        created = client.post(
            "/v1/benchmark-notes/deep-report",
            json={
                "source_kind": "worker_asset_analysis",
                "source_label": "real-asset",
                "title": "真实媒体",
                "duration_ms": 1000,
                "has_audio": False,
                "speech_status": "absent",
                "segments": [
                    {
                        "start_ms": 0,
                        "end_ms": 1000,
                        "description": "真实代表帧",
                        "ocr_text": ["真实文字"],
                    }
                ],
            },
        )
        latest = client.get("/v1/benchmark-notes/deep-report/latest")

    assert missing.status_code == 404
    assert missing.json()["code"] == "BENCHMARK_DEEP_REPORT_NOT_FOUND"
    assert synthetic.status_code == 422
    assert synthetic.json()["code"] == "BENCHMARK_MEDIA_SOURCE_INVALID"
    assert created.status_code == 200
    assert latest.status_code == 200
    assert latest.headers["Cache-Control"] == "private, no-store"
    assert latest.json()["source_kind"] == "worker_asset_analysis"
    assert latest.json()["title"] == "真实媒体"


def test_preview_endpoint_rejects_unsupported_platform_and_unsafe_url() -> None:
    gateway = BenchmarkAccountGateway(fetcher=lambda _url, _timeout, _limit: _profile_html())
    with TestClient(
        create_app(repository=InMemoryControlRepository(), benchmark_account_gateway=gateway)
    ) as client:
        unsupported = client.post(
            "/v1/benchmark-accounts/preview",
            json={"platform": "douyin", "profile_url": "https://www.douyin.com/user/test"},
        )
        unsafe = client.post(
            "/v1/benchmark-accounts/preview",
            json={
                "platform": "xiaohongshu",
                "profile_url": "https://127.0.0.1/user/profile/111111111111111111111111",
            },
        )

    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "BENCHMARK_PLATFORM_UNSUPPORTED"
    assert unsafe.status_code == 422
    assert unsafe.json()["code"] == "BENCHMARK_PROFILE_INVALID"


class _FixtureNoteProvider:
    platform = "xiaohongshu"

    def collect(self, profile_url: str, note_id: str) -> BenchmarkNoteSourceEvidence:
        assert profile_url == BAIZHOU_XIAOXIONG.profile_url
        return BenchmarkNoteSourceEvidence(
            profile_user_id="5a8cf39111be10466d285d6b",
            note_id=note_id,
            canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            captured_at=datetime(2026, 9, 5, tzinfo=UTC),
            title="真实详情证据",
            description="只返回经过脱敏的页面正文。",
            likes=metric_from_display("10万+"),
            collects=metric_from_display("1.2万"),
            comments=metric_from_display("4191"),
            media=BenchmarkMediaProbe(
                kind="video",
                video_available=True,
                image_count=0,
                duration_ms=17_850,
                width=3840,
                height=2160,
                trusted_media_origin=True,
            ),
            limitations=["不返回短期令牌或签名媒体 URL。"],
        )


def test_authenticated_note_source_endpoint_returns_sanitized_real_provider_contract() -> None:
    gateway = BenchmarkNoteSourceGateway(_FixtureNoteProvider())
    with TestClient(
        create_app(
            repository=InMemoryControlRepository(),
            benchmark_note_source_gateway=gateway,
        )
    ) as client:
        response = client.post(
            "/v1/benchmark-notes/source-evidence",
            json={
                "platform": "xiaohongshu",
                "profile_url": BAIZHOU_XIAOXIONG.profile_url,
                "note_id": "6a94178b0000000025026802",
            },
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.json()
    assert body["title"] == "真实详情证据"
    assert body["media"]["duration_ms"] == 17_850
    serialized = json.dumps(body)
    assert "xsec" not in serialized
    assert "xhscdn" not in serialized

    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_DIR.glob("*.schema.json")
    ]
    schema = next(
        item
        for item in schemas
        if item["$id"].endswith("benchmark-note-source-evidence.schema.json")
    )
    registry = Registry().with_resources(
        (item["$id"], Resource.from_contents(item)) for item in schemas
    )
    validator_for(schema)(schema, registry=registry).validate(body)


def test_authenticated_note_provider_validates_note_before_contacting_browser() -> None:
    provider = XiaohongshuAuthenticatedNoteProvider(
        managed_browser_base_url="http://127.0.0.1:5556"
    )
    with pytest.raises(BenchmarkAccountError) as error:
        provider.collect(BAIZHOU_XIAOXIONG.profile_url, "not-a-note")

    assert error.value.code == "BENCHMARK_NOTE_INVALID"


def test_demo_endpoint_exposes_provider_failure_without_fake_data() -> None:
    def fail(_url: str, _timeout: float, _limit: int) -> bytes:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_UNAVAILABLE", "source offline", retryable=True
        )

    gateway = BenchmarkAccountGateway(fetcher=fail)
    with TestClient(
        create_app(repository=InMemoryControlRepository(), benchmark_account_gateway=gateway)
    ) as client:
        response = client.get("/v1/benchmark-accounts/demo")

    assert response.status_code == 503
    assert response.json()["code"] == "BENCHMARK_SOURCE_UNAVAILABLE"
    assert response.json()["details"] == {"retryable": True}
