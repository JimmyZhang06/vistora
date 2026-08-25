import json

import pytest

from framefactory_api.remote_assets import (
    RemoteAssetError,
    _query_relevance,
    _subtitle_languages,
    _wikimedia_derivative,
    _wikimedia_original_filename,
    _wikimedia_public_domain_evidence,
    _wikimedia_search_query,
    _ytdlp_download_error,
    classify_source,
    parse_rednote_response,
    parse_search_results,
)


def _commons_rights_payload(*, license_name: str = "Public domain") -> dict:
    filename = "Apollo_11_Landing_first_steps.ogv"
    return {
        "query": {
            "pages": {
                "123": {
                    "title": f"File:{filename}",
                    "imageinfo": [
                        {
                            "url": (
                                "https://upload.wikimedia.org/wikipedia/commons/"
                                f"a/a1/{filename}"
                            ),
                            "descriptionurl": (
                                "https://commons.wikimedia.org/wiki/"
                                f"File:{filename}"
                            ),
                            "extmetadata": {
                                "LicenseShortName": {"value": license_name},
                                "Copyrighted": {
                                    "value": (
                                        "False" if license_name == "Public domain" else "True"
                                    )
                                },
                                "Artist": {"value": "NASA"},
                            },
                        }
                    ],
                }
            }
        }
    }


def test_wikimedia_transcode_maps_back_to_original_file_for_rights_lookup() -> None:
    source = (
        "https://upload.wikimedia.org/wikipedia/commons/transcoded/a/a1/"
        "Apollo_11_Landing_first_steps.ogv/"
        "Apollo_11_Landing_first_steps.ogv.720p.webm"
    )

    assert _wikimedia_original_filename(source) == "Apollo_11_Landing_first_steps.ogv"


def test_commons_public_domain_metadata_becomes_traceable_verified_evidence() -> None:
    evidence = _wikimedia_public_domain_evidence(
        _commons_rights_payload(),
        expected_filename="Apollo_11_Landing_first_steps.ogv",
        verified_at="2026-08-22T12:00:00Z",
    )

    assert evidence == {
        "title": "Apollo_11_Landing_first_steps.ogv",
        "locator": (
            "https://commons.wikimedia.org/wiki/"
            "File:Apollo_11_Landing_first_steps.ogv"
        ),
        "license": "Public domain",
        "attribution": "NASA",
        "verified_at": "2026-08-22T12:00:00Z",
    }


def test_commons_licensed_or_mismatched_metadata_is_not_marked_public_domain() -> None:
    with pytest.raises(RemoteAssetError) as licensed:
        _wikimedia_public_domain_evidence(
            _commons_rights_payload(license_name="CC BY 4.0"),
            expected_filename="Apollo_11_Landing_first_steps.ogv",
            verified_at="2026-08-22T12:00:00Z",
        )
    with pytest.raises(RemoteAssetError) as mismatch:
        _wikimedia_public_domain_evidence(
            _commons_rights_payload(),
            expected_filename="Different_file.ogv",
            verified_at="2026-08-22T12:00:00Z",
        )
    with pytest.raises(RemoteAssetError) as explicitly_not_public_domain:
        _wikimedia_public_domain_evidence(
            _commons_rights_payload(license_name="Not public domain"),
            expected_filename="Apollo_11_Landing_first_steps.ogv",
            verified_at="2026-08-22T12:00:00Z",
        )

    assert licensed.value.code == "REMOTE_RIGHTS_UNVERIFIED"
    assert mismatch.value.code == "REMOTE_RIGHTS_UNVERIFIED"
    assert explicitly_not_public_domain.value.code == "REMOTE_RIGHTS_UNVERIFIED"


def test_wikimedia_prefers_smaller_official_transcode_over_original() -> None:
    original = "https://upload.wikimedia.org/wikipedia/commons/a/a1/source.ogv"
    transcode = (
        "https://upload.wikimedia.org/wikipedia/commons/transcoded/a/a1/"
        "source.ogv/source.ogv.360p.mpeg4.mov"
    )
    source, media_type, size = _wikimedia_derivative(
        {
            "url": original,
            "mime": "application/ogg",
            "size": 26_000_000,
            "derivatives": [
                {
                    "src": original,
                    "type": 'video/ogg; codecs="theora"',
                    "width": 640,
                    "bandwidth": 4_800_000,
                },
                {
                    "src": transcode,
                    "type": "video/quicktime",
                    "width": 640,
                    "bandwidth": 1_000_000,
                },
            ],
        },
        43.3,
    )

    assert source == transcode
    assert media_type == "video/quicktime"
    assert 5_000_000 < size < 6_000_000


def test_requested_subtitle_languages_are_normalized_for_asset_provenance() -> None:
    assert _subtitle_languages(
        {
            "requested_subtitles": {
                "zh-Hans": {"filepath": "video.zh-Hans.vtt"},
                "en": {"filepath": "video.en.vtt"},
                "ignored": None,
            }
        }
    ) == ("en", "zh-Hans")


def test_youtube_bot_challenge_is_a_non_retryable_configuration_error() -> None:
    error = _ytdlp_download_error(
        b"ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser"
    )

    assert error.code == "REMOTE_AUTH_REQUIRED"
    assert error.retryable is False
    assert "cookies" not in str(error).casefold()


def test_unknown_downloader_failure_remains_retryable() -> None:
    error = _ytdlp_download_error(b"temporary upstream connection reset")

    assert error.code == "REMOTE_DOWNLOAD_FAILED"
    assert error.retryable is True


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://youtu.be/example", "youtube"),
        ("https://www.youtube.com/watch?v=example", "youtube"),
        ("https://www.bilibili.com/video/BV1example", "bilibili"),
        ("https://b23.tv/example", "bilibili"),
        ("https://www.xiaohongshu.com/explore/example", "rednote"),
        ("https://xhslink.com/n/example", "rednote"),
    ],
)
def test_source_classification_is_host_allowlisted(url: str, platform: str) -> None:
    assert classify_source(url) == platform


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=example",
        "https://youtube.com.evil.invalid/watch?v=example",
        "https://user:password@www.bilibili.com/video/example",
        "https://example.com/video.mp4",
    ],
)
def test_source_classification_rejects_unsafe_or_unsupported_hosts(url: str) -> None:
    with pytest.raises(RemoteAssetError):
        classify_source(url)


def test_rednote_nested_json_response_is_parsed_without_trusting_html() -> None:
    result = {"code": 0, "videoList": ["https://cdn.example/video.mp4"]}
    body = json.dumps({"data": json.dumps(json.dumps(result))}).encode()
    assert parse_rednote_response(body) == result


def test_rednote_invalid_response_fails_closed() -> None:
    with pytest.raises(RemoteAssetError, match="unsupported response"):
        parse_rednote_response(b'{"data":"not-json"}')


def test_search_results_are_normalized_to_public_platform_urls() -> None:
    output = (
        b'{"id":"abc123","title":"First","uploader":"Creator"}\n'
        b'{"id":"abc123","title":"Duplicate"}\n'
        b'{"id":"def456","title":"Second","duration":42}\n'
    )

    results = parse_search_results("youtube", output, limit=3)

    assert [item.source_url for item in results] == [
        "https://www.youtube.com/watch?v=abc123",
        "https://www.youtube.com/watch?v=def456",
    ]
    assert results[0].author == "Creator"
    assert results[1].duration_seconds == 42


def test_bilibili_search_http_result_is_upgraded_before_safety_validation() -> None:
    output = b'{"id":"829639473","url":"http://www.bilibili.com/video/av829639473"}\n'

    results = parse_search_results("bilibili", output, limit=1)

    assert results[0].source_url == "https://www.bilibili.com/video/av829639473"


def test_relevance_gate_rejects_same_sport_but_wrong_subject() -> None:
    assert _query_relevance(
        "Xu Xin table tennis match", "Table Tennis Training Simulator"
    ) < 0.5
    assert _query_relevance(
        "Xu Xin table tennis match", "Ma Long vs Xu Xin table tennis final"
    ) >= 0.5
    assert _query_relevance("许昕 乒乓球 比赛", "许昕经典乒乓球比赛集锦") >= 0.5


def test_relevance_gate_rejects_wrong_event_or_year() -> None:
    assert (
        _query_relevance(
            "张继科 2011 巴黎世界杯 男单决赛",
            "张继科vs王皓 2011世乒赛男单决赛",
        )
        == 0.0
    )
    assert (
        _query_relevance(
            "张继科 2012 伦敦奥运会 男单决赛",
            "张继科 2016 里约奥运会男单决赛",
        )
        == 0.0
    )


@pytest.mark.parametrize(
    "wrong_title",
    (
        "Apollo 17 EVA NASA",
        "NASA Apollo 8 Manned Space Flight Report 1968",
        "The Apollo 4 Mission 1967 NASA",
    ),
)
def test_relevance_gate_rejects_conflicting_alpha_numeric_entity(
    wrong_title: str,
) -> None:
    assert _query_relevance("NASA Apollo 11 moon landing", wrong_title) == 0.0


def test_relevance_gate_accepts_exact_identifier_and_preserves_unnumbered_rules() -> None:
    assert (
        _query_relevance(
            "NASA Apollo 11 moon landing",
            "Apollo 11 Landing - first steps on the moon NASA",
        )
        >= 0.5
    )
    assert _query_relevance("Apollo moon landing", "Apollo moon landing archive") == 1.0
    assert _query_relevance("Apollo 11 documentary", "Apollo documentary") >= 0.5


def test_wikimedia_query_projection_preserves_numeric_identifiers_with_cjk_suffix() -> None:
    assert _wikimedia_search_query("Apollo 11 月面平坦, 远处可见登月舱。") == "Apollo 11"
    assert (
        _wikimedia_search_query("NASA Apollo 11 登月舱着陆月面, 无火焰或烟雾。")
        == "NASA Apollo 11"
    )
    numeric_only = "1969 1970 月面历史影像"
    assert _wikimedia_search_query(numeric_only) == numeric_only
