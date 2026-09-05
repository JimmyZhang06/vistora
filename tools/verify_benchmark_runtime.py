"""Read-only local API compatibility check; never starts login or paid work."""

from __future__ import annotations

import argparse
import json
import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMAS = Path(__file__).resolve().parents[1] / "packages/contracts/schemas/v1"
AUTH = "/v1/benchmark-auth/xiaohongshu/status"
HISTORY = "/v1/benchmark-history"
REQUIRED_ROUTES = {
    AUTH: "get",
    "/v1/benchmark-auth/xiaohongshu/qrcode": "post",
    HISTORY: "get",
    "/v1/benchmark-history/{record_id}": "get",
    "/v1/benchmark-accounts/report": "post",
    "/v1/benchmark-analysis/jobs": "post",
}


class RuntimeCheckError(ValueError):
    """Only fixed, non-sensitive diagnostic codes cross the CLI boundary."""


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeCheckError("BENCHMARK_RUNTIME_REDIRECT_REJECTED")


def local_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            and parsed.port is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeCheckError("BENCHMARK_RUNTIME_LOCAL_ORIGIN_REQUIRED")
    return value.rstrip("/")


def read_json(origin: str, path: str, web_origin: str) -> tuple[dict, object]:
    request = Request(origin + path, headers={"Accept": "application/json", "Origin": web_origin})
    opener = build_opener(ProxyHandler({}), RejectRedirects())
    try:
        with opener.open(request, timeout=20) as response:
            limit = 4 * 1024 * 1024
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise RuntimeCheckError("BENCHMARK_RUNTIME_RESPONSE_TOO_LARGE")
            if response.headers.get_content_type() != "application/json":
                raise RuntimeCheckError("BENCHMARK_RUNTIME_INVALID_RESPONSE")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise RuntimeCheckError("BENCHMARK_RUNTIME_INVALID_RESPONSE")
            return result, response.headers
    except HTTPError as exc:
        code = "BENCHMARK_API_VERSION_MISMATCH" if exc.code in {404, 405} else f"BENCHMARK_RUNTIME_HTTP_{exc.code}"
        raise RuntimeCheckError(code) from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeCheckError("BENCHMARK_RUNTIME_UNREACHABLE") from None
    except (UnicodeError, json.JSONDecodeError, HTTPException):
        raise RuntimeCheckError("BENCHMARK_RUNTIME_INVALID_RESPONSE") from None


def verify_preflight(origin: str, path: str, web_origin: str, method: str) -> None:
    allowed_headers = {"content-type"}
    if method == "POST":
        allowed_headers.add("idempotency-key")
    request = Request(origin + path, method="OPTIONS", headers={
        "Origin": web_origin, "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": ",".join(sorted(allowed_headers)),
    })
    try:
        with build_opener(ProxyHandler({}), RejectRedirects()).open(request, timeout=5) as response:
            headers = response.headers
            methods = {value.strip().upper() for value in headers.get("Access-Control-Allow-Methods", "").split(",")}
            names = {value.strip().lower() for value in headers.get("Access-Control-Allow-Headers", "").split(",")}
            if (
                headers.get("Access-Control-Allow-Origin") != web_origin
                or method not in methods or not allowed_headers <= names
            ):
                raise RuntimeCheckError("BENCHMARK_RUNTIME_PREFLIGHT_REJECTED")
    except HTTPError:
        raise RuntimeCheckError("BENCHMARK_RUNTIME_PREFLIGHT_REJECTED") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeCheckError("BENCHMARK_RUNTIME_UNREACHABLE") from None
    except HTTPException:
        raise RuntimeCheckError("BENCHMARK_RUNTIME_INVALID_RESPONSE") from None


def validator(filename: str) -> Draft202012Validator:
    documents = {
        name: json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
        for name in ("common.schema.json", "benchmark-auth-state.schema.json", "benchmark-history-page.schema.json")
    }
    registry = Registry().with_resources(
        (value["$id"], Resource.from_contents(value)) for value in documents.values()
    )
    return Draft202012Validator(documents[filename], registry=registry, format_checker=FormatChecker())


def verify(api_url: str, web_origin: str) -> dict:
    api_url, web_origin = local_origin(api_url), local_origin(web_origin)
    spec, _ = read_json(api_url, "/openapi.json", web_origin)
    paths = spec.get("paths", {})
    if not isinstance(paths, dict) or any(
        not isinstance(paths.get(path), dict) or method not in paths[path]
        for path, method in REQUIRED_ROUTES.items()
    ):
        raise RuntimeCheckError("BENCHMARK_API_VERSION_MISMATCH")
    for path, method in ((AUTH, "GET"), ("/v1/benchmark-auth/xiaohongshu/qrcode", "POST"), (HISTORY, "GET")):
        verify_preflight(api_url, path, web_origin, method)
    # GET status preserves an existing login and never requests a fresh QR.
    status, headers = read_json(api_url, AUTH, web_origin)
    history, history_headers = read_json(api_url, HISTORY + "?limit=1", web_origin)
    for body, metadata, schema in (
        (status, headers, "benchmark-auth-state.schema.json"),
        (history, history_headers, "benchmark-history-page.schema.json"),
    ):
        if not validator(schema).is_valid(body):
            raise RuntimeCheckError("BENCHMARK_RUNTIME_CONTRACT_MISMATCH")
        if "no-store" not in metadata.get("Cache-Control", "").lower():
            raise RuntimeCheckError("BENCHMARK_RUNTIME_CACHE_UNSAFE")
        if metadata.get("Access-Control-Allow-Origin") != web_origin:
            raise RuntimeCheckError("BENCHMARK_RUNTIME_ORIGIN_MISMATCH")
    if status["state"] == "not_configured":
        raise RuntimeCheckError("BENCHMARK_AUTH_NOT_CONFIGURED")
    # Never print the response, QR, report titles, session identifiers or errors from a provider.
    return {
        "compatible": True,
        "connection_state": status["state"],
        "history": "passed",
        "live_login_verified": status["state"] == "authorized",
        "scan_flow_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--web-origin", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.api_url, args.web_origin)
    except RuntimeCheckError as exc:
        print(json.dumps({"compatible": False, "code": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
