from __future__ import annotations

import struct
from pathlib import Path
from typing import Self

import pytest
from framefactory.worker.adapters.asset_analysis import ClamdScanner
from framefactory.worker.asset_pipeline import AssetPipelineError


class FakeClamdSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent: list[bytes] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response


def scanner(response: bytes, connection: FakeClamdSocket) -> ClamdScanner:
    def connect(address: tuple[str, int], *, timeout: float) -> FakeClamdSocket:
        assert address == ("clamav", 3310)
        assert timeout == 12
        return connection

    connection.response = response
    return ClamdScanner("clamav", timeout_seconds=12, connector=connect)


def test_clamd_scanner_streams_bounded_bytes_and_accepts_clean_file(tmp_path: Path) -> None:
    source = tmp_path / "clean.bin"
    source.write_bytes(b"safe-bytes")
    connection = FakeClamdSocket(b"")

    result = scanner(b"stream: OK\0", connection).scan(source)

    assert result == {"status": "clean", "engine": "clamd"}
    assert connection.sent[0] == b"zINSTREAM\0"
    assert connection.sent[1] == struct.pack("!I", len(b"safe-bytes"))
    assert connection.sent[2] == b"safe-bytes"
    assert connection.sent[-1] == struct.pack("!I", 0)


def test_clamd_scanner_fails_closed_for_detected_malware(tmp_path: Path) -> None:
    source = tmp_path / "detected.bin"
    source.write_bytes(b"probe")
    connection = FakeClamdSocket(b"")

    with pytest.raises(AssetPipelineError) as raised:
        scanner(b"stream: signature FOUND\0", connection).scan(source)

    assert raised.value.code == "malware_detected"
    assert raised.value.retryable is False


def test_clamd_scanner_rejects_oversized_file_before_connecting(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (1024 * 1024 + 1))

    def unexpected_connect(*_args: object, **_kwargs: object) -> FakeClamdSocket:
        raise AssertionError("oversized files must not reach ClamAV")

    scanner_instance = ClamdScanner(
        "clamav",
        maximum_stream_bytes=1024 * 1024,
        connector=unexpected_connect,
    )

    with pytest.raises(AssetPipelineError) as raised:
        scanner_instance.scan(source)

    assert raised.value.code == "malware_scan_too_large"
    assert raised.value.retryable is False


def test_clamd_scanner_rejects_unterminated_response(tmp_path: Path) -> None:
    source = tmp_path / "unknown.bin"
    source.write_bytes(b"probe")
    connection = FakeClamdSocket(b"")

    with pytest.raises(AssetPipelineError) as raised:
        scanner(b"stream: OK", connection).scan(source)

    assert raised.value.code == "malware_scan_failed"
    assert raised.value.retryable is True
