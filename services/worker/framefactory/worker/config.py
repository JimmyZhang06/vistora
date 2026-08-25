"""Strict environment configuration for the production worker."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

_QUEUE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSettings:
    base_url: str
    api_key: str = field(repr=False)
    research_model: str = ""
    writing_model: str = ""
    quality_model: str = ""
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("FRAMEFACTORY_OPENAI_BASE_URL must be an HTTP(S) URL with a host")
        if not self.api_key:
            raise ValueError("FRAMEFACTORY_OPENAI_API_KEY must not be empty")
        if not all((self.research_model, self.writing_model, self.quality_model)):
            raise ValueError("all FRAMEFACTORY_OPENAI_*_MODEL values are required")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("FRAMEFACTORY_OPENAI_TIMEOUT_SECONDS must be positive and finite")


@dataclass(frozen=True, slots=True)
class ResearchSearchSettings:
    """Optional vendor-neutral HTTPS JSON search endpoint."""

    url: str
    bearer_token: str = field(repr=False)
    timeout_seconds: float = 20.0
    maximum_response_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "FRAMEFACTORY_RESEARCH_SEARCH_URL must be a credential-free HTTPS URL"
            )
        if not self.bearer_token:
            raise ValueError("FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN must not be empty")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError(
                "FRAMEFACTORY_RESEARCH_SEARCH_TIMEOUT_SECONDS must be positive and finite"
            )
        if not 1_024 <= self.maximum_response_bytes <= 4_194_304:
            raise ValueError(
                "FRAMEFACTORY_RESEARCH_SEARCH_MAX_RESPONSE_BYTES must be between 1024 and 4194304"
            )


@dataclass(frozen=True, slots=True)
class ObjectStorageSettings:
    bucket: str
    region: str = "us-east-1"
    key_prefix: str = ""
    endpoint_url: str | None = None
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.bucket or len(self.bucket) > 255:
            raise ValueError("FRAMEFACTORY_S3_BUCKET must not be empty")
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError("S3 access key id and secret must be configured together")
        if self.key_prefix and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.key_prefix):
            raise ValueError(
                "FRAMEFACTORY_S3_KEY_PREFIX must be one lowercase safe path segment"
            )
        if self.endpoint_url:
            parsed = urlsplit(self.endpoint_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("FRAMEFACTORY_S3_ENDPOINT_URL must be an HTTP(S) URL with a host")


@dataclass(frozen=True, slots=True)
class LegacyMediaSettings:
    """Explicit migration bridge for the old local TTS, catalog and FFmpeg stack."""

    asset_root: Path | None = None
    catalog_paths: tuple[Path, ...] = ()
    tts_voice: str = "zh-CN-YunjianNeural"
    tts_rate: str = "+25%"
    ffmpeg_command: str = "ffmpeg"
    ffprobe_command: str = "ffprobe"
    width: int = 1080
    height: int = 1920
    frame_rate: int = 30
    maximum_assets: int = 12

    def __post_init__(self) -> None:
        root = self.asset_root.expanduser().resolve() if self.asset_root is not None else None
        catalogs = tuple(path.expanduser().resolve() for path in self.catalog_paths)
        if catalogs and (root is None or not root.is_dir()):
            raise ValueError("legacy catalogs require an existing FRAMEFACTORY_LEGACY_ASSET_ROOT")
        for catalog in catalogs:
            if not catalog.is_file():
                raise ValueError("every legacy asset catalog must be an existing file")
            assert root is not None
            if not catalog.is_relative_to(root):
                raise ValueError("legacy asset catalogs must stay inside FRAMEFACTORY_LEGACY_ASSET_ROOT")
        if not self.tts_voice or not self.tts_rate:
            raise ValueError("legacy TTS voice and rate must not be empty")
        if not self.ffmpeg_command or not self.ffprobe_command:
            raise ValueError("legacy FFmpeg commands must not be empty")
        if self.width < 2 or self.height < 2 or self.width % 2 or self.height % 2:
            raise ValueError("legacy render width and height must be positive even integers")
        if not 1 <= self.frame_rate <= 120:
            raise ValueError("legacy render frame rate must be between 1 and 120")
        if not 1 <= self.maximum_assets <= 50:
            raise ValueError("legacy maximum assets must be between 1 and 50")
        object.__setattr__(self, "asset_root", root)
        object.__setattr__(self, "catalog_paths", catalogs)


@dataclass(frozen=True, slots=True)
class AssetLibrarySettings:
    database_url: str
    minimum_similarity: float = 0.35
    maximum_assets: int = 12
    control_api_url: str | None = None
    acquisition_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        database = urlsplit(self.database_url)
        if database.scheme not in {"postgres", "postgresql"} or not database.hostname:
            raise ValueError("asset library requires a PostgreSQL URL with a host")
        if not isfinite(self.minimum_similarity) or not 0 < self.minimum_similarity <= 1:
            raise ValueError("FRAMEFACTORY_ASSET_MINIMUM_SIMILARITY must be in (0, 1]")
        if not 1 <= self.maximum_assets <= 50:
            raise ValueError("FRAMEFACTORY_ASSET_MAXIMUM_ASSETS must be between 1 and 50")
        if self.control_api_url:
            control_api = urlsplit(self.control_api_url)
            if (
                control_api.scheme not in {"http", "https"}
                or not control_api.hostname
                or control_api.username
                or control_api.password
            ):
                raise ValueError("FRAMEFACTORY_CONTROL_API_URL must be an HTTP(S) URL")
        if (
            not isfinite(self.acquisition_timeout_seconds)
            or self.acquisition_timeout_seconds <= 0
        ):
            raise ValueError("asset acquisition timeout must be positive and finite")


@dataclass(frozen=True, slots=True)
class AssetAnalysisSettings:
    """Explicit provider and queue settings for downloaded-asset analysis."""

    base_url: str
    api_key: str = field(repr=False)
    model: str = "qwen-vl-max"
    queue_name: str = "asset-analysis"
    timeout_seconds: float = 180.0
    rate_limit_per_minute: int = 30
    auto_ready: bool = True
    asr_base_url: str | None = None
    asr_api_key: str = field(default="", repr=False)
    asr_model: str | None = None
    asr_timeout_seconds: float = 600.0
    asr_response_format: Literal["json", "verbose_json"] = "verbose_json"
    asr_timestamp_mode: Literal["none", "segment", "word"] = "word"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("FRAMEFACTORY_ASSET_VISION_BASE_URL must be an HTTP(S) URL")
        if not self.api_key or not self.model:
            raise ValueError("asset vision API key and model must not be empty")
        if not _QUEUE.fullmatch(self.queue_name):
            raise ValueError("FRAMEFACTORY_ASSET_ANALYSIS_QUEUE contains unsupported characters")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("asset vision timeout must be positive and finite")
        if not 1 <= self.rate_limit_per_minute <= 600:
            raise ValueError("asset analysis rate limit must be between 1 and 600")
        asr_values = (self.asr_base_url, self.asr_api_key, self.asr_model)
        if any(asr_values) and not all(asr_values):
            raise ValueError(
                "FRAMEFACTORY_ASR_BASE_URL, FRAMEFACTORY_ASR_API_KEY and "
                "FRAMEFACTORY_ASR_MODEL must be configured together"
            )
        if self.asr_base_url:
            asr = urlsplit(self.asr_base_url)
            if asr.scheme not in {"http", "https"} or not asr.hostname:
                raise ValueError("FRAMEFACTORY_ASR_BASE_URL must be an HTTP(S) URL")
        if not isfinite(self.asr_timeout_seconds) or self.asr_timeout_seconds <= 0:
            raise ValueError("asset ASR timeout must be positive and finite")
        if self.asr_response_format not in {"json", "verbose_json"}:
            raise ValueError("FRAMEFACTORY_ASR_RESPONSE_FORMAT is unsupported")
        if self.asr_timestamp_mode not in {"none", "segment", "word"}:
            raise ValueError("FRAMEFACTORY_ASR_TIMESTAMP_MODE is unsupported")
        if self.asr_response_format == "json" and self.asr_timestamp_mode != "none":
            raise ValueError("JSON-only ASR responses cannot promise timestamp granularity")


@dataclass(frozen=True, slots=True)
class RunwaySettings:
    """Text-only generated-video provider configuration.

    Model and base URL are explicit deployment inputs so readiness never
    silently changes when a provider adds or reroutes models.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str = "gen4.5"
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "FRAMEFACTORY_RUNWAY_BASE_URL must be a credential-free HTTPS URL"
            )
        if not self.api_key:
            raise ValueError("FRAMEFACTORY_RUNWAY_API_KEY must not be empty")
        if self.model != "gen4.5":
            raise ValueError(
                "FRAMEFACTORY_RUNWAY_MODEL must be gen4.5 for generated-only text-to-video"
            )
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError(
                "FRAMEFACTORY_RUNWAY_TIMEOUT_SECONDS must be positive and finite"
            )


@dataclass(frozen=True, slots=True)
class FullAiVisionSettings:
    """Independent per-Beat vision provider for generated-only verification."""

    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "FRAMEFACTORY_FULL_AI_VISION_BASE_URL must be a credential-free HTTPS URL"
            )
        if not self.api_key or not self.model:
            raise ValueError("Full-AI vision API key and model must not be empty")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError(
                "FRAMEFACTORY_FULL_AI_VISION_TIMEOUT_SECONDS must be positive and finite"
            )


@dataclass(frozen=True, slots=True)
class WebCaptureSettings:
    """Dedicated browser-capture runtime and its fail-closed egress assertion."""

    proxy_url: str
    egress_policy_enforced: bool
    proxy_username: str = field(default="", repr=False)
    proxy_password: str = field(default="", repr=False)
    allow_insecure_loopback_proxy: bool = False
    disable_chromium_sandbox: bool = False
    allow_non_standard_ports: bool = False
    navigation_timeout_seconds: int = 20
    settle_timeout_seconds: int = 30
    screenshot_timeout_seconds: int = 10
    job_timeout_seconds: int = 90
    maximum_resources: int = 400
    maximum_transfer_bytes: int = 104_857_600
    maximum_body_characters: int = 12_000
    maximum_redirects: int = 5

    def __post_init__(self) -> None:
        parsed = urlsplit(self.proxy_url)
        loopback_http = (
            self.allow_insecure_loopback_proxy
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        )
        if (
            (parsed.scheme != "https" and not loopback_http)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "FRAMEFACTORY_BROWSER_EGRESS_PROXY_URL must be a credential-free HTTPS URL "
                "or an explicitly enabled development loopback HTTP proxy"
            )
        if not self.egress_policy_enforced:
            raise ValueError(
                "browser capture requires FRAMEFACTORY_BROWSER_EGRESS_POLICY_ENFORCED=true"
            )
        if bool(self.proxy_username) != bool(self.proxy_password):
            raise ValueError("browser egress proxy username and password must be paired")
        for name in (
            "navigation_timeout_seconds",
            "settle_timeout_seconds",
            "screenshot_timeout_seconds",
            "job_timeout_seconds",
            "maximum_resources",
            "maximum_transfer_bytes",
            "maximum_body_characters",
            "maximum_redirects",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"browser capture {name} must be positive")
        if self.job_timeout_seconds < self.navigation_timeout_seconds:
            raise ValueError("browser capture hard timeout must cover navigation timeout")
        if not 1 <= self.maximum_resources <= 2_000:
            raise ValueError("browser capture maximum resources must be between 1 and 2000")
        if not 1_048_576 <= self.maximum_transfer_bytes <= 536_870_912:
            raise ValueError("browser capture maximum bytes must be between 1 MiB and 512 MiB")
        if not 1_000 <= self.maximum_body_characters <= 50_000:
            raise ValueError("browser capture body characters must be between 1000 and 50000")
        if not 1 <= self.maximum_redirects <= 10:
            raise ValueError("browser capture maximum redirects must be between 1 and 10")


def _positive_float(environment: dict[str, str], name: str, default: str) -> float:
    try:
        value = float(environment.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _boolean(environment: dict[str, str], name: str, default: bool = False) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _secret(environment: dict[str, str], name: str) -> str:
    direct = environment.get(name, "").strip()
    file_name = environment.get(f"{name}_FILE", "").strip()
    if direct and file_name:
        raise ValueError(f"configure only one of {name} or {name}_FILE")
    if file_name:
        try:
            direct = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"could not read {name}_FILE") from exc
        if not direct:
            raise ValueError(f"{name}_FILE must not be empty")
    return direct


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_url: str
    redis_url: str
    environment: str = "production"
    intake_queue_name: str = "runs"
    queue_name: str = "run-steps"
    browser_capture_only: bool = False
    redis_namespace: str = "framefactory"
    worker_id: str = ""
    worker_concurrency: int = 1
    lease_seconds: float = 30.0
    poll_interval_seconds: float = 0.5
    recovery_interval_seconds: float = 15.0
    connect_timeout_seconds: float = 5.0
    allow_insecure_transport: bool = False
    openai_compatible: OpenAICompatibleSettings | None = None
    research_search: ResearchSearchSettings | None = None
    object_storage: ObjectStorageSettings | None = None
    legacy_media: LegacyMediaSettings | None = None
    asset_library: AssetLibrarySettings | None = None
    asset_analysis: AssetAnalysisSettings | None = None
    runway: RunwaySettings | None = None
    full_ai_vision: FullAiVisionSettings | None = None
    web_capture: WebCaptureSettings | None = None
    declared_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        database = urlsplit(self.database_url)
        redis = urlsplit(self.redis_url)
        if database.scheme not in {"postgres", "postgresql"} or not database.hostname:
            raise ValueError("FRAMEFACTORY_DATABASE_URL must be a PostgreSQL URL with a host")
        if redis.scheme not in {"redis", "rediss"} or not redis.hostname:
            raise ValueError("FRAMEFACTORY_REDIS_URL must be a Redis URL with a host")
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("FRAMEFACTORY_ENV must be development, test, or production")
        normalized_capabilities = tuple(
            sorted({str(value).strip() for value in self.declared_capabilities if str(value).strip()})
        )
        if any(not _QUEUE.fullmatch(value) for value in normalized_capabilities):
            raise ValueError(
                "FRAMEFACTORY_WORKER_CAPABILITIES contains an invalid operation name"
            )
        object.__setattr__(self, "declared_capabilities", normalized_capabilities)
        if not _QUEUE.fullmatch(self.intake_queue_name):
            raise ValueError("FRAMEFACTORY_RUN_QUEUE contains unsupported characters")
        if not _QUEUE.fullmatch(self.queue_name):
            raise ValueError("FRAMEFACTORY_STEP_QUEUE contains unsupported characters")
        if self.browser_capture_only and self.queue_name != "browser-capture":
            raise ValueError(
                "browser-capture-only workers must consume FRAMEFACTORY_STEP_QUEUE=browser-capture"
            )
        if not self.browser_capture_only and self.queue_name == "browser-capture":
            raise ValueError(
                "ordinary workers must not consume the reserved browser-capture queue"
            )
        if self.browser_capture_only != (self.web_capture is not None):
            raise ValueError(
                "browser capture settings and FRAMEFACTORY_BROWSER_CAPTURE_ONLY=true "
                "must be configured together"
            )
        if self.browser_capture_only:
            unrelated = tuple(
                name
                for name, value in (
                    ("openai_compatible", self.openai_compatible),
                    ("research_search", self.research_search),
                    ("legacy_media", self.legacy_media),
                    ("asset_library", self.asset_library),
                    ("asset_analysis", self.asset_analysis),
                    ("runway", self.runway),
                    ("full_ai_vision", self.full_ai_vision),
                )
                if value is not None
            )
            if unrelated:
                raise ValueError(
                    "browser-capture-only workers reject unrelated providers: "
                    + ", ".join(unrelated)
                )
        if not _QUEUE.fullmatch(self.redis_namespace):
            raise ValueError("FRAMEFACTORY_REDIS_NAMESPACE contains unsupported characters")
        if not self.worker_id or len(self.worker_id) > 255:
            raise ValueError("FRAMEFACTORY_WORKER_ID must contain 1 to 255 characters")
        if not 1 <= self.worker_concurrency <= 32:
            raise ValueError("FRAMEFACTORY_WORKER_CONCURRENCY must be between 1 and 32")
        for name in (
            "lease_seconds",
            "poll_interval_seconds",
            "recovery_interval_seconds",
            "connect_timeout_seconds",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if self.environment == "production" and not self.allow_insecure_transport:
            query = database.query.lower()
            if "sslmode=require" not in query and "sslmode=verify-" not in query:
                raise ValueError(
                    "production PostgreSQL requires sslmode=require/verify-*; "
                    "set FRAMEFACTORY_ALLOW_INSECURE_TRANSPORT only behind a trusted private network"
                )
            if redis.scheme != "rediss":
                raise ValueError(
                    "production Redis requires rediss://; set "
                    "FRAMEFACTORY_ALLOW_INSECURE_TRANSPORT only behind a trusted private network"
                )
            if self.openai_compatible and urlsplit(self.openai_compatible.base_url).scheme != "https":
                raise ValueError("production model provider requires HTTPS")
            if (
                self.object_storage
                and self.object_storage.endpoint_url
                and urlsplit(self.object_storage.endpoint_url).scheme != "https"
            ):
                raise ValueError("production object storage endpoint requires HTTPS")
        if (
            self.environment == "production"
            and self.openai_compatible is not None
            and urlsplit(self.openai_compatible.base_url).scheme != "https"
        ):
            raise ValueError("production model provider requires HTTPS")
        if self.openai_compatible is not None and self.object_storage is None:
            raise ValueError(
                "configured model providers require FRAMEFACTORY_S3_BUCKET for durable artifacts"
            )
        if self.legacy_media is not None and self.object_storage is None:
            raise ValueError(
                "configured legacy media providers require FRAMEFACTORY_S3_BUCKET for durable artifacts"
            )
        if self.asset_library is not None and self.object_storage is None:
            raise ValueError(
                "configured database asset library requires FRAMEFACTORY_S3_BUCKET"
            )
        if self.asset_analysis is not None and self.object_storage is None:
            raise ValueError("configured asset analysis requires FRAMEFACTORY_S3_BUCKET")
        if self.runway is not None and self.object_storage is None:
            raise ValueError(
                "configured Runway generation requires FRAMEFACTORY_S3_BUCKET"
            )
        if self.full_ai_vision is not None and self.object_storage is None:
            raise ValueError(
                "configured Full-AI vision verification requires FRAMEFACTORY_S3_BUCKET"
            )
        if self.web_capture is not None and self.object_storage is None:
            raise ValueError("configured browser capture requires FRAMEFACTORY_S3_BUCKET")
        if (
            self.browser_capture_only
            and self.object_storage is not None
            and self.object_storage.key_prefix != "browser-capture"
        ):
            raise ValueError(
                "browser-capture-only workers require "
                "FRAMEFACTORY_S3_KEY_PREFIX=browser-capture"
            )
        if (
            self.environment == "production"
            and not self.allow_insecure_transport
            and self.asset_analysis is not None
            and urlsplit(self.asset_analysis.base_url).scheme != "https"
        ):
            raise ValueError("production asset vision provider requires HTTPS")
        if (
            self.environment == "production"
            and not self.allow_insecure_transport
            and self.asset_analysis is not None
            and self.asset_analysis.asr_base_url is not None
            and urlsplit(self.asset_analysis.asr_base_url).scheme != "https"
        ):
            raise ValueError("production ASR provider requires HTTPS")
        if (
            self.environment == "production"
            and not self.allow_insecure_transport
            and self.runway is not None
            and urlsplit(self.runway.base_url).scheme != "https"
        ):
            raise ValueError("production Runway provider requires HTTPS")

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> WorkerSettings:
        environment = dict(os.environ if environ is None else environ)
        database_url = environment.get("FRAMEFACTORY_DATABASE_URL", "").strip()
        redis_url = environment.get("FRAMEFACTORY_REDIS_URL", "").strip()
        missing = [
            name
            for name, value in (
                ("FRAMEFACTORY_DATABASE_URL", database_url),
                ("FRAMEFACTORY_REDIS_URL", redis_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"required worker configuration is missing: {', '.join(missing)}")
        worker_id = environment.get("FRAMEFACTORY_WORKER_ID", "").strip()
        if not worker_id:
            worker_id = f"{socket.gethostname()}-{os.getpid()}"
        return cls(
            database_url=database_url,
            redis_url=redis_url,
            environment=environment.get("FRAMEFACTORY_ENV", "production").strip().lower(),
            intake_queue_name=environment.get("FRAMEFACTORY_RUN_QUEUE", "runs").strip(),
            queue_name=environment.get("FRAMEFACTORY_STEP_QUEUE", "run-steps").strip(),
            browser_capture_only=_boolean(
                environment, "FRAMEFACTORY_BROWSER_CAPTURE_ONLY"
            ),
            redis_namespace=environment.get(
                "FRAMEFACTORY_REDIS_NAMESPACE", "framefactory"
            ).strip(),
            worker_id=worker_id,
            worker_concurrency=_positive_int(
                environment, "FRAMEFACTORY_WORKER_CONCURRENCY", "1"
            ),
            lease_seconds=_positive_float(
                environment, "FRAMEFACTORY_WORKER_LEASE_SECONDS", "30"
            ),
            poll_interval_seconds=_positive_float(
                environment, "FRAMEFACTORY_WORKER_POLL_SECONDS", "0.5"
            ),
            recovery_interval_seconds=_positive_float(
                environment, "FRAMEFACTORY_WORKER_RECOVERY_SECONDS", "15"
            ),
            connect_timeout_seconds=_positive_float(
                environment, "FRAMEFACTORY_CONNECT_TIMEOUT_SECONDS", "5"
            ),
            allow_insecure_transport=_boolean(
                environment, "FRAMEFACTORY_ALLOW_INSECURE_TRANSPORT"
            ),
            openai_compatible=_openai_settings(environment),
            research_search=_research_search_settings(environment),
            object_storage=_object_storage_settings(environment),
            legacy_media=_legacy_media_settings(environment),
            asset_library=_asset_library_settings(environment, database_url),
            asset_analysis=_asset_analysis_settings(environment),
            runway=_runway_settings(environment),
            full_ai_vision=_full_ai_vision_settings(environment),
            web_capture=_web_capture_settings(environment),
            declared_capabilities=tuple(
                value.strip()
                for value in environment.get(
                    "FRAMEFACTORY_WORKER_CAPABILITIES", ""
                ).split(",")
                if value.strip()
            ),
        )


def _openai_settings(environment: dict[str, str]) -> OpenAICompatibleSettings | None:
    base_url = environment.get("FRAMEFACTORY_OPENAI_BASE_URL", "").strip()
    api_key = _secret(environment, "FRAMEFACTORY_OPENAI_API_KEY")
    models = (
        environment.get("FRAMEFACTORY_OPENAI_RESEARCH_MODEL", "").strip(),
        environment.get("FRAMEFACTORY_OPENAI_WRITING_MODEL", "").strip(),
        environment.get("FRAMEFACTORY_OPENAI_QUALITY_MODEL", "").strip(),
    )
    if not base_url and not api_key and not any(models):
        return None
    return OpenAICompatibleSettings(
        base_url=base_url,
        api_key=api_key,
        research_model=models[0],
        writing_model=models[1],
        quality_model=models[2],
        timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_OPENAI_TIMEOUT_SECONDS", "60"
        ),
    )


def _research_search_settings(
    environment: dict[str, str],
) -> ResearchSearchSettings | None:
    url = environment.get("FRAMEFACTORY_RESEARCH_SEARCH_URL", "").strip()
    bearer_token = _secret(
        environment, "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN"
    )
    raw_timeout = environment.get(
        "FRAMEFACTORY_RESEARCH_SEARCH_TIMEOUT_SECONDS", ""
    ).strip()
    raw_maximum = environment.get(
        "FRAMEFACTORY_RESEARCH_SEARCH_MAX_RESPONSE_BYTES", ""
    ).strip()
    if not any((url, bearer_token, raw_timeout, raw_maximum)):
        return None
    if not url or not bearer_token:
        raise ValueError(
            "FRAMEFACTORY_RESEARCH_SEARCH_URL and "
            "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN must be configured together"
        )
    return ResearchSearchSettings(
        url=url,
        bearer_token=bearer_token,
        timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_RESEARCH_SEARCH_TIMEOUT_SECONDS", "20"
        ),
        maximum_response_bytes=_positive_int(
            environment,
            "FRAMEFACTORY_RESEARCH_SEARCH_MAX_RESPONSE_BYTES",
            "1048576",
        ),
    )


def _object_storage_settings(environment: dict[str, str]) -> ObjectStorageSettings | None:
    bucket = environment.get("FRAMEFACTORY_S3_BUCKET", "").strip()
    if not bucket:
        return None
    return ObjectStorageSettings(
        bucket=bucket,
        region=environment.get("FRAMEFACTORY_S3_REGION", "us-east-1").strip(),
        key_prefix=environment.get("FRAMEFACTORY_S3_KEY_PREFIX", "").strip(),
        endpoint_url=environment.get("FRAMEFACTORY_S3_ENDPOINT_URL", "").strip() or None,
        access_key_id=_secret(environment, "FRAMEFACTORY_S3_ACCESS_KEY_ID") or None,
        secret_access_key=_secret(environment, "FRAMEFACTORY_S3_SECRET_ACCESS_KEY") or None,
    )


def _positive_int(environment: dict[str, str], name: str, default: str) -> int:
    raw = environment.get(name, default).strip()
    if not raw.isdecimal():
        raise ValueError(f"{name} must be a positive integer")
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _legacy_media_settings(environment: dict[str, str]) -> LegacyMediaSettings | None:
    if not _boolean(environment, "FRAMEFACTORY_LEGACY_MEDIA_ENABLED"):
        return None
    root_value = environment.get("FRAMEFACTORY_LEGACY_ASSET_ROOT", "").strip()
    catalogs_value = environment.get("FRAMEFACTORY_LEGACY_ASSET_CATALOGS", "").strip()
    if bool(root_value) != bool(catalogs_value):
        raise ValueError(
            "FRAMEFACTORY_LEGACY_ASSET_ROOT and FRAMEFACTORY_LEGACY_ASSET_CATALOGS "
            "must be configured together"
        )
    catalogs = tuple(
        Path(value.strip())
        for value in catalogs_value.split(os.pathsep)
        if value.strip()
    )
    return LegacyMediaSettings(
        asset_root=Path(root_value) if root_value else None,
        catalog_paths=catalogs,
        tts_voice=environment.get(
            "FRAMEFACTORY_LEGACY_TTS_VOICE", "zh-CN-YunjianNeural"
        ).strip(),
        tts_rate=environment.get("FRAMEFACTORY_LEGACY_TTS_RATE", "+25%").strip(),
        ffmpeg_command=environment.get("FRAMEFACTORY_FFMPEG_COMMAND", "ffmpeg").strip(),
        ffprobe_command=environment.get("FRAMEFACTORY_FFPROBE_COMMAND", "ffprobe").strip(),
        width=_positive_int(environment, "FRAMEFACTORY_LEGACY_RENDER_WIDTH", "1080"),
        height=_positive_int(environment, "FRAMEFACTORY_LEGACY_RENDER_HEIGHT", "1920"),
        frame_rate=_positive_int(environment, "FRAMEFACTORY_LEGACY_RENDER_FPS", "30"),
        maximum_assets=_positive_int(
            environment, "FRAMEFACTORY_LEGACY_MAXIMUM_ASSETS", "12"
        ),
    )


def _asset_library_settings(
    environment: dict[str, str], database_url: str
) -> AssetLibrarySettings | None:
    if not _boolean(environment, "FRAMEFACTORY_ASSET_LIBRARY_ENABLED"):
        return None
    return AssetLibrarySettings(
        database_url=database_url,
        minimum_similarity=_positive_float(
            environment, "FRAMEFACTORY_ASSET_MINIMUM_SIMILARITY", "0.35"
        ),
        maximum_assets=_positive_int(
            environment, "FRAMEFACTORY_ASSET_MAXIMUM_ASSETS", "12"
        ),
        control_api_url=environment.get("FRAMEFACTORY_CONTROL_API_URL", "").strip()
        or None,
        acquisition_timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_ASSET_ACQUISITION_TIMEOUT_SECONDS", "600"
        ),
    )


def _asset_analysis_settings(
    environment: dict[str, str],
) -> AssetAnalysisSettings | None:
    base_url = environment.get("FRAMEFACTORY_ASSET_VISION_BASE_URL", "").strip()
    api_key = _secret(environment, "FRAMEFACTORY_ASSET_VISION_API_KEY")
    model = environment.get("FRAMEFACTORY_ASSET_VISION_MODEL", "").strip()
    asr_base_url = environment.get("FRAMEFACTORY_ASR_BASE_URL", "").strip()
    asr_api_key = _secret(environment, "FRAMEFACTORY_ASR_API_KEY")
    asr_model = environment.get("FRAMEFACTORY_ASR_MODEL", "").strip()
    if not any((base_url, api_key, model, asr_base_url, asr_api_key, asr_model)):
        return None
    return AssetAnalysisSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        queue_name=environment.get(
            "FRAMEFACTORY_ASSET_ANALYSIS_QUEUE", "asset-analysis"
        ).strip(),
        timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_ASSET_VISION_TIMEOUT_SECONDS", "180"
        ),
        rate_limit_per_minute=_positive_int(
            environment, "FRAMEFACTORY_ASSET_ANALYSIS_RATE_PER_MINUTE", "30"
        ),
        auto_ready=_boolean(
            environment, "FRAMEFACTORY_ASSET_ANALYSIS_AUTO_READY", True
        ),
        asr_base_url=asr_base_url or None,
        asr_api_key=asr_api_key,
        asr_model=asr_model or None,
        asr_timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_ASR_TIMEOUT_SECONDS", "600"
        ),
        asr_response_format=environment.get(
            "FRAMEFACTORY_ASR_RESPONSE_FORMAT", "verbose_json"
        ).strip(),
        asr_timestamp_mode=environment.get(
            "FRAMEFACTORY_ASR_TIMESTAMP_MODE", "word"
        ).strip(),
    )


def _runway_settings(environment: dict[str, str]) -> RunwaySettings | None:
    base_url = environment.get("FRAMEFACTORY_RUNWAY_BASE_URL", "").strip()
    api_key = _secret(environment, "FRAMEFACTORY_RUNWAY_API_KEY")
    model = environment.get("FRAMEFACTORY_RUNWAY_MODEL", "").strip()
    raw_timeout = environment.get("FRAMEFACTORY_RUNWAY_TIMEOUT_SECONDS", "").strip()
    if not any((base_url, api_key, model, raw_timeout)):
        return None
    if not all((base_url, api_key, model)):
        raise ValueError(
            "FRAMEFACTORY_RUNWAY_BASE_URL, FRAMEFACTORY_RUNWAY_API_KEY and "
            "FRAMEFACTORY_RUNWAY_MODEL must be configured together"
        )
    return RunwaySettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_RUNWAY_TIMEOUT_SECONDS", "60"
        ),
    )


def _full_ai_vision_settings(
    environment: dict[str, str],
) -> FullAiVisionSettings | None:
    base_url = environment.get("FRAMEFACTORY_FULL_AI_VISION_BASE_URL", "").strip()
    api_key = _secret(environment, "FRAMEFACTORY_FULL_AI_VISION_API_KEY")
    model = environment.get("FRAMEFACTORY_FULL_AI_VISION_MODEL", "").strip()
    raw_timeout = environment.get(
        "FRAMEFACTORY_FULL_AI_VISION_TIMEOUT_SECONDS", ""
    ).strip()
    if not any((base_url, api_key, model, raw_timeout)):
        return None
    if not all((base_url, api_key, model)):
        raise ValueError(
            "FRAMEFACTORY_FULL_AI_VISION_BASE_URL, "
            "FRAMEFACTORY_FULL_AI_VISION_API_KEY and "
            "FRAMEFACTORY_FULL_AI_VISION_MODEL must be configured together"
        )
    return FullAiVisionSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=_positive_float(
            environment, "FRAMEFACTORY_FULL_AI_VISION_TIMEOUT_SECONDS", "180"
        ),
    )


def _web_capture_settings(
    environment: dict[str, str],
) -> WebCaptureSettings | None:
    enabled = _boolean(environment, "FRAMEFACTORY_BROWSER_CAPTURE_ENABLED")
    if not enabled:
        browser_values = (
            environment.get("FRAMEFACTORY_BROWSER_EGRESS_PROXY_URL", ""),
            environment.get("FRAMEFACTORY_BROWSER_EGRESS_PROXY_USERNAME", ""),
            environment.get("FRAMEFACTORY_BROWSER_EGRESS_PROXY_PASSWORD", ""),
        )
        if any(value.strip() for value in browser_values):
            raise ValueError(
                "browser capture proxy settings require "
                "FRAMEFACTORY_BROWSER_CAPTURE_ENABLED=true"
            )
        return None
    proxy_url = environment.get(
        "FRAMEFACTORY_BROWSER_EGRESS_PROXY_URL", ""
    ).strip()
    if not proxy_url:
        raise ValueError("browser capture requires FRAMEFACTORY_BROWSER_EGRESS_PROXY_URL")
    allow_loopback_proxy = _boolean(
        environment, "FRAMEFACTORY_BROWSER_ALLOW_INSECURE_LOOPBACK_PROXY"
    )
    disable_chromium_sandbox = _boolean(
        environment, "FRAMEFACTORY_BROWSER_DISABLE_CHROMIUM_SANDBOX"
    )
    development_only_override = allow_loopback_proxy or disable_chromium_sandbox
    if (
        development_only_override
        and environment.get("FRAMEFACTORY_ENV", "production").lower()
        != "development"
    ):
        raise ValueError(
            "browser loopback/no-sandbox overrides are development-only"
        )
    return WebCaptureSettings(
        proxy_url=proxy_url,
        egress_policy_enforced=_boolean(
            environment, "FRAMEFACTORY_BROWSER_EGRESS_POLICY_ENFORCED"
        ),
        proxy_username=_secret(
            environment, "FRAMEFACTORY_BROWSER_EGRESS_PROXY_USERNAME"
        ),
        proxy_password=_secret(
            environment, "FRAMEFACTORY_BROWSER_EGRESS_PROXY_PASSWORD"
        ),
        allow_insecure_loopback_proxy=allow_loopback_proxy,
        disable_chromium_sandbox=disable_chromium_sandbox,
        allow_non_standard_ports=_boolean(
            environment, "FRAMEFACTORY_BROWSER_ALLOW_NON_STANDARD_PORTS"
        ),
        navigation_timeout_seconds=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_NAVIGATION_TIMEOUT_SECONDS", "20"
        ),
        settle_timeout_seconds=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_SETTLE_TIMEOUT_SECONDS", "30"
        ),
        screenshot_timeout_seconds=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_SCREENSHOT_TIMEOUT_SECONDS", "10"
        ),
        job_timeout_seconds=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_JOB_TIMEOUT_SECONDS", "90"
        ),
        maximum_resources=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_MAXIMUM_RESOURCES", "400"
        ),
        maximum_transfer_bytes=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_MAXIMUM_TRANSFER_BYTES", "104857600"
        ),
        maximum_body_characters=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_MAXIMUM_BODY_CHARACTERS", "12000"
        ),
        maximum_redirects=_positive_int(
            environment, "FRAMEFACTORY_BROWSER_MAXIMUM_REDIRECTS", "5"
        ),
    )
