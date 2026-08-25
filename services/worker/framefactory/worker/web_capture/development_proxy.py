"""Development-only CONNECT proxy with connection-time public-IP enforcement.

The production deployment still requires an independently managed egress proxy
or firewall.  This small proxy exists so the local launcher can exercise the
same isolated browser-capture queue without weakening the production profile.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from .security import PublicHttpsPolicy, PublicHttpsUrlValidator, UnsafeWebUrl

logger = logging.getLogger("framefactory.browser_egress_proxy")

_HEADER_LIMIT = 16_384
_HEADER_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_SECONDS = 10.0
_IDLE_TIMEOUT_SECONDS = 60.0


def validate_connect_authority(
    authority: str,
    *,
    validator: PublicHttpsUrlValidator | None = None,
) -> tuple[str, int, tuple[str, ...]]:
    """Return a canonical public destination for one HTTPS CONNECT request."""

    value = authority.strip()
    if not value or any(ord(character) < 0x21 for character in value):
        raise UnsafeWebUrl("invalid_proxy_target", "proxy target is malformed")
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1 or value[closing + 1 :] != ":443":
            raise UnsafeWebUrl("invalid_proxy_target", "proxy target must use port 443")
        hostname = value[1:closing]
    else:
        hostname, separator, port = value.rpartition(":")
        if not separator or port != "443" or not hostname:
            raise UnsafeWebUrl("invalid_proxy_target", "proxy target must use port 443")
    checked = (validator or PublicHttpsUrlValidator(PublicHttpsPolicy())).validate(
        f"https://{_url_host(hostname)}/"
    )
    return checked.hostname, checked.port, checked.resolved_ips


async def _open_public_upstream(ips: Sequence[str], port: int):
    last_error: OSError | None = None
    for address in ips:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(address, port),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            last_error = exc
    raise OSError("no validated public proxy destination was reachable") from last_error


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await asyncio.wait_for(
            reader.read(65_536), timeout=_IDLE_TIMEOUT_SECONDS
        ):
            writer.write(chunk)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        header = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=_HEADER_TIMEOUT_SECONDS
        )
        if len(header) > _HEADER_LIMIT:
            raise ValueError("proxy header is too large")
        request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
        method, authority, version = request_line.split(" ", 2)
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ValueError("only HTTPS CONNECT is supported")
        hostname, port, ips = validate_connect_authority(authority)
        upstream_reader, upstream_writer = await _open_public_upstream(ips, port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            _relay(reader, upstream_writer),
            _relay(upstream_reader, writer),
        )
        logger.debug("completed CONNECT tunnel host=%s", hostname)
    except (ValueError, UnicodeError, UnsafeWebUrl):
        writer.write(
            b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        )
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
        writer.write(
            b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        )
        await writer.drain()
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
        writer.close()
        await writer.wait_closed()


async def serve(host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("development proxy must bind to a loopback address")
    server = await asyncio.start_server(
        _handle_client,
        host,
        port,
        limit=_HEADER_LIMIT,
    )
    sockets = server.sockets or ()
    addresses = ", ".join(str(sock.getsockname()) for sock in sockets)
    logger.info("development browser egress proxy ready on %s", addresses)
    async with server:
        await server.serve_forever()


def _url_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname else hostname


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=58888)
    args = parser.parse_args()
    if not 1_024 <= args.port <= 65_535:
        parser.error("--port must be between 1024 and 65535")
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
