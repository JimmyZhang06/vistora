import json

import pytest

from framefactory_api.remote_assets import (
    RemoteAssetError,
    _query_relevance,
    _subtitle_languages,
    _wikimedia_derivative,
    _ytdlp_download_error,
    classify_source,
    parse_rednote_response,
    parse_search_results,
)


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
