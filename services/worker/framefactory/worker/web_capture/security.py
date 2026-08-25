"""Fail-closed URL validation for the untrusted browser-capture boundary."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeWebUrl(ValueError):
    """A URL cannot be proven to target only public HTTPS infrastructure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Resolver = Callable[[str, int], Iterable[str]]


@dataclass(frozen=True, slots=True)
class ValidatedWebUrl:
    navigation_url: str
    redacted_url: str
    hostname: str
    port: int
    resolved_ips: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicHttpsPolicy:
    allow_non_standard_ports: bool = False
    maximum_url_characters: int = 2_048

    def __post_init__(self) -> None:
        if not 256 <= self.maximum_url_characters <= 16_384:
            raise ValueError("maximum URL characters must be between 256 and 16384")


class PublicHttpsUrlValidator:
    """Resolve every destination and reject when any answer is not global.

    This is an application-layer guard only.  DNS can change after validation,
    so production capture additionally requires an independently enforced
    egress proxy/firewall policy at the actual connection boundary.
    """

    def __init__(
        self,
        policy: PublicHttpsPolicy | None = None,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self.policy = policy or PublicHttpsPolicy()
        self._resolver = resolver or _resolve_addresses

    def validate(self, raw_url: str) -> ValidatedWebUrl:
        if not isinstance(raw_url, str):
            raise UnsafeWebUrl("invalid_url", "capture URL must be a string")
        value = raw_url.strip()
        if not value or len(value) > self.policy.maximum_url_characters:
            raise UnsafeWebUrl("invalid_url", "capture URL length is outside policy")
        if value != raw_url or any(ord(character) < 0x20 for character in value):
            raise UnsafeWebUrl("invalid_url", "capture URL contains whitespace or controls")
        if "\\" in value:
            raise UnsafeWebUrl("invalid_url", "capture URL contains an ambiguous backslash")
        try:
            parsed = urlsplit(value)
            port = parsed.port or 443
        except ValueError as exc:
            raise UnsafeWebUrl("invalid_url", "capture URL cannot be parsed safely") from exc
        if parsed.scheme.lower() != "https":
            raise UnsafeWebUrl("https_required", "capture URL must use HTTPS")
        if not parsed.hostname:
            raise UnsafeWebUrl("host_required", "capture URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeWebUrl("userinfo_forbidden", "capture URL must not contain userinfo")
        if not self.policy.allow_non_standard_ports and port != 443:
            raise UnsafeWebUrl("port_forbidden", "capture URL must use the standard HTTPS port")

        hostname = _canonical_hostname(parsed.hostname)
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise UnsafeWebUrl("non_public_host", "capture URL resolves to a local name")
        addresses = tuple(sorted(set(self._resolver(hostname, port))))
        if not addresses:
            raise UnsafeWebUrl("dns_resolution_failed", "capture URL host has no address")
        canonical_ips: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value.split("%", 1)[0])
            except ValueError as exc:
                raise UnsafeWebUrl(
                    "dns_resolution_failed", "capture URL returned an invalid address"
                ) from exc
            if not address.is_global:
                raise UnsafeWebUrl(
                    "non_public_address",
                    "capture URL resolved to a non-global address",
                )
            canonical_ips.append(address.compressed)

        normalized = _normalized_url(parsed, hostname, port)
        return ValidatedWebUrl(
            navigation_url=normalized,
            redacted_url=redact_url(normalized),
            hostname=hostname,
            port=port,
            resolved_ips=tuple(sorted(set(canonical_ips))),
        )


def redact_url(raw_url: str) -> str:
    """Remove credentials, fragments and query values before logs/artifacts."""

    try:
        parsed = urlsplit(raw_url)
        hostname = _canonical_hostname(parsed.hostname or "invalid")
        port = parsed.port
    except (UnicodeError, ValueError):
        return "https://invalid/"
    netloc = _netloc(hostname, port)
    query = "redacted=1" if parsed.query else ""
    return urlunsplit(("https", netloc, parsed.path or "/", query, ""))


def _canonical_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".").lower()
    if not candidate or len(candidate) > 253:
        raise UnsafeWebUrl("invalid_host", "capture URL host length is invalid")
    try:
        address = ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        try:
            candidate = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise UnsafeWebUrl("invalid_host", "capture URL host is invalid") from exc
        labels = candidate.split(".")
        if any(not label or len(label) > 63 for label in labels):
            raise UnsafeWebUrl("invalid_host", "capture URL host labels are invalid")
        return candidate
    return address.compressed


def _normalized_url(parsed: SplitResult, hostname: str, port: int) -> str:
    netloc = _netloc(hostname, None if port == 443 else port)
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _netloc(hostname: str, port: int | None) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host}:{port}" if port is not None else host


def _resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise UnsafeWebUrl("dns_resolution_failed", "capture URL DNS lookup failed") from exc
    return tuple(str(answer[4][0]) for answer in answers)
