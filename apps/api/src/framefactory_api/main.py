from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .account import (
    AccountCapabilitiesResponse,
    AccountService,
    ApiKeyCreate,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    CreationPreferencesResponse,
    CreationPreferencesUpdate,
    ProfileResponse,
    ProfileUpdate,
    SessionResponse,
)
from .context import (
    DefaultWorkspaceContextProvider,
    WorkspaceContext,
    WorkspaceContextProvider,
)
from .contracts import ContractValidator
from .errors import ApiError, ValidationError
from .full_ai import (
    FullAiControlService,
    FullAiEstimateRequest,
    FullAiEstimateResponse,
    FullAiOptionsResponse,
    FullAiRunCreate,
    FullAiRunResponse,
    FullAiRuntimeConfiguration,
)
from .models import (
    AssetAcquisitionCreate,
    AssetBatchReanalysis,
    AssetBatchReview,
    AssetBatchTags,
    AssetLibraryCreate,
    AssetMetadataPatch,
    AssetReanalysisRequest,
    AssetStatusTransition,
    AssetUploadComplete,
    AssetUploadCreate,
    BatchItemPage,
    ChannelWrite,
    ContextResponse,
    ForkSkillRequest,
    ForkSkillResponse,
    GenerationBatchCreate,
    GenerationBatchRetryFailed,
    HealthResponse,
    LibraryBuildJobCreate,
    PublishSkillVersionRequest,
    RemoteAssetImportCreate,
    ResourcePage,
    RunCreate,
    SkillIdentityInput,
    SkillTestExecutionCreate,
    SkillVersionCreate,
    SkillVersionPatch,
    StepReviewRequest,
    ValidationReport,
)
from .queue import JobQueue, QueueError
from .remote_assets import RemoteAssetError, RemoteAssetGateway
from .repository import ControlRepository, InMemoryControlRepository, Resource
from .seed_catalog import load_official_catalog
from .service import ControlService
from .settings import Settings
from .storage import (
    ObjectIntegrityError,
    ObjectLocator,
    ObjectNotFound,
    ObjectStorage,
    ObjectStorageUnavailable,
)
from .webpage_video import (
    WebpageArtifactResponse,
    WebpageCaptureResponse,
    WebpageCaptureReviewRequest,
    WebpagePageResponse,
    WebpageScopeContent,
    WebpageScopeReviewRequest,
    WebpageSiteManifestResponse,
    WebpageSiteResponse,
    WebpageStoryboardContent,
    WebpageStoryboardReviewRequest,
    WebpageVideoControlService,
    WebpageVideoOptionsResponse,
    WebpageVideoRunCreate,
    WebpageVideoRunResponse,
    WebpageVideoRuntimeConfiguration,
)

WorkspaceHeader = Annotated[UUID | None, Header(alias="X-Workspace-Id")]
IdempotencyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=255,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
IfMatchHeader = Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)]
OptionalIfMatchHeader = Annotated[
    str | None, Header(alias="If-Match", min_length=1, max_length=32)
]


def _get_service(request: Request) -> ControlService:
    return request.app.state.control_service


def _get_context_provider(request: Request) -> WorkspaceContextProvider:
    return request.app.state.context_provider


def _get_account_service(request: Request) -> AccountService:
    return request.app.state.account_service


def _get_full_ai_service(request: Request) -> FullAiControlService:
    return request.app.state.full_ai_service


def _get_webpage_video_service(request: Request) -> WebpageVideoControlService:
    return request.app.state.webpage_video_service


def _get_context(
    provider: Annotated[WorkspaceContextProvider, Depends(_get_context_provider)],
    requested_workspace_id: WorkspaceHeader = None,
) -> WorkspaceContext:
    return provider.resolve(requested_workspace_id)


ServiceDependency = Annotated[ControlService, Depends(_get_service)]
ContextDependency = Annotated[WorkspaceContext, Depends(_get_context)]
AccountServiceDependency = Annotated[AccountService, Depends(_get_account_service)]
FullAiServiceDependency = Annotated[FullAiControlService, Depends(_get_full_ai_service)]
WebpageVideoServiceDependency = Annotated[
    WebpageVideoControlService, Depends(_get_webpage_video_service)
]


def _page(resources: Sequence[Resource], limit: int, cursor: str | None) -> ResourcePage:
    start = _decode_cursor(cursor)
    if start > len(resources):
        raise ValidationError("cursor is outside the current result set", path="cursor")
    window = list(resources[start : start + limit])
    next_offset = start + len(window)
    has_more = next_offset < len(resources)
    return ResourcePage(
        data=window,
        page={
            "limit": limit,
            "has_more": has_more,
            "next_cursor": _encode_cursor(next_offset) if has_more else None,
        },
    )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        offset = value["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("cursor is invalid", path="cursor") from exc


def _asset_filter_fingerprint(filters: dict[str, Any]) -> str:
    encoded = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _asset_page(
    resources: Sequence[Resource],
    *,
    limit: int,
    cursor: str | None,
    filter_fingerprint: str,
) -> ResourcePage:
    start = 0
    if cursor is not None:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            if value.get("filters") != filter_fingerprint:
                raise ValueError
            marker = (str(value["updated_at"]), str(value["id"]))
            start = next(
                (
                    index
                    for index, item in enumerate(resources)
                    if (str(item.get("updated_at", "")), str(item["id"])) < marker
                ),
                len(resources),
            )
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "cursor is invalid for the current asset filters", path="cursor"
            ) from exc
    window = list(resources[start : start + limit])
    has_more = start + len(window) < len(resources)
    next_cursor = None
    if has_more and window:
        last = window[-1]
        payload = {
            "updated_at": last.get("updated_at", ""),
            "id": last["id"],
            "filters": filter_fingerprint,
        }
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")
    return ResourcePage(
        data=window,
        page={"limit": limit, "has_more": has_more, "next_cursor": next_cursor},
    )


def _public_asset(resource: Resource) -> Resource:
    value = json.loads(json.dumps(resource, default=str))
    file = value.get("file")
    if isinstance(file, dict):
        for key in ("bucket", "object_key", "content_hash", "storage_provider"):
            file.pop(key, None)
    value.pop("upload", None)
    return value


def _revision(if_match: str) -> int:
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"')
    try:
        revision = int(value)
    except ValueError as exc:
        raise ValidationError("If-Match must contain a numeric revision", path="If-Match") from exc
    if revision < 1:
        raise ValidationError("If-Match revision must be positive", path="If-Match")
    return revision


def _required_revision(if_match: str | None) -> int:
    if if_match is None:
        raise ApiError(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "PRECONDITION_REQUIRED",
            "If-Match header is required for this Channel operation",
            details={"header": "If-Match"},
        )
    return _revision(if_match)


def _etag(resource: Resource) -> str:
    return f'"{int(resource.get("revision", 1))}"'


def _require_asset_permission(context: WorkspaceContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "ASSET_PERMISSION_DENIED",
            "The current identity is not allowed to perform this asset operation",
            details={"required_permission": permission},
        )


def _require_url_capture_permission(
    context: WorkspaceContext, permission: str
) -> None:
    if permission not in context.permissions:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "URL_CAPTURE_PERMISSION_DENIED",
            "The current identity is not allowed to perform this URL capture operation",
            details={"required_permission": permission},
        )


async def _sign_webpage_artifact(
    artifact: WebpageArtifactResponse | None,
    *,
    context: WorkspaceContext,
    repository: ControlRepository,
    storage: ObjectStorage | None,
) -> WebpageArtifactResponse | None:
    if artifact is None:
        return None
    if storage is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WEBPAGE_VIDEO_OBJECT_STORAGE_UNAVAILABLE",
            "Object storage is unavailable for webpage-video preview delivery",
        )
    persisted = await repository.get_artifact(context.workspace_id, artifact.id)
    if persisted.get("content_hash") != artifact.sha256:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "WEBPAGE_VIDEO_ARTIFACT_CHANGED",
            "The persisted artifact no longer matches the reviewed content hash",
            details={"artifact_id": str(artifact.id)},
        )
    locator = ObjectLocator(
        key=str(persisted["object_key"]),
        sha256=artifact.sha256,
        content_type=artifact.media_type,
    )
    try:
        signed = await storage.presign_download(
            context.workspace_id, locator, expires_in=900
        )
    except (ObjectNotFound, ObjectIntegrityError, ObjectStorageUnavailable) as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WEBPAGE_VIDEO_ARTIFACT_UNAVAILABLE",
            "The webpage-video artifact could not be delivered",
            details={"artifact_id": str(artifact.id)},
        ) from exc
    return artifact.model_copy(
        update={
            "preview_url": signed.url,
            "download_url": signed.url,
            "url_expires_at": signed.expires_at,
        }
    )


async def _sign_webpage_capture(
    capture: WebpageCaptureResponse,
    *,
    context: WorkspaceContext,
    repository: ControlRepository,
    storage: ObjectStorage | None,
) -> WebpageCaptureResponse:
    artifact = await _sign_webpage_artifact(
        capture.artifact,
        context=context,
        repository=repository,
        storage=storage,
    )
    return capture.model_copy(update={"artifact": artifact})


async def _sign_webpage_run(
    resource: WebpageVideoRunResponse,
    *,
    context: WorkspaceContext,
    repository: ControlRepository,
    storage: ObjectStorage | None,
) -> WebpageVideoRunResponse:
    final_video = await _sign_webpage_artifact(
        resource.final_video,
        context=context,
        repository=repository,
        storage=storage,
    )
    return resource.model_copy(update={"final_video": final_video})


async def _sign_webpage_page(
    page: WebpagePageResponse,
    *,
    context: WorkspaceContext,
    repository: ControlRepository,
    storage: ObjectStorage | None,
) -> WebpagePageResponse:
    screenshot = await _sign_webpage_artifact(
        page.screenshot, context=context, repository=repository, storage=storage
    )
    regions = []
    for region in page.regions:
        artifact = await _sign_webpage_artifact(
            region.artifact, context=context, repository=repository, storage=storage
        )
        regions.append(region.model_copy(update={"artifact": artifact}))
    return page.model_copy(update={"screenshot": screenshot, "regions": regions})


async def _sign_webpage_site_manifest(
    manifest: WebpageSiteManifestResponse | None,
    *,
    context: WorkspaceContext,
    repository: ControlRepository,
    storage: ObjectStorage | None,
) -> WebpageSiteManifestResponse | None:
    if manifest is None:
        return None
    artifact = await _sign_webpage_artifact(
        manifest.artifact, context=context, repository=repository, storage=storage
    )
    content = manifest.content
    if isinstance(content, WebpageScopeContent):
        pages = [
            await _sign_webpage_page(
                page, context=context, repository=repository, storage=storage
            )
            for page in content.pages
        ]
        content = content.model_copy(update={"pages": pages})
    elif isinstance(content, WebpageStoryboardContent):
        pages = [
            await _sign_webpage_page(
                page, context=context, repository=repository, storage=storage
            )
            for page in content.pages
        ]
        shots = []
        for shot in content.shots:
            signed = await _sign_webpage_artifact(
                shot.artifact, context=context, repository=repository, storage=storage
            )
            shots.append(shot.model_copy(update={"artifact": signed}))
        content = content.model_copy(update={"pages": pages, "shots": shots})
    return manifest.model_copy(update={"artifact": artifact, "content": content})


async def _sign_webpage_site(
    site: WebpageSiteResponse,
    *,
    context: WorkspaceContext,
    repository: ControlRepository,
    storage: ObjectStorage | None,
) -> WebpageSiteResponse:
    scope = await _sign_webpage_site_manifest(
        site.scope, context=context, repository=repository, storage=storage
    )
    storyboard = await _sign_webpage_site_manifest(
        site.storyboard, context=context, repository=repository, storage=storage
    )
    return site.model_copy(update={"scope": scope, "storyboard": storyboard})


async def _enqueue_asset_analysis(
    queue: JobQueue,
    *,
    job: Resource,
    asset_id: UUID,
    workspace_id: UUID,
) -> None:
    """Publish one durable asset-analysis command using a stable dedupe key."""

    await queue.enqueue(
        queue_name="asset-analysis",
        payload={
            "job_id": job["id"],
            "asset_id": str(asset_id),
            "workspace_id": str(workspace_id),
        },
        deduplication_key=f"asset-analysis:{job['id']}",
        max_attempts=3,
    )


def create_app(
    *,
    settings: Settings | None = None,
    repository: ControlRepository | None = None,
    context_provider: WorkspaceContextProvider | None = None,
    job_queue: JobQueue | None = None,
    object_storage: ObjectStorage | None = None,
    remote_asset_gateway: RemoteAssetGateway | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    resolved_provider = context_provider or DefaultWorkspaceContextProvider(resolved_settings)
    validator = ContractValidator(resolved_settings.contract_schema_dir)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        owned_resources: list[Any] = []
        try:
            resolved_repository = repository
            if resolved_repository is None:
                resolved_repository = await _create_repository(resolved_settings, validator)
                owned_resources.append(resolved_repository)

            resolved_queue = job_queue
            if resolved_queue is None and resolved_settings.redis_enabled:
                resolved_queue = _create_redis_queue()
                owned_resources.append(resolved_queue)

            resolved_storage = object_storage
            if resolved_storage is None and resolved_settings.object_storage_enabled:
                resolved_storage = await _create_object_storage(resolved_settings)
                owned_resources.append(resolved_storage)

            await _healthcheck(resolved_repository)
            await _healthcheck(resolved_queue)
            await _healthcheck(resolved_storage)
            context = resolved_provider.resolve()
            await resolved_repository.ensure_account(
                context.user_id,
                context.workspace_id,
                email=resolved_settings.default_user_email,
                display_name=resolved_settings.default_user_display_name,
            )

            application.state.repository = resolved_repository
            application.state.job_queue = resolved_queue
            application.state.object_storage = resolved_storage
            application.state.remote_asset_gateway = (
                remote_asset_gateway or RemoteAssetGateway()
            )
            application.state.control_service = ControlService(
                resolved_repository,
                validator,
                worker_capabilities=resolved_settings.worker_capabilities,
            )
            application.state.account_service = AccountService(resolved_repository)
            application.state.full_ai_service = FullAiControlService(
                resolved_repository,
                FullAiRuntimeConfiguration(
                    provider_name=resolved_settings.full_ai_provider_name,
                    model_id=resolved_settings.full_ai_model_id,
                    cost_per_second_minor=(
                        resolved_settings.full_ai_cost_per_second_minor
                    ),
                    credit_unit_minor=resolved_settings.full_ai_credit_unit_minor,
                    worker_capabilities=resolved_settings.worker_capabilities,
                    queue_available=resolved_queue is not None,
                    object_storage_available=resolved_storage is not None,
                    terms_reference=resolved_settings.full_ai_terms_reference,
                    terms_content_hash=resolved_settings.full_ai_terms_content_hash,
                    terms_captured_at=resolved_settings.full_ai_terms_captured_at,
                    pricing_reference=resolved_settings.full_ai_pricing_reference,
                    pricing_content_hash=resolved_settings.full_ai_pricing_content_hash,
                    pricing_captured_at=resolved_settings.full_ai_pricing_captured_at,
                    output_rights_confirmed=(
                        resolved_settings.full_ai_output_rights_confirmed
                    ),
                    output_rights_license_basis=(
                        resolved_settings.full_ai_output_rights_license_basis
                    ),
                ),
                queue=resolved_queue,
                validate_scheduler_run=lambda value: validator.validate("run", value),
            )
            application.state.webpage_video_service = WebpageVideoControlService(
                resolved_repository,
                WebpageVideoRuntimeConfiguration(
                    worker_capabilities=resolved_settings.worker_capabilities,
                    queue_available=resolved_queue is not None,
                    object_storage_available=resolved_storage is not None,
                    public_delivery_available=(
                        resolved_storage is not None
                        and (
                            (
                                getattr(
                                    getattr(resolved_storage, "settings", None),
                                    "public_endpoint_url",
                                    None,
                                )
                                or getattr(
                                    getattr(resolved_storage, "settings", None),
                                    "endpoint_url",
                                    None,
                                )
                            )
                            in {None, ""}
                            or str(
                                getattr(
                                    getattr(resolved_storage, "settings", None),
                                    "public_endpoint_url",
                                    None,
                                )
                                or getattr(
                                    getattr(resolved_storage, "settings", None),
                                    "endpoint_url",
                                    "",
                                )
                            ).startswith("https://")
                            or (
                                bool(
                                    getattr(
                                        getattr(resolved_storage, "settings", None),
                                        "allow_insecure_loopback_public_endpoint",
                                        False,
                                    )
                                )
                                and bool(
                                    getattr(
                                        getattr(resolved_storage, "settings", None),
                                        "public_endpoint_url",
                                        None,
                                    )
                                )
                            )
                        )
                    ),
                ),
                queue=resolved_queue,
                validate_scheduler_run=lambda value: validator.validate("run", value),
            )
            application.state.persistence = resolved_settings.repository_backend
            yield
        finally:
            for resource in reversed(owned_resources):
                await _close(resource)

    app = FastAPI(
        title=resolved_settings.app_name,
        version="1.0.0",
        summary="Single-workspace Vistora control plane",
        description=(
            "The current runtime always resolves one default workspace. X-Workspace-Id is an "
            "optional forward-compatibility hint and does not select a tenant."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.context_provider = resolved_provider
    app.state.repository = repository
    app.state.job_queue = job_queue
    app.state.object_storage = object_storage
    app.state.remote_asset_gateway = remote_asset_gateway
    app.state.full_ai_service = None
    app.state.webpage_video_service = None
    app.state.persistence = resolved_settings.repository_backend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.cors_allow_origins),
        allow_origin_regex=resolved_settings.cors_allow_origin_regex,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Request-Id",
            "X-Workspace-Id",
        ],
        expose_headers=["ETag", "Location", "X-Request-Id"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Any) -> Response:
        request.state.request_id = _request_id(request)
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        request_id = _request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "schema_version": "1.0.0",
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
                "details": exc.details,
            },
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _request_id(request)
        return JSONResponse(
            status_code=422,
            content={
                "schema_version": "1.0.0",
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "The request does not match the API contract",
                "request_id": request_id,
                "details": {"errors": json.loads(json.dumps(exc.errors(), default=str))},
            },
            headers={"X-Request-Id": request_id},
        )

    @app.get("/healthz", response_model=HealthResponse, tags=["System"])
    async def health(request: Request) -> HealthResponse:
        await _healthcheck(request.app.state.repository)
        await _healthcheck(request.app.state.job_queue)
        await _healthcheck(request.app.state.object_storage)
        return HealthResponse(
            status="ok",
            service="framefactory-api",
            persistence=(
                "process_memory"
                if request.app.state.persistence == "memory"
                else "postgresql"
            ),
        )

    @app.get("/readyz", response_model=HealthResponse, tags=["System"])
    async def readiness(request: Request) -> HealthResponse:
        return await health(request)

    @app.get("/v1/context", response_model=ContextResponse, tags=["Context"])
    async def current_context(context: ContextDependency) -> ContextResponse:
        return ContextResponse(
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workspace_name=context.workspace_name,
        )

    @app.get(
        "/v1/full-ai/options",
        response_model=FullAiOptionsResponse,
        tags=["Full AI"],
    )
    async def get_full_ai_options(
        full_ai: FullAiServiceDependency,
        context: ContextDependency,
    ) -> FullAiOptionsResponse:
        return await full_ai.options(context)

    @app.post(
        "/v1/full-ai/estimate",
        response_model=FullAiEstimateResponse,
        tags=["Full AI"],
    )
    async def estimate_full_ai_run(
        command: FullAiEstimateRequest,
        full_ai: FullAiServiceDependency,
        context: ContextDependency,
    ) -> FullAiEstimateResponse:
        return await full_ai.estimate(context, command)

    @app.post(
        "/v1/full-ai/runs",
        response_model=FullAiRunResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Full AI"],
    )
    async def create_full_ai_run(
        command: FullAiRunCreate,
        full_ai: FullAiServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> FullAiRunResponse:
        resource, _created = await full_ai.create_run(
            context, command, idempotency_key
        )
        response.headers["Location"] = f"/v1/full-ai/runs/{resource.id}"
        return resource

    @app.get(
        "/v1/full-ai/runs/{full_ai_run_id}",
        response_model=FullAiRunResponse,
        tags=["Full AI"],
    )
    async def get_full_ai_run(
        full_ai_run_id: UUID,
        full_ai: FullAiServiceDependency,
        context: ContextDependency,
    ) -> FullAiRunResponse:
        return await full_ai.get_run(context, full_ai_run_id)

    @app.get(
        "/v1/webpage-video/options",
        response_model=WebpageVideoOptionsResponse,
        tags=["Webpage Video"],
    )
    async def get_webpage_video_options(
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
    ) -> WebpageVideoOptionsResponse:
        _require_url_capture_permission(context, "url_capture:read")
        return await webpage_video.options(context)

    @app.post(
        "/v1/webpage-video/runs",
        response_model=WebpageVideoRunResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Webpage Video"],
    )
    async def create_webpage_video_run(
        command: WebpageVideoRunCreate,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> WebpageVideoRunResponse:
        _require_url_capture_permission(context, "url_capture:write")
        resource, _created = await webpage_video.create_run(
            context, command, idempotency_key
        )
        response.headers["Location"] = f"/v1/webpage-video/runs/{resource.id}"
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_run(
            resource,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.get(
        "/v1/webpage-video/runs/{webpage_video_run_id}",
        response_model=WebpageVideoRunResponse,
        tags=["Webpage Video"],
    )
    async def get_webpage_video_run(
        webpage_video_run_id: UUID,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        request: Request,
        response: Response,
    ) -> WebpageVideoRunResponse:
        _require_url_capture_permission(context, "url_capture:read")
        resource = await webpage_video.get_run(context, webpage_video_run_id)
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_run(
            resource,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.get(
        "/v1/webpage-video/runs/{webpage_video_run_id}/capture",
        response_model=WebpageCaptureResponse,
        tags=["Webpage Video"],
    )
    async def get_webpage_video_capture(
        webpage_video_run_id: UUID,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        request: Request,
        response: Response,
    ) -> WebpageCaptureResponse:
        _require_url_capture_permission(context, "url_capture:read")
        capture = await webpage_video.get_capture(context, webpage_video_run_id)
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_capture(
            capture,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.post(
        "/v1/webpage-video/runs/{webpage_video_run_id}/capture/review",
        response_model=WebpageVideoRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Webpage Video"],
    )
    async def review_webpage_video_capture(
        webpage_video_run_id: UUID,
        command: WebpageCaptureReviewRequest,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> WebpageVideoRunResponse:
        _require_url_capture_permission(context, "url_capture:review")
        resource = await webpage_video.review_capture(
            context, webpage_video_run_id, command, idempotency_key
        )
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_run(
            resource,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.get(
        "/v1/webpage-video/runs/{webpage_video_run_id}/site",
        response_model=WebpageSiteResponse,
        tags=["Webpage Video"],
    )
    async def get_webpage_video_site(
        webpage_video_run_id: UUID,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        request: Request,
        response: Response,
    ) -> WebpageSiteResponse:
        _require_url_capture_permission(context, "url_capture:read")
        site = await webpage_video.get_site(context, webpage_video_run_id)
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_site(
            site,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.post(
        "/v1/webpage-video/runs/{webpage_video_run_id}/scope/review",
        response_model=WebpageSiteResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Webpage Video"],
    )
    async def review_webpage_video_scope(
        webpage_video_run_id: UUID,
        command: WebpageScopeReviewRequest,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> WebpageSiteResponse:
        _require_url_capture_permission(context, "url_capture:review")
        site = await webpage_video.review_site_manifest(
            context, webpage_video_run_id, "scope", command, idempotency_key
        )
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_site(
            site,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.post(
        "/v1/webpage-video/runs/{webpage_video_run_id}/storyboard/review",
        response_model=WebpageSiteResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Webpage Video"],
    )
    async def review_webpage_video_storyboard(
        webpage_video_run_id: UUID,
        command: WebpageStoryboardReviewRequest,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> WebpageSiteResponse:
        _require_url_capture_permission(context, "url_capture:review")
        site = await webpage_video.review_site_manifest(
            context, webpage_video_run_id, "storyboard", command, idempotency_key
        )
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_site(
            site,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.post(
        "/v1/webpage-video/runs/{webpage_video_run_id}/cancel",
        response_model=WebpageVideoRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Webpage Video"],
    )
    async def cancel_webpage_video_run(
        webpage_video_run_id: UUID,
        webpage_video: WebpageVideoServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> WebpageVideoRunResponse:
        _require_url_capture_permission(context, "url_capture:write")
        resource = await webpage_video.cancel_run(
            context, webpage_video_run_id, idempotency_key
        )
        response.headers["Cache-Control"] = "private, no-store"
        return await _sign_webpage_run(
            resource,
            context=context,
            repository=request.app.state.repository,
            storage=request.app.state.object_storage,
        )

    @app.get(
        "/v1/account/capabilities",
        response_model=AccountCapabilitiesResponse,
        tags=["Account"],
    )
    async def account_capabilities() -> AccountCapabilitiesResponse:
        return AccountCapabilitiesResponse()

    @app.get("/v1/account/profile", response_model=ProfileResponse, tags=["Account"])
    async def get_account_profile(
        account: AccountServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        resource = await account.repository.get_account_profile(
            context.workspace_id, context.user_id
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.put("/v1/account/profile", response_model=ProfileResponse, tags=["Account"])
    async def update_account_profile(
        command: ProfileUpdate,
        account: AccountServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        resource = await account.update_profile(
            context.workspace_id,
            context.user_id,
            command,
            _revision(if_match),
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.get(
        "/v1/account/creation-preferences",
        response_model=CreationPreferencesResponse,
        tags=["Account"],
    )
    async def get_creation_preferences(
        account: AccountServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        resource = await account.repository.get_creation_preferences(
            context.workspace_id, context.user_id
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.put(
        "/v1/account/creation-preferences",
        response_model=CreationPreferencesResponse,
        tags=["Account"],
    )
    async def update_creation_preferences(
        command: CreationPreferencesUpdate,
        account: AccountServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        resource = await account.update_creation_preferences(
            context.workspace_id,
            context.user_id,
            command,
            _revision(if_match),
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.get(
        "/v1/account/sessions",
        response_model=list[SessionResponse],
        tags=["Account"],
    )
    async def list_account_sessions(
        account: AccountServiceDependency,
        context: ContextDependency,
    ) -> list[Resource]:
        return await account.repository.list_account_sessions(
            context.workspace_id, context.user_id
        )

    @app.delete(
        "/v1/account/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["Account"],
    )
    async def revoke_account_session(
        session_id: UUID,
        account: AccountServiceDependency,
        context: ContextDependency,
    ) -> Response:
        await account.repository.revoke_account_session(
            context.workspace_id, context.user_id, session_id
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/account/api-keys",
        response_model=list[ApiKeyResponse],
        tags=["Account"],
    )
    async def list_api_keys(
        account: AccountServiceDependency,
        context: ContextDependency,
    ) -> list[Resource]:
        return await account.repository.list_api_keys(context.workspace_id, context.user_id)

    @app.post(
        "/v1/account/api-keys",
        response_model=ApiKeyCreatedResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Account"],
    )
    async def create_api_key(
        command: ApiKeyCreate,
        account: AccountServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> ApiKeyCreatedResponse:
        resource, secret = await account.create_api_key(
            context.workspace_id, context.user_id, command
        )
        response.headers["Location"] = f"/v1/account/api-keys/{resource['id']}"
        return ApiKeyCreatedResponse(key=ApiKeyResponse.model_validate(resource), api_key=secret)

    @app.delete(
        "/v1/account/api-keys/{key_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["Account"],
    )
    async def revoke_api_key(
        key_id: UUID,
        account: AccountServiceDependency,
        context: ContextDependency,
    ) -> Response:
        await account.repository.revoke_api_key(
            context.workspace_id, context.user_id, key_id
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/account/two-factor", status_code=501, tags=["Account"])
    async def configure_two_factor() -> None:
        raise ApiError(
            501,
            "CAPABILITY_UNAVAILABLE",
            "Two-factor authentication is not available in this release",
            details={"capability": "two_factor_authentication"},
        )

    @app.get("/v1/channels", response_model=ResourcePage, tags=["Channels"])
    async def list_channels(
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        search: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
        platform: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        channel_status: Annotated[
            Literal["active", "paused", "archived"] | None,
            Query(alias="status"),
        ] = None,
    ) -> ResourcePage:
        resources = await service.list_channels(
            context,
            search=search,
            platform=platform,
            status=channel_status,
        )
        return _page(resources, limit, cursor)

    @app.post(
        "/v1/channels",
        status_code=status.HTTP_201_CREATED,
        tags=["Channels"],
    )
    async def create_channel(
        command: ChannelWrite,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> Resource:
        resource, _created = await service.create_channel(
            context, command, idempotency_key
        )
        response.headers["ETag"] = _etag(resource)
        response.headers["Location"] = f"/v1/channels/{resource['id']}"
        return resource

    @app.get("/v1/channels/{channel_id}", tags=["Channels"])
    async def get_channel(
        channel_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        resource = await service.get_channel(context, channel_id)
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.put("/v1/channels/{channel_id}", tags=["Channels"])
    async def replace_channel(
        channel_id: UUID,
        command: ChannelWrite,
        service: ServiceDependency,
        context: ContextDependency,
        response: Response,
        if_match: OptionalIfMatchHeader = None,
    ) -> Resource:
        resource = await service.replace_channel(
            context, channel_id, command, _required_revision(if_match)
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.delete(
        "/v1/channels/{channel_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["Channels"],
    )
    async def archive_channel(
        channel_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: OptionalIfMatchHeader = None,
    ) -> Response:
        await service.archive_channel(context, channel_id, _required_revision(if_match))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/skills", response_model=ResourcePage, tags=["Skills"])
    async def list_skills(
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return _page(await service.list_skills(context), limit, cursor)

    @app.post("/v1/skills", status_code=status.HTTP_201_CREATED, tags=["Skills"])
    async def create_skill(
        command: SkillIdentityInput,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> Resource:
        resource, _created = await service.create_skill(context, command, idempotency_key)
        response.headers["ETag"] = _etag(resource)
        response.headers["Location"] = f"/v1/skills/{resource['id']}"
        return resource

    @app.get("/v1/skills/{skill_id}", tags=["Skills"])
    async def get_skill(
        skill_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        resource = await service.get_skill(context, skill_id)
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.put("/v1/skills/{skill_id}", tags=["Skills"])
    async def replace_skill(
        skill_id: UUID,
        command: SkillIdentityInput,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        resource = await service.replace_skill(context, skill_id, command, _revision(if_match))
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.delete(
        "/v1/skills/{skill_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["Skills"],
    )
    async def delete_skill(
        skill_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Response:
        await service.delete_skill(context, skill_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/skills/{skill_id}/fork",
        response_model=ForkSkillResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Skills"],
    )
    async def fork_skill(
        skill_id: UUID,
        command: ForkSkillRequest,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> ForkSkillResponse:
        (skill, version), _created = await service.fork_skill(
            context, skill_id, command, idempotency_key
        )
        response.headers["Location"] = f"/v1/skills/{skill['id']}"
        response.headers["ETag"] = _etag(skill)
        return ForkSkillResponse(skill=skill, version=version)

    @app.get("/v1/skill-versions", response_model=ResourcePage, tags=["Skills"])
    async def list_skill_versions(
        service: ServiceDependency,
        context: ContextDependency,
        skill_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        versions = await service.list_skill_versions(context, skill_id)
        return _page(versions, limit, cursor)

    @app.post("/v1/skill-versions", status_code=status.HTTP_201_CREATED, tags=["Skills"])
    async def create_skill_version(
        command: SkillVersionCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> Resource:
        resource, _created = await service.create_skill_version(
            context, command, idempotency_key
        )
        response.headers["ETag"] = _etag(resource)
        response.headers["Location"] = f"/v1/skill-versions/{resource['id']}"
        return resource

    @app.get("/v1/skill-versions/{skill_version_id}", tags=["Skills"])
    async def get_skill_version(
        skill_version_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        resource = await service.get_skill_version(context, skill_version_id)
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.patch("/v1/skill-versions/{skill_version_id}", tags=["Skills"])
    async def patch_skill_version(
        skill_version_id: UUID,
        command: SkillVersionPatch,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        resource = await service.patch_skill_version(
            context, skill_version_id, command, _revision(if_match)
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.delete(
        "/v1/skill-versions/{skill_version_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["Skills"],
    )
    async def delete_skill_version(
        skill_version_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
    ) -> Response:
        await service.delete_skill_version(context, skill_version_id, _revision(if_match))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/skill-versions/{skill_version_id}/validate",
        response_model=ValidationReport,
        tags=["Skills"],
    )
    async def validate_skill_version(
        skill_version_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        if_match: IfMatchHeader,
        response: Response,
    ) -> ValidationReport:
        report, _created = await service.validate_skill_version(
            context, skill_version_id, _revision(if_match), idempotency_key
        )
        response.headers["ETag"] = f'"{report.revision}"'
        return report

    @app.post("/v1/skill-versions/{skill_version_id}/publish", tags=["Skills"])
    async def publish_skill_version(
        skill_version_id: UUID,
        command: PublishSkillVersionRequest,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        resource, _created = await service.publish_skill_version(
            context,
            skill_version_id,
            _revision(if_match),
            command.release_notes,
            idempotency_key,
        )
        response.headers["ETag"] = _etag(resource)
        return resource

    @app.get("/v1/skill-test-executions", response_model=ResourcePage, tags=["Skills"])
    async def list_skill_test_executions(
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return _page(await service.list_skill_test_executions(context), limit, cursor)

    @app.post(
        "/v1/skill-test-executions",
        status_code=status.HTTP_201_CREATED,
        tags=["Skills"],
    )
    async def create_skill_test_execution(
        command: SkillTestExecutionCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> Resource:
        resource, _created = await service.create_skill_test_execution(
            context, command, idempotency_key
        )
        response.headers["Location"] = f"/v1/skill-test-executions/{resource['id']}"
        return resource

    @app.get("/v1/skill-test-executions/{execution_id}", tags=["Skills"])
    async def get_skill_test_execution(
        execution_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.get_skill_test_execution(context, execution_id)

    @app.get("/v1/runs", response_model=ResourcePage, tags=["Runs"])
    async def list_runs(
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return _page(await service.list_runs(context), limit, cursor)

    @app.get(
        "/v1/generation-batches",
        response_model=ResourcePage,
        tags=["Batches"],
    )
    async def list_generation_batches(
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return _page(await service.list_generation_batches(context), limit, cursor)

    @app.post(
        "/v1/generation-batches",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Batches"],
    )
    async def create_generation_batch(
        command: GenerationBatchCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
        request: Request,
    ) -> Resource:
        batch, runs, _created = await service.create_generation_batch(
            context, command, idempotency_key
        )
        queue: JobQueue | None = request.app.state.job_queue
        if queue is not None:
            semaphore = asyncio.Semaphore(32)

            async def dispatch(run: Resource) -> None:
                async with semaphore:
                    await queue.enqueue(
                        queue_name="runs",
                        payload={
                            "workspace_id": run["workspace_id"],
                            "run_id": run["id"],
                        },
                        deduplication_key=(
                            f"run:{run['workspace_id']}:{run['id']}"
                        ),
                        max_attempts=5,
                    )

            try:
                await asyncio.gather(*(dispatch(run) for run in runs))
            except QueueError as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "BATCH_DISPATCH_UNAVAILABLE",
                    "The batch was saved but one or more Runs could not be dispatched",
                    details={"batch_id": batch["id"]},
                ) from exc
        response.headers["Location"] = f"/v1/generation-batches/{batch['id']}"
        return batch

    @app.get("/v1/generation-batches/{batch_id}", tags=["Batches"])
    async def get_generation_batch(
        batch_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.get_generation_batch(context, batch_id)

    @app.post(
        "/v1/generation-batches/{batch_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Batches"],
    )
    async def cancel_generation_batch(
        batch_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
    ) -> Resource:
        return await service.cancel_generation_batch(
            context, batch_id, idempotency_key
        )

    @app.post(
        "/v1/generation-batches/{batch_id}/retry-failed",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Batches"],
    )
    async def retry_failed_generation_batch(
        batch_id: UUID,
        command: GenerationBatchRetryFailed,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> Resource:
        batch, runs, _created = await service.retry_failed_generation_batch(
            context, batch_id, command, idempotency_key
        )
        queue: JobQueue | None = request.app.state.job_queue
        if queue is not None:
            semaphore = asyncio.Semaphore(32)

            async def dispatch(run: Resource) -> None:
                async with semaphore:
                    await queue.enqueue(
                        queue_name="runs",
                        payload={
                            "workspace_id": run["workspace_id"],
                            "run_id": run["id"],
                        },
                        deduplication_key=(
                            f"run:{run['workspace_id']}:{run['id']}"
                        ),
                        max_attempts=5,
                    )

            try:
                await asyncio.gather(*(dispatch(run) for run in runs))
            except QueueError as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "BATCH_DISPATCH_UNAVAILABLE",
                    "The retry batch was saved but could not be dispatched",
                    details={"batch_id": batch["id"]},
                ) from exc
        response.headers["Location"] = f"/v1/generation-batches/{batch['id']}"
        return batch

    @app.get(
        "/v1/generation-batches/{batch_id}/items",
        response_model=BatchItemPage,
        tags=["Batches"],
    )
    async def list_generation_batch_items(
        batch_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        status_filter: Annotated[
            str | None,
            Query(
                alias="status",
                pattern=(
                    r"^(queued|running|awaiting_review|succeeded|failed|cancelled)$"
                ),
            ),
        ] = None,
        search: Annotated[str | None, Query(max_length=300)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> BatchItemPage:
        offset = _decode_cursor(cursor)
        values, total = await service.list_generation_batch_items(
            context,
            batch_id,
            status=status_filter,
            search=search,
            offset=offset,
            limit=limit,
        )
        next_offset = offset + len(values)
        has_more = next_offset < total
        return BatchItemPage(
            data=values,
            total_count=total,
            page={
                "limit": limit,
                "has_more": has_more,
                "next_cursor": _encode_cursor(next_offset) if has_more else None,
            },
        )

    @app.get(
        "/v1/asset-libraries",
        response_model=ResourcePage,
        tags=["Assets"],
    )
    async def list_asset_libraries(
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        _require_asset_permission(context, "assets:read")
        return _page(await service.list_asset_libraries(context), limit, cursor)

    @app.post(
        "/v1/asset-libraries",
        status_code=status.HTTP_201_CREATED,
        tags=["Assets"],
    )
    async def create_asset_library(
        command: AssetLibraryCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        resource, _created = await service.create_asset_library(
            context, command, idempotency_key
        )
        response.headers["Location"] = f"/v1/asset-libraries/{resource['id']}"
        return resource

    @app.get("/v1/asset-libraries/{library_id}", tags=["Assets"])
    async def get_asset_library(
        library_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        _require_asset_permission(context, "assets:read")
        return await service.get_asset_library(context, library_id)

    @app.post(
        "/v1/library-build-jobs",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Assets"],
    )
    async def create_library_build_job(
        command: LibraryBuildJobCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        job, _created = await service.create_library_build_job(
            context, command, idempotency_key
        )
        response.headers["Location"] = f"/v1/library-build-jobs/{job['id']}"
        response.headers["ETag"] = _etag(job)
        if job["status"] == "queued":
            queue: JobQueue | None = request.app.state.job_queue
            if queue is None:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "LIBRARY_BUILD_QUEUE_UNAVAILABLE",
                    "The library build job was recorded but the asynchronous queue is unavailable",
                    details={"job_id": job["id"]},
                )
            try:
                await queue.enqueue(
                    queue_name="library-build",
                    payload={
                        "workspace_id": job["workspace_id"],
                        "job_id": job["id"],
                        "library_id": job["library_id"],
                        "spec": job["spec"],
                    },
                    deduplication_key=(
                        f"library-build:{job['workspace_id']}:{job['id']}"
                    ),
                    # Analysis polling is expected work, not a provider retry.
                    # Keep enough leased deliveries for long-form media while
                    # the PostgreSQL job remains the durable source of truth.
                    max_attempts=2000,
                )
            except QueueError as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "LIBRARY_BUILD_QUEUE_UNAVAILABLE",
                    "The library build job was recorded but could not be enqueued",
                    details={"job_id": job["id"]},
                ) from exc
        return job

    @app.get("/v1/library-build-jobs/{job_id}", tags=["Assets"])
    async def get_library_build_job(
        job_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:read")
        job = await service.get_library_build_job(context, job_id)
        response.headers["ETag"] = _etag(job)
        return job

    @app.post(
        "/v1/library-build-jobs/{job_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Assets"],
    )
    async def cancel_library_build_job(
        job_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        job = await service.cancel_library_build_job(
            context, job_id, _revision(if_match)
        )
        response.headers["ETag"] = _etag(job)
        return job

    @app.get(
        "/v1/asset-libraries/{library_id}/assets",
        response_model=ResourcePage,
        tags=["Assets"],
    )
    async def list_library_assets(
        library_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
        kind: Annotated[list[str] | None, Query()] = None,
        copyright_status: Annotated[list[str] | None, Query()] = None,
        analysis_status: Annotated[list[str] | None, Query()] = None,
        tag: Annotated[list[str] | None, Query(max_length=100)] = None,
        q: Annotated[str | None, Query(max_length=300)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> ResourcePage:
        _require_asset_permission(context, "assets:read")
        filters = {
            "status": status_filter or [],
            "kind": kind or [],
            "copyright_status": copyright_status or [],
            "analysis_status": analysis_status or [],
            "tag": tag or [],
            "q": q or "",
        }
        allowed = {
            "status": {"processing", "quarantined", "ready", "disabled", "deleted"},
            "kind": {"image", "video", "audio", "document", "text", "other"},
            "copyright_status": {
                "unknown",
                "owned",
                "licensed",
                "public_domain",
                "restricted",
            },
            "analysis_status": {"pending", "running", "completed", "failed"},
        }
        for name, choices in allowed.items():
            invalid = sorted(set(filters[name]) - choices)
            if invalid:
                raise ValidationError(
                    f"unsupported {name} filter", path=name, values=invalid
                )
        values = await service.list_assets(
            context,
            library_id,
            statuses=tuple(filters["status"]),
            kinds=tuple(filters["kind"]),
            copyright_statuses=tuple(filters["copyright_status"]),
            analysis_statuses=tuple(filters["analysis_status"]),
            tags=tuple(filters["tag"]),
            search=q,
        )
        page = _asset_page(
            values,
            limit=limit,
            cursor=cursor,
            filter_fingerprint=_asset_filter_fingerprint(filters),
        )
        page.data = [_public_asset(item) for item in page.data]
        return page

    @app.get(
        "/v1/asset-libraries/{library_id}/import-jobs",
        response_model=ResourcePage,
        tags=["Assets"],
    )
    async def list_asset_import_jobs(
        library_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        _require_asset_permission(context, "assets:read")
        return _page(await service.list_asset_import_jobs(context, library_id), limit, cursor)

    @app.post("/v1/assets/batch-review", tags=["Assets"])
    async def batch_review_assets(
        command: AssetBatchReview,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
    ) -> Resource:
        _require_asset_permission(context, "assets:review")
        result, _created = await service.batch_review_assets(
            context, command, idempotency_key
        )
        return result

    @app.post("/v1/assets/batch-tags", tags=["Assets"])
    async def batch_tag_assets(
        command: AssetBatchTags,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        result, _created = await service.batch_tag_assets(
            context, command, idempotency_key
        )
        return result

    @app.post("/v1/assets/batch-reanalyze", status_code=202, tags=["Assets"])
    async def batch_reanalyze_assets(
        command: AssetBatchReanalysis,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        result, _created = await service.batch_reanalyze_assets(
            context, command, idempotency_key
        )
        queue: JobQueue | None = request.app.state.job_queue
        if queue is None and result["succeeded_count"]:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                "Reanalysis jobs were recorded but the asynchronous queue is unavailable",
                details={"job_ids": [item["job_id"] for item in result["succeeded"]]},
            )
        if queue is not None:
            for item in result["succeeded"]:
                try:
                    await queue.enqueue(
                        queue_name="asset-analysis",
                        payload={
                            "job_id": item["job_id"],
                            "asset_id": item["asset_id"],
                            "workspace_id": str(context.workspace_id),
                        },
                        deduplication_key=f"asset-analysis:{item['job_id']}",
                        max_attempts=3,
                    )
                except QueueError as exc:
                    raise ApiError(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                        "Reanalysis jobs were recorded but could not be enqueued",
                        details={"job_id": item["job_id"]},
                    ) from exc
        return result

    @app.get("/v1/assets/{asset_id}", tags=["Assets"])
    async def get_asset(
        asset_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:read")
        resource = await service.get_asset(context, asset_id)
        response.headers["ETag"] = _etag(resource)
        return _public_asset(resource)

    @app.patch("/v1/assets/{asset_id}", tags=["Assets"])
    async def update_asset_metadata(
        asset_id: UUID,
        command: AssetMetadataPatch,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        resource = await service.update_asset_metadata(
            context, asset_id, command, _revision(if_match)
        )
        response.headers["ETag"] = _etag(resource)
        return _public_asset(resource)

    @app.post("/v1/assets/{asset_id}/status-transitions", tags=["Assets"])
    async def transition_asset_status(
        asset_id: UUID,
        command: AssetStatusTransition,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:review")
        resource = await service.transition_asset_status(
            context, asset_id, command, _revision(if_match)
        )
        response.headers["ETag"] = _etag(resource)
        return _public_asset(resource)

    @app.delete("/v1/assets/{asset_id}", tags=["Assets"])
    async def soft_delete_asset(
        asset_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        resource = await service.delete_asset(context, asset_id, _revision(if_match))
        response.headers["ETag"] = _etag(resource)
        return _public_asset(resource)

    @app.post("/v1/assets/{asset_id}/restore", tags=["Assets"])
    async def restore_asset(
        asset_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        resource = await service.restore_asset(context, asset_id, _revision(if_match))
        response.headers["ETag"] = _etag(resource)
        return _public_asset(resource)

    async def related_asset_page(
        relation: str,
        asset_id: UUID,
        service: ControlService,
        context: WorkspaceContext,
        limit: int,
        cursor: str | None,
    ) -> ResourcePage:
        _require_asset_permission(context, "assets:read")
        return _page(
            await service.list_asset_related(context, asset_id, relation), limit, cursor
        )

    @app.get("/v1/assets/{asset_id}/segments", response_model=ResourcePage, tags=["Assets"])
    async def list_asset_segments(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page("segments", asset_id, service, context, limit, cursor)

    @app.get("/v1/assets/{asset_id}/analyses", response_model=ResourcePage, tags=["Assets"])
    async def list_asset_analyses(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page("analyses", asset_id, service, context, limit, cursor)

    @app.get("/v1/assets/{asset_id}/sources", response_model=ResourcePage, tags=["Assets"])
    async def list_asset_sources(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page("sources", asset_id, service, context, limit, cursor)

    @app.get(
        "/v1/assets/{asset_id}/rights-evidence",
        response_model=ResourcePage,
        tags=["Assets"],
    )
    async def list_asset_rights_evidence(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page(
            "rights_evidence", asset_id, service, context, limit, cursor
        )

    @app.get(
        "/v1/assets/{asset_id}/usage-records", response_model=ResourcePage, tags=["Assets"]
    )
    async def list_asset_usage_records(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page(
            "usage_records", asset_id, service, context, limit, cursor
        )

    @app.get(
        "/v1/assets/{asset_id}/analysis-jobs", response_model=ResourcePage, tags=["Assets"]
    )
    async def list_asset_analysis_jobs(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page(
            "analysis_jobs", asset_id, service, context, limit, cursor
        )

    @app.get(
        "/v1/assets/{asset_id}/audit-events", response_model=ResourcePage, tags=["Assets"]
    )
    async def list_asset_audit_events(
        asset_id: UUID, service: ServiceDependency, context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return await related_asset_page(
            "audit_events", asset_id, service, context, limit, cursor
        )

    @app.post("/v1/assets/{asset_id}/reanalyze", status_code=202, tags=["Assets"])
    async def reanalyze_asset(
        asset_id: UUID,
        command: AssetReanalysisRequest,
        service: ServiceDependency,
        context: ContextDependency,
        if_match: IfMatchHeader,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        job, _created = await service.request_asset_reanalysis(
            context, asset_id, command, _revision(if_match), idempotency_key
        )
        response.headers["Location"] = f"/v1/assets/{asset_id}/analysis-jobs"
        queue: JobQueue | None = request.app.state.job_queue
        if queue is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                "The reanalysis job was recorded but the asynchronous queue is unavailable",
                details={"job_id": job["id"]},
            )
        try:
            await _enqueue_asset_analysis(
                queue,
                job=job,
                asset_id=asset_id,
                workspace_id=context.workspace_id,
            )
        except QueueError as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                "The reanalysis job was recorded but could not be enqueued",
                details={"job_id": job["id"]},
            ) from exc
        return job

    @app.get("/v1/assets/{asset_id}/downloads/{variant}", tags=["Assets"])
    async def download_asset(
        asset_id: UUID,
        variant: str,
        service: ServiceDependency,
        context: ContextDependency,
        request: Request,
        expires_in: Annotated[int, Query(ge=60, le=1800)] = 900,
    ) -> Resource:
        _require_asset_permission(context, "assets:download")
        if variant not in {"poster", "preview", "original"}:
            raise ValidationError("variant must be poster, preview or original", path="variant")
        asset = await service.get_asset(context, asset_id)
        if asset["status"] == "deleted" or (
            variant == "original" and asset["status"] != "ready"
        ):
            raise ApiError(
                status.HTTP_403_FORBIDDEN,
                "ASSET_DOWNLOAD_FORBIDDEN",
                "The current asset state does not permit this download",
            )
        descriptor = asset["file"]
        resolved_variant = variant
        if variant == "poster":
            poster_descriptor = (asset.get("metadata") or {}).get("poster")
            if isinstance(poster_descriptor, dict):
                descriptor = poster_descriptor
            else:
                raise ApiError(404, "ASSET_POSTER_NOT_FOUND", "Asset poster is unavailable")
        elif variant == "preview":
            preview_descriptor = (asset.get("metadata") or {}).get("preview")
            if isinstance(preview_descriptor, dict):
                descriptor = preview_descriptor
            elif descriptor.get("scan_status") == "clean":
                resolved_variant = "original"
            else:
                raise ApiError(404, "ASSET_PREVIEW_NOT_FOUND", "Asset preview is unavailable")
        key = str(descriptor.get("object_key", ""))
        prefix = f"workspaces/{context.workspace_id}/"
        if not key.startswith(prefix) or "/../" in f"/{key}/":
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "ASSET_OBJECT_KEY_INVALID",
                "The persisted asset object is outside its workspace boundary",
            )
        locator = ObjectLocator(
            key=key,
            sha256=str(descriptor.get("content_hash") or descriptor.get("sha256") or ""),
            content_type=str(descriptor.get("media_type") or descriptor.get("content_type") or ""),
        )
        storage: ObjectStorage | None = request.app.state.object_storage
        if storage is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Asset storage is not configured",
            )
        try:
            signed = await storage.presign_download(
                context.workspace_id, locator, expires_in=expires_in
            )
        except ObjectNotFound as exc:
            raise ApiError(404, "ASSET_OBJECT_NOT_FOUND", "Asset object was not found") from exc
        except (ObjectIntegrityError, ObjectStorageUnavailable) as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Could not create a safe asset download",
            ) from exc
        return {
            "method": signed.method,
            "url": signed.url,
            "headers": dict(signed.headers),
            "expires_at": signed.expires_at.isoformat().replace("+00:00", "Z"),
            "variant": resolved_variant,
            "requested_variant": variant,
            "fallback": resolved_variant != variant,
            "media_type": locator.content_type,
            "byte_size": descriptor.get("byte_size"),
        }

    @app.post(
        "/v1/asset-uploads",
        status_code=status.HTTP_201_CREATED,
        tags=["Assets"],
    )
    async def create_asset_upload(
        command: AssetUploadCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        storage: ObjectStorage | None = request.app.state.object_storage
        if storage is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Asset storage is not configured",
            )
        try:
            upload = await storage.initiate_upload(
                context.workspace_id,
                sha256=command.sha256,
                content_type=command.content_type,
            )
        except ObjectStorageUnavailable as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Could not prepare the media upload",
            ) from exc
        bucket = getattr(getattr(storage, "settings", None), "bucket", "")
        if not bucket:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Asset storage does not expose a durable bucket",
            )
        resource, _created = await service.create_asset_upload(
            context,
            command,
            upload,
            bucket=bucket,
            idempotency_key=idempotency_key,
        )
        response.headers["Location"] = f"/v1/assets/{resource['id']}"
        return resource

    @app.post(
        "/v1/asset-uploads/{asset_id}/complete",
        tags=["Assets"],
    )
    async def complete_asset_upload(
        asset_id: UUID,
        command: AssetUploadComplete,
        service: ServiceDependency,
        context: ContextDependency,
        request: Request,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        storage: ObjectStorage | None = request.app.state.object_storage
        if storage is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Asset storage is not configured",
            )
        locator = ObjectLocator(
            key=command.object_key,
            sha256=command.sha256,
            content_type=command.content_type,
        )
        try:
            stored = await storage.complete_upload(context.workspace_id, locator)
        except ObjectNotFound as exc:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "ASSET_UPLOAD_NOT_FOUND",
                "Uploaded media could not be found",
            ) from exc
        except ObjectIntegrityError as exc:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "ASSET_UPLOAD_INTEGRITY_ERROR",
                "Uploaded media failed integrity validation",
            ) from exc
        except ObjectStorageUnavailable as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Could not verify the uploaded media",
            ) from exc
        completed = await service.complete_asset_upload(
            context, asset_id, command, stored
        )
        queue: JobQueue | None = request.app.state.job_queue
        if queue is None:
            # Upload completion remains valid when the asynchronous service is
            # intentionally disabled (for example contract-only tests). The
            # asset truthfully stays pending instead of pretending to analyse.
            return completed
        analysis_key = f"upload-analysis-{asset_id}-{command.sha256[:16]}"
        job, _created = await service.request_asset_reanalysis(
            context,
            asset_id,
            AssetReanalysisRequest(reason="automatic_upload_analysis"),
            int(completed["revision"]),
            analysis_key,
        )
        try:
            await _enqueue_asset_analysis(
                queue,
                job=job,
                asset_id=asset_id,
                workspace_id=context.workspace_id,
            )
        except QueueError as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                "The upload is stored, but its analysis job could not be enqueued",
                details={"asset_id": str(asset_id), "job_id": job["id"]},
            ) from exc
        return await service.get_asset(context, asset_id)

    @app.post(
        "/v1/asset-imports",
        status_code=status.HTTP_201_CREATED,
        tags=["Assets"],
    )
    async def import_remote_asset(
        command: RemoteAssetImportCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> Resource:
        _require_asset_permission(context, "assets:write")
        storage: ObjectStorage | None = request.app.state.object_storage
        gateway: RemoteAssetGateway | None = request.app.state.remote_asset_gateway
        if storage is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Asset storage is not configured",
            )
        if gateway is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "REMOTE_ASSET_PROVIDER_UNAVAILABLE",
                "Remote asset acquisition is not configured",
            )
        bucket = getattr(getattr(storage, "settings", None), "bucket", "")
        if not bucket:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Asset storage does not expose a durable bucket",
            )

        with tempfile.TemporaryDirectory(prefix="framefactory-remote-asset-") as directory:
            try:
                downloaded = await gateway.download(command.source_url, Path(directory))
            except RemoteAssetError as exc:
                invalid = {
                    "REMOTE_SOURCE_INVALID",
                    "REMOTE_SOURCE_UNSUPPORTED",
                    "REMOTE_URL_UNSAFE",
                    "REMOTE_ASSET_TOO_LARGE",
                    "REMOTE_ASSET_TOO_LONG",
                    "REMOTE_MEDIA_UNSUPPORTED",
                    "REMOTE_RIGHTS_UNVERIFIED",
                    "REDNOTE_RESPONSE_INVALID",
                    "REDNOTE_VIDEO_NOT_FOUND",
                }
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT
                    if exc.code in invalid
                    else status.HTTP_503_SERVICE_UNAVAILABLE,
                    exc.code,
                    str(exc),
                    details={"retryable": exc.retryable},
                ) from exc

            source = {
                "platform": downloaded.platform,
                "source_url": downloaded.source_url,
                "canonical_url": downloaded.canonical_url,
                "external_id": downloaded.external_id,
                "author": downloaded.author,
                "license": downloaded.license_name,
                "duration_seconds": downloaded.duration_seconds,
                "subtitle_languages": ",".join(downloaded.subtitle_languages),
                "subtitle_capture": (
                    "embedded_from_provider"
                    if downloaded.subtitle_languages
                    else "audio_analysis_fallback"
                ),
                "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "rights_confirmed": True,
            }
            verified_rights_evidence: dict[str, Any] | None = None
            if (
                downloaded.platform == "wikimedia"
                and command.copyright_status == "public_domain"
                and downloaded.rights_evidence_type == "verified_public_domain"
                and str(downloaded.rights_evidence_locator or "").startswith(
                    "https://commons.wikimedia.org/"
                )
                and downloaded.rights_verified_at
                and downloaded.license_name
            ):
                verified_rights_evidence = {
                    "source_type": "website",
                    "locator": downloaded.rights_evidence_locator,
                    "provider": "wikimedia",
                    "attribution": downloaded.author or "Wikimedia Commons",
                    "license": downloaded.license_name,
                    "evidence_type": "verified_public_domain",
                    "verified_at": downloaded.rights_verified_at,
                    "verification_method": "commons_api_extmetadata",
                }
                source.update(
                    {
                        "rights_evidence_type": "verified_public_domain",
                        "rights_evidence_locator": downloaded.rights_evidence_locator,
                        "rights_verified_at": downloaded.rights_verified_at,
                    }
                )
            tags = list(
                dict.fromkeys(
                    [
                        *command.tags,
                        downloaded.platform,
                    ]
                )
            )
            upload_command = AssetUploadCreate(
                library_id=command.library_id,
                filename=downloaded.filename,
                title=command.title or downloaded.title,
                description=command.description or downloaded.description or downloaded.title,
                kind="video",
                content_type=downloaded.media_type,
                byte_size=downloaded.byte_size,
                sha256=downloaded.sha256,
                copyright_status=command.copyright_status,
                tags=tags[:64],
                source=source,
            )
            try:
                upload = await storage.initiate_upload(
                    context.workspace_id,
                    sha256=downloaded.sha256,
                    content_type=downloaded.media_type,
                )
                resource, created = await service.create_asset_upload(
                    context,
                    upload_command,
                    upload,
                    bucket=bucket,
                    idempotency_key=idempotency_key,
                    idempotency_payload=command.model_dump(mode="json"),
                    verified_rights_evidence=verified_rights_evidence,
                )
                if not created:
                    current = await service.get_asset(
                        context, UUID(str(resource["id"]))
                    )
                    if (current.get("metadata") or {}).get("upload_completed_at"):
                        response.headers["Location"] = f"/v1/assets/{current['id']}"
                        return current
                    file = current["file"]
                    target = ObjectLocator(
                        key=str(file["object_key"]),
                        sha256=str(file["content_hash"]),
                        content_type=str(file["media_type"]),
                    )
                    resource = current
                else:
                    target = upload.object
                await storage.upload_file(context.workspace_id, target, downloaded.path)
                stored = await storage.complete_upload(context.workspace_id, target)
                completed = await service.complete_asset_upload(
                    context,
                    UUID(str(resource["id"])),
                    AssetUploadComplete(
                        object_key=target.key,
                        sha256=target.sha256,
                        content_type=target.content_type,
                    ),
                    stored,
                )
            except (ObjectStorageUnavailable, ObjectNotFound) as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "REMOTE_ASSET_STORAGE_FAILED",
                    "Downloaded media could not be saved to object storage",
                ) from exc
            except ObjectIntegrityError as exc:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    "REMOTE_ASSET_INTEGRITY_ERROR",
                    "Downloaded media failed object integrity validation",
                ) from exc
        response.headers["Location"] = f"/v1/assets/{completed['id']}"
        return completed

    @app.post(
        "/v1/asset-acquisitions",
        status_code=status.HTTP_201_CREATED,
        tags=["Assets"],
    )
    async def acquire_assets_for_topics(
        command: AssetAcquisitionCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _require_asset_permission(context, "assets:write")
        gateway: RemoteAssetGateway | None = request.app.state.remote_asset_gateway
        if gateway is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "REMOTE_ASSET_PROVIDER_UNAVAILABLE",
                "Remote asset acquisition is not configured",
            )
        queue: JobQueue | None = request.app.state.job_queue
        if queue is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                "Remote assets cannot be acquired while automatic analysis is unavailable",
            )
        await service.get_asset_library(context, command.library_id)
        imported: list[Resource] = []
        analysis_jobs: list[Resource] = []
        analysis_dispatches: list[tuple[Resource, Resource]] = []
        unresolved: list[str] = []
        provider_errors: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for query in (value.strip() for value in command.queries):
            if len(imported) >= command.max_assets:
                break
            query_imported = False
            for platform in command.sources:
                if len(imported) >= command.max_assets:
                    break
                try:
                    # Search more candidates than the requested import count.
                    # A platform may expose a valid result whose individual
                    # download is unavailable; that must not abort all fallback.
                    candidates = await gateway.search(
                        platform,
                        query,
                        limit=min(10, max(3, (command.max_assets - len(imported)) * 3)),
                    )
                except RemoteAssetError as exc:
                    provider_errors.append(
                        {"platform": platform, "code": exc.code, "retryable": exc.retryable}
                    )
                    continue
                for candidate in candidates:
                    if candidate.source_url in seen_urls:
                        continue
                    seen_urls.add(candidate.source_url)
                    child_key = "auto-asset-" + hashlib.sha256(
                        f"{idempotency_key}:{candidate.source_url}".encode()
                    ).hexdigest()[:32]
                    try:
                        asset = await import_remote_asset(
                            RemoteAssetImportCreate(
                                library_id=command.library_id,
                                source_url=candidate.source_url,
                                title=candidate.title,
                                description=query,
                                copyright_status=(
                                    "public_domain"
                                    if platform == "wikimedia"
                                    else command.copyright_status
                                ),
                                # The provider query is acquisition provenance, not
                                # visual evidence.  Persisting it as a normal asset
                                # tag would let catalog label scoring prove its own
                                # query and could auto-select unrelated footage.
                                tags=[platform, "auto-acquired"],
                                rights_confirmed=True,
                            ),
                            service,
                            context,
                            child_key,
                            request,
                            response,
                        )
                    except ApiError as exc:
                        if exc.code == "ASSET_CONTENT_EXISTS" and exc.details.get("asset_id"):
                            existing = await service.get_asset(
                                context, UUID(str(exc.details["asset_id"]))
                            )
                            if str(existing.get("library_id")) != str(command.library_id):
                                # Content hashes are workspace-global, while an
                                # asset belongs to exactly one library. A hit in
                                # another library does not cover this Run's
                                # selected library and must not consume its
                                # acquisition budget. Continue with the next
                                # remote candidate instead.
                                provider_errors.append(
                                    {
                                        "platform": platform,
                                        "code": "REMOTE_CANDIDATE_ALREADY_IN_OTHER_LIBRARY",
                                        "retryable": False,
                                        "asset_id": str(existing["id"]),
                                    }
                                )
                                continue
                            if not (
                                existing.get("status") == "ready"
                                and existing.get("analysis_status") == "completed"
                            ):
                                tags = existing.get("tags") or (
                                    existing.get("metadata") or {}
                                ).get("tags", [])
                                if "auto-acquired" in tags and existing.get("status") != "deleted":
                                    # Retire a previously analysed, unusable
                                    # automatic candidate through the audited,
                                    # recoverable soft-delete path. The same URL
                                    # will then be skipped and the next search
                                    # candidate can be tried.
                                    await service.delete_asset(
                                        context,
                                        UUID(str(existing["id"])),
                                        int(existing["revision"]),
                                    )
                                provider_errors.append(
                                    {
                                        "platform": platform,
                                        "code": "REMOTE_CANDIDATE_REJECTED",
                                        "retryable": False,
                                        "asset_id": str(existing["id"]),
                                    }
                                )
                                continue
                            provider_errors.append(
                                {
                                    "platform": platform,
                                    "code": "REMOTE_CANDIDATE_ALREADY_AVAILABLE",
                                    "retryable": False,
                                    "asset_id": str(existing["id"]),
                                }
                            )
                            continue
                        else:
                            provider_errors.append(
                                {
                                    "platform": platform,
                                    "code": exc.code,
                                    "retryable": exc.status_code >= 500,
                                }
                            )
                            continue
                    if (
                        asset.get("status") == "ready"
                        and asset.get("analysis_status") == "completed"
                    ):
                        # A replayed child import can return a candidate that is
                        # already searchable in this library. It added no new
                        # coverage and has no analysis job that could wake a
                        # parked Run, so keep scanning remote candidates.
                        provider_errors.append(
                            {
                                "platform": platform,
                                "code": "REMOTE_CANDIDATE_ALREADY_AVAILABLE",
                                "retryable": False,
                                "asset_id": str(asset["id"]),
                            }
                        )
                        continue
                    if (
                        asset.get("analysis_status") == "completed"
                        and asset.get("status") != "ready"
                    ):
                        # An idempotent child import may replay a candidate that
                        # has since finished analysis and failed its automatic
                        # quality gate. Do not enqueue the same analysis key with
                        # a newer revision (which is correctly rejected as an
                        # idempotency conflict); retire it and continue searching.
                        tags = asset.get("tags") or (asset.get("metadata") or {}).get(
                            "tags", []
                        )
                        if "auto-acquired" in tags and asset.get("status") != "deleted":
                            await service.delete_asset(
                                context,
                                UUID(str(asset["id"])),
                                int(asset["revision"]),
                            )
                        provider_errors.append(
                            {
                                "platform": platform,
                                "code": "REMOTE_CANDIDATE_REJECTED",
                                "retryable": False,
                                "asset_id": str(asset["id"]),
                            }
                        )
                        continue
                    imported.append(asset)
                    if not (
                        asset.get("status") == "ready"
                        and asset.get("analysis_status") == "completed"
                    ):
                        analysis_key = "auto-analysis-" + hashlib.sha256(
                            f"{idempotency_key}:{asset['id']}".encode()
                        ).hexdigest()[:32]
                        job, _created = await service.request_asset_reanalysis(
                            context,
                            UUID(str(asset["id"])),
                            AssetReanalysisRequest(
                                reason=(
                                    "Automatically analyze and tag remotely acquired media"
                                )
                            ),
                            int(asset["revision"]),
                            analysis_key,
                        )
                        analysis_jobs.append(job)
                        analysis_dispatches.append((job, asset))
                    query_imported = True
                    # One candidate per story query per round. This prevents a
                    # broad first query (for example "training") from consuming
                    # the full budget before championship, Olympic or location
                    # beats have each had a chance to acquire footage.
                    break
                if query_imported:
                    break
            if not query_imported:
                unresolved.append(query)

        if not imported and provider_errors and any(
            bool(item["retryable"]) for item in provider_errors
        ):
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "REMOTE_ACQUISITION_UNAVAILABLE",
                "All configured remote acquisition providers were unavailable",
                details={"providers": provider_errors},
            )
        for job, asset in analysis_dispatches:
            try:
                await queue.enqueue(
                    queue_name="asset-analysis",
                    payload={
                        "job_id": job["id"],
                        "asset_id": asset["id"],
                        "workspace_id": str(context.workspace_id),
                        "run_id": str(command.run_id) if command.run_id else None,
                        "step_id": command.step_id,
                        "acquisition_id": idempotency_key,
                        "acquisition_count": len(analysis_dispatches),
                    },
                    deduplication_key=f"asset-analysis:{job['id']}",
                    max_attempts=3,
                )
            except QueueError as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "ASSET_ANALYSIS_QUEUE_UNAVAILABLE",
                    "Remote media was saved but its analysis job could not be enqueued",
                    details={"asset_id": asset["id"], "job_id": job["id"]},
                ) from exc
        return {
            "imported_assets": imported,
            "imported_count": len(imported),
            "analysis_jobs": analysis_jobs,
            "unresolved_queries": unresolved,
            "provider_errors": provider_errors,
            "run_id": str(command.run_id) if command.run_id else None,
            "step_id": command.step_id,
        }

    @app.post("/v1/runs/estimate", tags=["Runs"])
    async def estimate_run(
        command: RunCreate,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.estimate_run(context, command)

    @app.post("/v1/runs", status_code=status.HTTP_201_CREATED, tags=["Runs"])
    async def create_run(
        command: RunCreate,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        response: Response,
        request: Request,
    ) -> Resource:
        resource, _created = await service.create_run(context, command, idempotency_key)
        queue: JobQueue | None = request.app.state.job_queue
        if queue is not None:
            try:
                await queue.enqueue(
                    queue_name="runs",
                    payload={
                        "workspace_id": resource["workspace_id"],
                        "run_id": resource["id"],
                    },
                    deduplication_key=(
                        f"run:{resource['workspace_id']}:{resource['id']}"
                    ),
                    max_attempts=5,
                )
            except QueueError as exc:
                # The Run is already durable. Replaying the same idempotent
                # request will retry this publish, while the worker's database
                # recovery scan remains the final delivery safety net.
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "RUN_DISPATCH_UNAVAILABLE",
                    "The Run was saved but the execution queue is temporarily unavailable",
                    details={"run_id": resource["id"]},
                ) from exc
        response.headers["Location"] = f"/v1/runs/{resource['id']}"
        return resource

    @app.get("/v1/runs/{run_id}", tags=["Runs"])
    async def get_run(
        run_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.get_run(context, run_id)

    @app.post(
        "/v1/runs/{run_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Runs"],
    )
    async def cancel_run(
        run_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
    ) -> Resource:
        resource, _created = await service.cancel_run(
            context, run_id, idempotency_key
        )
        return resource

    @app.get("/v1/steps", response_model=ResourcePage, tags=["Steps"])
    async def list_run_steps(
        run_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return _page(await service.list_run_steps(context, run_id), limit, cursor)

    @app.get("/v1/steps/{step_id}", tags=["Steps"])
    async def get_run_step(
        step_id: str,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.get_run_step(context, step_id)

    @app.post(
        "/v1/steps/{step_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Steps"],
    )
    async def retry_run_step(
        step_id: str,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
        request: Request,
    ) -> Resource:
        resource, _created = await service.retry_run_step(
            context, step_id, idempotency_key
        )
        queue: JobQueue | None = request.app.state.job_queue
        if queue is not None:
            try:
                await queue.enqueue(
                    queue_name=resource["queue_name"],
                    payload={
                        "workspace_id": resource["workspace_id"],
                        "run_id": resource["run_id"],
                        "step_id": resource["id"],
                    },
                    deduplication_key=(
                        f"{resource['workspace_id']}:{resource['id']}:"
                        f"{int(resource['attempt']) + 1}"
                    ),
                    max_attempts=100,
                )
            except QueueError as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "STEP_DISPATCH_UNAVAILABLE",
                    "The retry was saved but the execution queue is temporarily unavailable",
                    details={"step_id": resource["id"]},
                ) from exc
        return resource

    @app.post(
        "/v1/steps/{step_id}/review",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Steps"],
    )
    async def review_run_step(
        step_id: str,
        command: StepReviewRequest,
        service: ServiceDependency,
        context: ContextDependency,
        idempotency_key: IdempotencyHeader,
    ) -> Resource:
        resource, _created = await service.review_run_step(
            context, step_id, command, idempotency_key
        )
        return resource

    @app.get("/v1/artifacts", response_model=ResourcePage, tags=["Artifacts"])
    async def list_artifacts(
        run_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ResourcePage:
        return _page(await service.list_artifacts(context, run_id), limit, cursor)

    @app.get("/v1/artifacts/{artifact_id}", tags=["Artifacts"])
    async def get_artifact(
        artifact_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.get_artifact(context, artifact_id)

    @app.get(
        "/v1/artifacts/{artifact_id}/content",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        tags=["Artifacts"],
    )
    async def get_artifact_content(
        artifact_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        request: Request,
        inline: Annotated[bool, Query()] = False,
    ):
        artifact = await service.get_artifact(context, artifact_id)
        storage: ObjectStorage | None = request.app.state.object_storage
        if storage is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Artifact content storage is not configured",
            )
        locator = ObjectLocator(
            key=str(artifact["object_key"]),
            sha256=str(artifact["content_hash"]),
            content_type=str(artifact["media_type"]),
        )
        if inline:
            media_type = str(artifact["media_type"])
            if media_type != "application/json" and not media_type.startswith("text/"):
                raise ApiError(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "ARTIFACT_PREVIEW_UNSUPPORTED",
                    "Only JSON and text artifacts can be read inline",
                )
            if int(artifact["byte_size"]) > 2_000_000:
                raise ApiError(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "ARTIFACT_PREVIEW_TOO_LARGE",
                    "Artifact is too large for inline preview",
                )
            try:
                content = await storage.read_bytes(
                    context.workspace_id,
                    locator,
                    maximum_bytes=2_000_000,
                )
            except ObjectNotFound as exc:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND,
                    "ARTIFACT_CONTENT_NOT_FOUND",
                    "Artifact content is unavailable",
                ) from exc
            except ObjectIntegrityError as exc:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    "ARTIFACT_INTEGRITY_ERROR",
                    "Artifact content failed integrity validation",
                ) from exc
            except ObjectStorageUnavailable as exc:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "OBJECT_STORAGE_UNAVAILABLE",
                    "Artifact content storage is temporarily unavailable",
                ) from exc
            return Response(
                content=content,
                media_type=media_type,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": f'inline; filename="{artifact["filename"]}"',
                    "X-Content-SHA256": str(artifact["content_hash"]),
                },
            )
        try:
            signed = await storage.presign_download(context.workspace_id, locator)
        except ObjectNotFound as exc:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "ARTIFACT_CONTENT_NOT_FOUND",
                "Artifact content is unavailable",
            ) from exc
        except ObjectIntegrityError as exc:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "ARTIFACT_INTEGRITY_ERROR",
                "Artifact content failed integrity validation",
            ) from exc
        except ObjectStorageUnavailable as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OBJECT_STORAGE_UNAVAILABLE",
                "Artifact content storage is temporarily unavailable",
            ) from exc
        return RedirectResponse(
            signed.url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/v1/events", response_model=ResourcePage, tags=["Events"])
    async def list_events(
        run_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> ResourcePage:
        return _page(
            await service.list_events(
                context, run_id, after_sequence=after_sequence, limit=limit + 1
            ),
            limit,
            None,
        )

    @app.get("/v1/events/{event_id}", tags=["Events"])
    async def get_event(
        event_id: UUID,
        service: ServiceDependency,
        context: ContextDependency,
    ) -> Resource:
        return await service.get_event(context, event_id)

    _register_reserved_routes(app)
    return app


def _register_reserved_routes(app: FastAPI) -> None:
    """Keep future single-installation resources explicit without enabling them."""

    reserved: dict[str, tuple[str, ...]] = {
        "/v1/users": ("GET",),
        "/v1/users/{user_id}": ("GET",),
        "/v1/workspaces": ("GET", "POST"),
        "/v1/workspaces/{workspace_id}": ("GET",),
        "/v1/render-presets": ("GET", "POST"),
        "/v1/render-presets/{render_preset_id}": ("GET",),
        "/v1/pipelines": ("GET", "POST"),
        "/v1/pipelines/{pipeline_id}": ("GET",),
    }

    def unavailable_endpoint(feature_name: str):
        async def unavailable() -> None:
            raise ApiError(
                501,
                "CAPABILITY_UNAVAILABLE",
                f"{feature_name} management is reserved but not enabled in single-workspace mode",
                details={"capability": feature_name, "mode": "single_workspace"},
            )

        return unavailable

    for path, methods in reserved.items():
        feature = path.split("/")[2]
        for method in methods:
            operation = f"reserved_{method.lower()}_{feature.replace('-', '_')}_{len(app.routes)}"
            endpoint = unavailable_endpoint(feature)
            endpoint.__name__ = operation
            app.add_api_route(
                path,
                endpoint,
                methods=[method],
                operation_id=operation,
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                tags=["Reserved"],
            )


async def _create_repository(
    settings: Settings, validator: ContractValidator
) -> ControlRepository:
    if settings.repository_backend == "memory":
        official_skills, official_versions = load_official_catalog(
            validator,
            settings.official_seed_manifest,
        )
        return InMemoryControlRepository(
            skills=official_skills,
            skill_versions=official_versions,
            pipelines=[seed.version for seed in official_versions.pipelines],
        )

    from .postgres_repository import PostgreSQLControlRepository

    assert settings.database_url is not None
    official_skills, official_versions = load_official_catalog(
        validator,
        settings.official_seed_manifest,
    )
    repository = await PostgreSQLControlRepository.connect(
        settings.database_url,
        min_size=settings.postgres_pool_min_size,
        max_size=settings.postgres_pool_max_size,
        default_user_id=settings.default_user_id,
        default_workspace_id=settings.default_workspace_id,
        default_workspace_name=settings.default_workspace_name,
        default_user_email=settings.default_user_email,
        default_user_display_name=settings.default_user_display_name,
        official_skills=official_skills,
        official_skill_versions=official_versions,
    )
    try:
        await repository.bootstrap()
    except Exception:
        await repository.close()
        raise
    return repository


def _create_redis_queue() -> JobQueue:
    from .redis_config import RedisQueueSettings
    from .redis_queue import RedisJobQueue

    return RedisJobQueue.from_settings(RedisQueueSettings.from_environment())


async def _create_object_storage(settings: Settings) -> ObjectStorage:
    from .s3_storage import S3ObjectStorage, S3StorageSettings

    storage = S3ObjectStorage(S3StorageSettings.from_environment())
    await storage.ensure_bucket(create_if_missing=settings.s3_create_bucket)
    return storage


async def _healthcheck(resource: Any | None) -> None:
    if resource is None:
        return
    healthcheck = getattr(resource, "healthcheck", None)
    if healthcheck is None:
        return
    result = healthcheck()
    if inspect.isawaitable(result):
        await result


async def _close(resource: Any | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    supplied = request.headers.get("X-Request-Id")
    try:
        return str(UUID(supplied)) if supplied else str(uuid4())
    except ValueError:
        return str(uuid4())


app = create_app()
