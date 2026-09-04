"""Vendor-neutral OpenAI-compatible structured-output adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.skills.canonical import thaw_json
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact
from framefactory.worker.retrieval.beats import normalize_beats


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float
    maximum_response_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class ResearchSearchGateway(Protocol):
    """Explicit web-search boundary; a text model is never treated as search."""

    def search(
        self,
        *,
        query: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]] | Awaitable[Sequence[Mapping[str, Any]]]: ...


class HttpsJsonResearchSearchGateway:
    """Bounded vendor-neutral search adapter; it never synthesizes source URLs."""

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        timeout_seconds: float,
        maximum_response_bytes: int = 1_048_576,
        transport: HttpTransport | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "research search endpoint must be a credential-free HTTPS URL"
            )
        if not bearer_token:
            raise ValueError("research search bearer token must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("research search timeout must be positive")
        if not 1_024 <= maximum_response_bytes <= 4_194_304:
            raise ValueError("research search response limit is outside the safe range")
        self._url = url
        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._transport = transport or UrllibTransport()

    async def search(
        self,
        *,
        query: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        normalized_query = query.strip()
        if not normalized_query:
            raise PermanentStepError("research search query must not be empty")
        if len(normalized_query) > 2_000:
            raise PermanentStepError("research search query exceeded 2000 characters")
        if not 1 <= limit <= 10:
            raise PermanentStepError("research search limit must be between 1 and 10")
        request = HttpRequest(
            url=self._url,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(
                {"query": normalized_query, "limit": limit},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            timeout_seconds=self._timeout_seconds,
            maximum_response_bytes=self._maximum_response_bytes,
        )
        response = await asyncio.to_thread(self._transport.send, request)
        if response.status in {408, 409, 425, 429} or response.status >= 500:
            raise RetryableStepError(
                f"research search endpoint returned retryable HTTP {response.status}"
            )
        if response.status < 200 or response.status >= 300:
            raise PermanentStepError(
                f"research search endpoint rejected the request with HTTP {response.status}"
            )
        if len(response.body) > self._maximum_response_bytes:
            raise PermanentStepError(
                "research search response exceeded its configured limit"
            )
        try:
            envelope = json.loads(response.body)
            results = envelope["results"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentStepError(
                "research search endpoint returned an invalid JSON response"
            ) from exc
        if not isinstance(results, list) or any(
            not isinstance(item, Mapping) for item in results
        ):
            raise PermanentStepError(
                "research search endpoint results must be an array of objects"
            )
        return tuple(results[:limit])


class DashscopeResearchSearchGateway:
    """DashScope Generation search adapter with explicit, attributable sources."""

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        model: str,
        timeout_seconds: float,
        maximum_response_bytes: int = 1_048_576,
        transport: HttpTransport | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "DashScope research endpoint must be a credential-free HTTPS URL"
            )
        if not bearer_token:
            raise ValueError("DashScope research bearer token must not be empty")
        if not model or len(model) > 200:
            raise ValueError(
                "DashScope research model must contain 1 to 200 characters"
            )
        if timeout_seconds <= 0:
            raise ValueError("DashScope research timeout must be positive")
        if not 1_024 <= maximum_response_bytes <= 4_194_304:
            raise ValueError(
                "DashScope research response limit is outside the safe range"
            )
        self._url = url
        self._bearer_token = bearer_token
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._transport = transport or UrllibTransport()

    async def search(
        self,
        *,
        query: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        normalized_query = query.strip()
        if not normalized_query:
            raise PermanentStepError("research search query must not be empty")
        if len(normalized_query) > 2_000:
            raise PermanentStepError("research search query exceeded 2000 characters")
        if not 1 <= limit <= 10:
            raise PermanentStepError("research search limit must be between 1 and 10")
        body = {
            "model": self._model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": normalized_query,
                    }
                ]
            },
            "parameters": {
                "result_format": "message",
                "enable_search": True,
                "search_options": {
                    "forced_search": True,
                    "enable_source": True,
                    "search_strategy": "turbo",
                },
            },
        }
        request = HttpRequest(
            url=self._url,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            timeout_seconds=self._timeout_seconds,
            maximum_response_bytes=self._maximum_response_bytes,
        )
        response = await asyncio.to_thread(self._transport.send, request)
        if response.status in {408, 409, 425, 429} or response.status >= 500:
            raise RetryableStepError(
                f"DashScope research search returned retryable HTTP {response.status}"
            )
        if response.status < 200 or response.status >= 300:
            raise PermanentStepError(
                f"DashScope research search rejected the request with HTTP {response.status}"
            )
        if len(response.body) > self._maximum_response_bytes:
            raise PermanentStepError(
                "DashScope research response exceeded its configured limit"
            )
        try:
            envelope = json.loads(response.body)
            search_info = envelope["output"]["search_info"]
            raw_results = search_info["search_results"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentStepError(
                "DashScope research search returned an invalid JSON response"
            ) from exc
        if not isinstance(raw_results, list):
            raise PermanentStepError(
                "DashScope research search results must be an array"
            )
        results: list[dict[str, str]] = []
        for item in raw_results:
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            site_name = str(item.get("site_name") or "").strip()
            if _https_url_key(url) is None or not title:
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": site_name or title,
                }
            )
            if len(results) == limit:
                break
        if not results:
            raise PermanentStepError(
                "DashScope research search returned no usable sources"
            )
        return tuple(results)


class UrllibTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        raw = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method="POST",
        )
        read_limit = (
            request.maximum_response_bytes + 1
            if request.maximum_response_bytes is not None
            else -1
        )
        try:
            with urllib.request.urlopen(
                raw, timeout=request.timeout_seconds
            ) as response:
                return HttpResponse(
                    status=response.status, body=response.read(read_limit)
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read(read_limit))
        except (TimeoutError, urllib.error.URLError) as exc:
            raise RetryableStepError(
                "configured model provider is temporarily unreachable"
            ) from exc


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: HttpTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibTransport()

    async def structured(
        self,
        *,
        operation: str,
        system: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        body = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": operation.replace(".", "_"),
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        request = HttpRequest(
            url=self._url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout_seconds=self._timeout_seconds,
            maximum_response_bytes=4 * 1024 * 1024,
        )
        response = await asyncio.to_thread(self._transport.send, request)
        if response.status in {400, 422}:
            # Some OpenAI-compatible vendors implement JSON mode but not the
            # newer strict json_schema request shape. Keep the same local
            # schema validation and make one explicit compatibility retry.
            fallback_body = {
                **body,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"{system}\nReturn one JSON object that conforms exactly to this schema: "
                            + json.dumps(
                                schema, ensure_ascii=False, separators=(",", ":")
                            )
                        ),
                    },
                    body["messages"][1],
                ],
                "response_format": {"type": "json_object"},
            }
            fallback_request = HttpRequest(
                url=self._url,
                headers=request.headers,
                body=json.dumps(fallback_body, ensure_ascii=False).encode("utf-8"),
                timeout_seconds=self._timeout_seconds,
                maximum_response_bytes=4 * 1024 * 1024,
            )
            response = await asyncio.to_thread(self._transport.send, fallback_request)
        if response.status in {408, 409, 425, 429} or response.status >= 500:
            raise RetryableStepError(
                f"configured model provider returned retryable HTTP {response.status}"
            )
        if response.status < 200 or response.status >= 300:
            raise PermanentStepError(
                f"configured model provider rejected the request with HTTP {response.status}"
            )
        if len(response.body) > 4 * 1024 * 1024:
            raise PermanentStepError(
                "configured model provider response exceeded 4 MiB"
            )
        content: object = None
        finish_reason: object = None
        try:
            envelope = json.loads(response.body)
            choice = envelope["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            result = _decode_structured_content(content)
        except (
            IndexError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise PermanentStepError(
                "configured model provider returned an invalid structured response",
                code="model_structured_response_invalid",
                details={
                    "content_type": type(content).__name__,
                    "content_length": len(content)
                    if isinstance(content, str)
                    else None,
                    "finish_reason": str(finish_reason)
                    if finish_reason is not None
                    else None,
                },
            ) from exc
        if not isinstance(result, Mapping):
            raise PermanentStepError(
                "configured model provider returned a non-object result"
            )
        _validate_schema(result, schema, path="$")
        return result


_JSON_FENCE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.IGNORECASE | re.DOTALL)
_THINK_PREFIX = re.compile(r"\A\s*<think>.*?</think>\s*", re.DOTALL)


def _decode_structured_content(content: object) -> object:
    """Decode one provider JSON object while tolerating common compatible wrappers."""

    if not isinstance(content, str):
        return content
    candidate = _THINK_PREFIX.sub("", content, count=1).strip()
    fence = _JSON_FENCE.fullmatch(candidate)
    if fence is not None:
        candidate = fence.group(1).strip()
    return json.loads(candidate)


class OpenAIResearchCapability:
    def __init__(
        self,
        client: OpenAICompatibleClient,
        storage: ArtifactStorage,
        *,
        search_gateway: ResearchSearchGateway | None = None,
    ) -> None:
        self.client = client
        self.storage = storage
        self.search_gateway = search_gateway

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        snapshot = context.input_snapshot.to_dict()
        supplied_urls = _supplied_https_urls(snapshot)
        minimum_sources = _minimum_research_sources(snapshot)
        research_mode = _research_mode(snapshot)
        if research_mode == "off":
            if len(supplied_urls) < minimum_sources:
                raise _research_sources_error(
                    mode=research_mode,
                    minimum_sources=minimum_sources,
                    available_sources=len(supplied_urls),
                    search_attempted=False,
                )
            return _offline_research_result(
                context,
                storage=self.storage,
                snapshot=snapshot,
                supplied_urls=supplied_urls,
                minimum_sources=minimum_sources,
            )

        search_sources: tuple[dict[str, str], ...] = ()
        search_attempted = False
        if research_mode in {"when_missing", "required"} and (
            research_mode == "required" or len(supplied_urls) < minimum_sources
        ):
            search_attempted = True
            if self.search_gateway is None:
                raise PermanentStepError(
                    "research policy requires a configured web-search capability",
                    code="research_search_unavailable",
                    details={
                        "research_mode": research_mode,
                        "minimum_sources": minimum_sources,
                        "supplied_sources": len(supplied_urls),
                    },
                )
            search_sources = await _search_research_sources(
                self.search_gateway,
                snapshot=snapshot,
                limit=max(1, minimum_sources),
                research_mode=research_mode,
            )
        searched_urls = tuple(item["url"] for item in search_sources)
        allowed_urls = _deduplicate_https_urls((*supplied_urls, *searched_urls))
        if research_mode in {"when_missing", "required"} and (
            len(allowed_urls) < minimum_sources
            or (research_mode == "required" and not searched_urls)
        ):
            raise _research_sources_error(
                mode=research_mode,
                minimum_sources=minimum_sources,
                available_sources=len(allowed_urls),
                search_attempted=search_attempted,
            )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["brief", "sources"],
            "properties": {
                "brief": {"type": "string"},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "url", "claim"],
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "claim": {"type": "string"},
                        },
                    },
                },
            },
        }
        result = await self.client.structured(
            operation="research.collect",
            system=(
                "Produce a research brief only from the supplied immutable run input and policy. "
                "Do not invent sources; use an empty sources list when no verifiable source is supplied. "
                "Every URL in allowed_source_urls came from immutable Run input or the configured "
                "search gateway. Cite only those exact URLs and connect each claim to its source. "
                "A URL is not evidence for an unrelated event: keep the cited event, year and claim "
                "consistent with the URL/title. Recalculate every stated day interval from its dates."
            ),
            payload={
                "run": snapshot,
                "allowed_source_urls": list(allowed_urls),
                "search_results": list(search_sources),
            },
            schema=schema,
        )
        valid_sources = _normalize_research_sources(result.get("sources"), allowed_urls)
        result = {**dict(result), "sources": valid_sources}
        required_supplied_sources = min(minimum_sources, len(allowed_urls))
        if len(valid_sources) < required_supplied_sources:
            result = await self.client.structured(
                operation="research.collect",
                system=(
                    "Revise the research brief using the explicitly supplied HTTPS sources. "
                    "Return at least one source entry for each supplied URL needed to satisfy "
                    "minimum_sources. Each source must use a different supplied URL, preserve "
                    "that URL exactly, and never repeat one URL under multiple claims. Do not "
                    "add any URL that is not supplied."
                ),
                payload={
                    "run": snapshot,
                    "draft": dict(result),
                    "supplied_source_urls": list(allowed_urls),
                    "minimum_unique_sources": minimum_sources,
                },
                schema=schema,
            )
        valid_sources = _normalize_research_sources(result.get("sources"), allowed_urls)
        result = {**dict(result), "sources": valid_sources}
        validation_issues = _research_validation_issues(
            result,
            allowed_urls,
            snapshot,
        )
        if validation_issues and allowed_urls:
            result = await self.client.structured(
                operation="research.collect",
                system=(
                    "Repair the research draft so every deterministic validation issue is removed. "
                    "Use only the exact supplied URLs. Remove any digit, date, duration, quantity, "
                    "quantified comparison or year that is not already present verbatim in the "
                    "immutable Run input, including Chinese phrases such as 数天 or 数百公里; "
                    "express unsupported timing qualitatively instead. Do not invent replacements."
                ),
                payload={
                    "run": snapshot,
                    "draft": dict(result),
                    "validation_issues": list(validation_issues),
                    "supplied_source_urls": list(allowed_urls),
                },
                schema=schema,
            )
            valid_sources = _normalize_research_sources(
                result.get("sources"), allowed_urls
            )
            result = {**dict(result), "sources": valid_sources}
            validation_issues = _research_validation_issues(
                result,
                allowed_urls,
                snapshot,
            )
        if validation_issues:
            # A provider can repeat a forbidden quantity even after an explicit
            # repair prompt. Remove only the rejected measurement and replace it
            # with a qualitative phrase; never bypass or weaken validation.
            result = _qualitatively_redact_research_quantities(
                result,
                validation_issues,
            )
            validation_issues = _research_validation_issues(
                result,
                allowed_urls,
                snapshot,
            )
        valid_sources = _normalize_research_sources(result.get("sources"), allowed_urls)
        valid_sources, recovered_brief_sources = _recover_brief_citations(
            valid_sources,
            brief=result.get("brief"),
            supplied_urls=allowed_urls,
        )
        result = {**dict(result), "sources": valid_sources}
        validation_issues = _research_validation_issues(
            result,
            allowed_urls,
            snapshot,
        )
        if research_mode in {"when_missing", "required"}:
            used_keys = {
                key
                for item in valid_sources
                if (key := _https_url_key(item.get("url"))) is not None
            }
            searched_keys = {
                key
                for value in searched_urls
                if (key := _https_url_key(value)) is not None
            }
            if len(valid_sources) < minimum_sources or (
                research_mode == "required"
                and not used_keys.intersection(searched_keys)
            ):
                raise _research_sources_error(
                    mode=research_mode,
                    minimum_sources=minimum_sources,
                    available_sources=len(valid_sources),
                    search_attempted=search_attempted,
                )
        artifact = self.storage.publish(
            context,
            ProviderArtifact(
                kind="research",
                filename="research.json",
                media_type="application/json",
                data=_json_bytes(result),
            ),
        )
        return StepResult(
            artifacts=(artifact,),
            output_summary={
                "provider_protocol": "openai-compatible",
                "sources": len(valid_sources),
                "unique_sources": len(valid_sources),
                "minimum_sources": minimum_sources,
                "supplied_sources": len(supplied_urls),
                "searched_sources": len(searched_urls),
                "research_mode": research_mode or "legacy",
                "search_attempted": search_attempted,
                "recovered_brief_sources": recovered_brief_sources,
                "validation_issues": list(validation_issues),
            },
            requires_review=(
                bool(validation_issues)
                or (research_mode is None and len(valid_sources) < minimum_sources)
            ),
        )


class OpenAIWritingCapability:
    def __init__(
        self, client: OpenAICompatibleClient, storage: ArtifactStorage
    ) -> None:
        self.client = client
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        research = _required_artifact(context, "research")
        inventory = _optional_artifact(context, "inventory")
        snapshot = context.input_snapshot.to_dict()
        target_duration = _target_duration_seconds(snapshot)
        prompt_snapshot = _effective_writing_snapshot(snapshot, target_duration)
        research_payload = dict(self.storage.read_json(research))
        inventory_payload = (
            dict(self.storage.read_json(inventory)) if inventory is not None else None
        )
        inventory_coverage = (
            inventory_payload.get("coverage")
            if isinstance(inventory_payload, Mapping)
            else None
        )
        inventory_missing_concepts = (
            list(inventory_coverage.get("missing_concepts", []))
            if isinstance(inventory_coverage, Mapping)
            and isinstance(inventory_coverage.get("missing_concepts", []), list)
            else []
        )
        inventory_catalog_mode = (
            str(inventory_payload.get("catalog_mode") or "").strip()
            if isinstance(inventory_payload, Mapping)
            else ""
        )
        if not inventory_catalog_mode and isinstance(inventory_payload, Mapping):
            inventory_catalog_mode = (
                "frozen" if inventory_payload.get("catalog_snapshot_id") else "live"
            )
        frozen_inventory = (
            inventory_payload is not None and inventory_catalog_mode == "frozen"
        )
        inventory_auto_acquisition_pending = (
            isinstance(inventory_coverage, Mapping)
            and inventory_coverage.get("status") == "pending_auto_acquisition"
            and inventory_catalog_mode == "live"
        )
        narration_bounds = (
            _narration_character_bounds(target_duration)
            if target_duration is not None
            else None
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "narration", "scenes"],
            "properties": {
                "title": {"type": "string"},
                "narration": {"type": "string"},
                "scenes": {"type": "array", "items": {"type": "string"}},
                "beats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "id",
                            "sequence",
                            "narration",
                            "visual_description",
                            "must_match",
                            "must_not_match",
                        ],
                        "properties": {
                            "id": {"type": "string"},
                            "sequence": {"type": "integer", "minimum": 1},
                            "narration": {"type": "string"},
                            "visual_description": {"type": "string"},
                            "must_match": {
                                "type": "array",
                                "items": {"type": "string"},
                                **({"minItems": 1} if frozen_inventory else {}),
                            },
                            "must_not_match": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
        duration_instruction = (
            f" The requested duration is {target_duration} seconds. Write between "
            f"{narration_bounds[0]} and {narration_bounds[1]} non-whitespace narration "
            "characters so synthesized speech fills that duration; scene timing must cover "
            "the same duration."
            if narration_bounds is not None
            else ""
        )
        result = await self.client.structured(
            operation="writing.compose",
            system=(
                "Write a production script that follows the supplied effective Skill policy. "
                "Resolved production settings override conflicting Skill defaults. Treat the "
                "immutable run input and research brief as hard factual boundaries. Never invent "
                "dialogue, quotations, locations, props, actions, or shot details. Scene directions "
                "must use only conservative visual categories explicitly allowed by the input or "
                "research. A prohibition in the input is not evidence that the prohibited event "
                "occurred. When research has no sources, paraphrase only facts explicitly supplied "
                "by the user and do not write direct quotations. Do not make a website, no-data "
                "screen, data table or title card a required footage scene; express such information "
                "as an overlay on conservative, searchable B-roll. When a frozen inventory summary "
                "is supplied, keep required visual Beats within its covered concepts and representative "
                "subjects; missing concepts are warnings, never evidence that footage exists. "
                "When a live inventory is pending automatic acquisition, write conservative, "
                "independently searchable visual Beats from the verified story; its missing concepts "
                "are acquisition targets, not a reason to omit the story's necessary visuals. "
                "If the run declares a geographic scope, every authored Beat must copy the relevant "
                "place anchor into must_match so footage from a different location cannot satisfy it. "
                "Keep visual_description as a short literal search phrase, never narration, metaphor, "
                "policy copy, or an inferred action. "
                "Do not invent a point outcome, "
                "opponent error, crowd reaction or coaching action."
                " Return scenes as chronological atomic visual beats: each array item must describe "
                "one independently retrievable subject, place, action or state. Never combine "
                "multiple causal stages or later outcomes into one scene. This rule is universal "
                "and must be derived from the supplied content, not from a genre template."
                " If human review feedback is supplied, address every requested change explicitly."
                + duration_instruction
            ),
            payload={
                "run": prompt_snapshot,
                "research": research_payload,
                "inventory": inventory_payload,
                "human_review_feedback": context.review_feedback,
            },
            schema=schema,
        )
        revision_reasons: list[str] = []
        if narration_bounds is not None and not _narration_length_ok(
            str(result["narration"]), narration_bounds
        ):
            revision_reasons.append(
                f"Narration must contain between {narration_bounds[0]} and "
                f"{narration_bounds[1]} non-whitespace characters for "
                f"{target_duration} seconds."
            )
        initial_grounding_issues = _script_grounding_issues(
            result,
            research_payload,
            prompt_snapshot,
        )
        initial_inventory_issues = _inventory_visual_issues(result, inventory_payload)
        if initial_grounding_issues:
            revision_reasons.append(
                "Remove unsupported direct quotations and any detail not explicitly supported by "
                "the immutable run input or research brief."
            )
        if initial_inventory_issues:
            revision_reasons.append(
                "Every visual_description and must_match list must use literal subjects, places, "
                "or actions present in the frozen inventory representatives; do not copy narration "
                "into visual_description."
            )
        if revision_reasons:
            result = await self.client.structured(
                operation="writing.compose",
                system=(
                    "Revise the supplied draft without inventing facts. "
                    + " ".join(revision_reasons)
                    + " Preserve conservative scene categories and align scene time ranges to the "
                    "full requested duration."
                ),
                payload={
                    "run": prompt_snapshot,
                    "research": research_payload,
                    "inventory": inventory_payload,
                    "draft": dict(result),
                    "human_review_feedback": context.review_feedback,
                },
                schema=schema,
            )
            remaining_problems = list(
                _blocking_script_issues(result, research_payload, prompt_snapshot)
            )
            remaining_problems.extend(
                _inventory_visual_issues(result, inventory_payload)
            )
            if narration_bounds is not None and not _narration_length_ok(
                str(result["narration"]), narration_bounds
            ):
                remaining_problems.append(
                    f"narration_length_must_be_{narration_bounds[0]}_to_"
                    f"{narration_bounds[1]}_characters"
                )
            if remaining_problems:
                result = await self.client.structured(
                    operation="writing.compose",
                    system=(
                        "The previous revision still violates hard constraints: "
                        + ", ".join(remaining_problems)
                        + ". Rewrite it again. Remove every named forbidden term and unsupported "
                        "detail; use only explicit input facts and abstract analysis."
                    ),
                    payload={
                        "run": prompt_snapshot,
                        "research": research_payload,
                        "inventory": inventory_payload,
                        "draft": dict(result),
                        "human_review_feedback": context.review_feedback,
                    },
                    schema=schema,
                )
        result = _qualitatively_redact_script_quantities(
            result,
            _script_grounding_issues(result, research_payload, prompt_snapshot),
        )
        if (
            narration_bounds is not None
            and _narration_character_count(str(result["narration"]))
            > narration_bounds[1]
        ):
            result = dict(result)
            result["narration"] = _trim_narration(
                str(result["narration"]),
                minimum=narration_bounds[0],
                maximum=narration_bounds[1],
            )
        result = _ensure_structured_beats(result)
        result, inventory_repairs_applied = _repair_script_visuals_from_inventory(
            result, inventory_payload
        )
        actual_length = _narration_character_count(str(result["narration"]))
        duration_fit = narration_bounds is None or _narration_length_ok(
            str(result["narration"]), narration_bounds
        )
        grounding_issues = _script_grounding_issues(
            result,
            research_payload,
            prompt_snapshot,
        )
        grounding_issues = tuple(
            dict.fromkeys(
                (
                    *grounding_issues,
                    *_inventory_visual_issues(result, inventory_payload),
                )
            )
        )
        artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "script", "script.json", "application/json", _json_bytes(result)
            ),
        )
        return StepResult(
            artifacts=(artifact,),
            output_summary={
                "provider_protocol": "openai-compatible",
                "scenes": len(result.get("scenes", [])),
                "beats": len(result.get("beats", [])),
                "target_duration_seconds": target_duration,
                "narration_characters": actual_length,
                "narration_character_bounds": list(narration_bounds or ()),
                "duration_fit": duration_fit,
                "grounding_issues": list(grounding_issues),
                "inventory_constrained": (frozen_inventory),
                "inventory_catalog_mode": inventory_catalog_mode or None,
                "inventory_auto_acquisition_pending": (
                    inventory_auto_acquisition_pending
                ),
                "inventory_missing_concepts": inventory_missing_concepts,
                "inventory_repairs_applied": inventory_repairs_applied,
            },
            requires_review=not duration_fit or bool(grounding_issues),
        )


class OpenAIQualityCapability:
    """Metadata-only QC; it always requests human review and never claims AV inspection."""

    def __init__(
        self, client: OpenAICompatibleClient, storage: ArtifactStorage
    ) -> None:
        self.client = client
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        video = _required_artifact(context, "video")
        result = await self.client.structured(
            operation="quality.evaluate",
            system=(
                "Evaluate only the supplied artifact metadata and Skill QC policy. You cannot inspect "
                "the audiovisual bytes. Return risks for human review; never claim playback validation."
            ),
            payload={
                "run": context.input_snapshot.to_dict(),
                "video_metadata": video.to_dict(),
            },
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["risks", "notes"],
                "properties": {
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
            },
        )
        report = {
            **dict(result),
            "scope": "metadata_only",
            "verdict": "needs_manual_review",
        }
        artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "qc_report", "quality.json", "application/json", _json_bytes(report)
            ),
        )
        return StepResult(
            artifacts=(artifact,),
            output_summary={
                "provider_protocol": "openai-compatible",
                "scope": "metadata_only",
            },
            requires_review=True,
        )


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    try:
        return next(item for item in context.input_artifacts if item.kind == kind)
    except StopIteration as exc:
        raise PermanentStepError(f"required {kind} artifact is unavailable") from exc


def _optional_artifact(context: StepContext, kind: str) -> ArtifactRef | None:
    return next((item for item in context.input_artifacts if item.kind == kind), None)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(thaw_json(value), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )


def _research_mode(snapshot: Mapping[str, Any]) -> str | None:
    value = snapshot.get("research_mode")
    if value is None:
        # Historical v1/v2 Runs did not freeze this policy and keep their
        # existing provider/review behavior.
        return None
    mode = str(value).strip().casefold()
    if mode not in {"off", "when_missing", "required"}:
        raise PermanentStepError(
            "run input contains an invalid research_mode",
            code="research_policy_invalid",
            details={"research_mode": str(value)},
        )
    return mode


def _research_sources_error(
    *,
    mode: str,
    minimum_sources: int,
    available_sources: int,
    search_attempted: bool,
) -> PermanentStepError:
    return PermanentStepError(
        "research policy does not have enough verifiable HTTPS sources",
        code="research_sources_required",
        details={
            "research_mode": mode,
            "minimum_sources": minimum_sources,
            "available_sources": available_sources,
            "missing_sources": max(0, minimum_sources - available_sources),
            "search_attempted": search_attempted,
        },
    )


def _offline_research_result(
    context: StepContext,
    *,
    storage: ArtifactStorage,
    snapshot: Mapping[str, Any],
    supplied_urls: tuple[str, ...],
    minimum_sources: int,
) -> StepResult:
    """Publish only user-provided facts; this path performs no network call."""

    fact_lines: list[str] = []
    for key in ("topic", "angle", "brief", "facts", "context"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            fact_lines.append(f"{key}: {value.strip()}")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            items = [str(item).strip() for item in value if str(item).strip()]
            if items:
                fact_lines.append(f"{key}: {'; '.join(items[:100])}")
    brief = "\n".join(fact_lines).strip()
    if not brief:
        brief = "No user-supplied factual brief was provided."
    sources = [
        {
            "title": f"User-supplied source {index}",
            "url": url,
            "claim": (
                "The user supplied this source URL; its contents were not fetched "
                "because research_mode is off."
            ),
        }
        for index, url in enumerate(supplied_urls, start=1)
    ]
    artifact = storage.publish(
        context,
        ProviderArtifact(
            kind="research",
            filename="research.json",
            media_type="application/json",
            data=_json_bytes(
                {
                    "brief": brief,
                    "sources": sources,
                    "research_mode": "off",
                    "network_accessed": False,
                }
            ),
        ),
    )
    return StepResult(
        artifacts=(artifact,),
        output_summary={
            "provider_protocol": "offline",
            "research_mode": "off",
            "network_accessed": False,
            "search_attempted": False,
            "sources": len(sources),
            "unique_sources": len(sources),
            "minimum_sources": minimum_sources,
            "supplied_sources": len(supplied_urls),
            "validation_issues": [],
        },
    )


async def _search_research_sources(
    gateway: ResearchSearchGateway,
    *,
    snapshot: Mapping[str, Any],
    limit: int,
    research_mode: str,
) -> tuple[dict[str, str], ...]:
    query = str(snapshot.get("topic") or "").strip()
    if not query:
        query = _user_input_text(snapshot).strip()[:1000]
    try:
        result = gateway.search(query=query, limit=min(10, max(1, limit)))
        if inspect.isawaitable(result):
            result = await result
    except RetryableStepError as exc:
        raise RetryableStepError(
            "configured research search is temporarily unavailable",
            retry_after_seconds=exc.retry_after_seconds,
            code="research_search_unavailable",
            details={"research_mode": research_mode},
        ) from exc
    except PermanentStepError as exc:
        raise PermanentStepError(
            "configured research search rejected the request",
            code="research_search_unavailable",
            details={"research_mode": research_mode},
        ) from exc
    except Exception as exc:
        raise RetryableStepError(
            "configured research search is temporarily unavailable",
            code="research_search_unavailable",
            details={"research_mode": research_mode},
        ) from exc
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        raise PermanentStepError(
            "configured research search returned an invalid result",
            code="research_search_unavailable",
            details={"research_mode": research_mode},
        )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in result:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        key = _https_url_key(url)
        title = str(item.get("title") or "").strip()
        claim = str(item.get("claim") or item.get("snippet") or "").strip()
        if key is None or key in seen or not title or not claim:
            continue
        seen.add(key)
        normalized.append({"title": title, "url": url, "claim": claim})
        if len(normalized) == min(10, max(1, limit)):
            break
    return tuple(normalized)


def _deduplicate_https_urls(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _https_url_key(value)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _supplied_https_urls(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    # Sources are an input concern, not a title-formatting concern.  Scanning
    # every user field lets Skills keep a concise topic while placing evidence
    # and editorial constraints in fields such as ``angle`` or ``brief``.
    user_input = _user_input_text(snapshot)
    values = (
        match.rstrip(".,;:!?)]}，。；：！？）】》")
        for match in re.findall(r"https://[^\s<>\"']+", user_input, flags=re.IGNORECASE)
    )
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _https_url_key(value)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) == 10:
            break
    return tuple(result)


def _normalize_research_sources(
    value: object,
    supplied_urls: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return one source per unique, explicitly allowed HTTPS URL.

    The model is not a source-discovery transport. A syntactically valid URL is
    still evidence only when it was present in the immutable Run input. Keep the
    exact supplied spelling in the artifact so downstream audits can compare it
    without URL-rewrite ambiguity.
    """

    allowed = {
        key: supplied
        for supplied in supplied_urls
        if (key := _https_url_key(supplied)) is not None
    }
    if not allowed or not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        key = _https_url_key(item.get("url"))
        if key is None or key not in allowed or key in seen:
            continue
        seen.add(key)
        result.append({**dict(item), "url": allowed[key]})
    return result


def _recover_brief_citations(
    sources: list[dict[str, Any]],
    *,
    brief: object,
    supplied_urls: tuple[str, ...],
) -> tuple[list[dict[str, Any]], int]:
    """Normalize supplied URLs already cited in the brief into source entries.

    Some structured-output providers place a supplied reference in the prose
    source list but omit the parallel ``sources`` row.  Retaining that citation
    is deterministic and auditable: the URL must occur in the model-authored
    brief and must also belong to the immutable Run allow-list.  We do not add a
    factual claim; the generated row records only that the brief cited the
    user-supplied reference.  Missing URLs that are absent from the brief still
    fail the minimum-source gate.
    """

    brief_text = str(brief or "")
    # Check the immutable spelling directly.  A Markdown source entry commonly
    # has ``[https://...](https://...)``; a generic URL regex can consume the
    # intervening ``](...`` as part of the first URL and miss that citation.
    cited = {
        key
        for supplied in supplied_urls
        if supplied in brief_text and (key := _https_url_key(supplied)) is not None
    }
    present = {
        key for item in sources if (key := _https_url_key(item.get("url"))) is not None
    }
    result = list(sources)
    recovered = 0
    for supplied in supplied_urls:
        key = _https_url_key(supplied)
        if key is None or key in present or key not in cited:
            continue
        parsed = urllib.parse.urlsplit(supplied)
        result.append(
            {
                "title": f"User-supplied reference: {parsed.hostname}",
                "url": supplied,
                "claim": (
                    "Explicitly cited in the research brief as a user-supplied "
                    "reference for downstream fact verification."
                ),
            }
        )
        present.add(key)
        recovered += 1
    return result, recovered


def _https_url_key(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 2048 or any(character.isspace() for character in text):
        return None
    try:
        parsed = urllib.parse.urlsplit(text)
        # Accessing ``port`` also validates a malformed explicit port.
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    authority = hostname if port in (None, 443) else f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", authority, path, parsed.query, ""))


def _user_input_text(snapshot: Mapping[str, Any]) -> str:
    values: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if key != "_framefactory":
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(snapshot)
    return "\n".join(values)


_CHINESE_DATE = re.compile(
    r"(?P<year>(?:19|20)\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
)
_DAY_SPAN = re.compile(r"(?<!\d)(?P<days>\d{2,4})\s*天")
_URL_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_ASPECT_RATIO = re.compile(
    r"(?<!\d)(?P<ratio>9\s*[:：]\s*16|16\s*[:：]\s*9|1\s*[:：]\s*1|"
    r"4\s*[:：]\s*3|3\s*[:：]\s*4)(?!\d)"
    r"(?:\s*(?:竖屏|横屏|方形)(?:画幅|格式)?)?"
)
_ASPECT_LAYOUTS = {
    "9:16": "竖屏画幅",
    "16:9": "横屏画幅",
    "1:1": "方形画幅",
    "4:3": "横屏画幅",
    "3:4": "竖屏画幅",
}
_CLOCK_EXPRESSION = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2})[:：](?P<minute>\d{2})"
    r"(?:[:：](?P<second>\d{2}))?(?!\d)"
)
_POLICY_ASSIGNMENT = re.compile(
    r"[‘’“”'\"]?(?P<key>[A-Za-z][A-Za-z0-9_.-]*)\s*=\s*"
    r"(?P<number>\d+(?:\.\d+)?)[‘’“”'\"]?(?:\s*要求)?"
)


def _research_validation_issues(
    research: Mapping[str, Any],
    supplied_urls: tuple[str, ...],
    run_input: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return deterministic contradictions that a language model must not waive.

    This does not pretend to understand or fetch a source page.  It catches
    inexpensive, high-signal failures before research can unlock writing: a
    citation outside the immutable allow-list, a claim whose explicit year
    conflicts with the cited URL, and day-span arithmetic that contradicts the
    dates in the brief.
    """

    issues: list[str] = []
    allowed = {value.rstrip("/") for value in supplied_urls}
    sources = research.get("sources", [])
    for index, item in enumerate(sources if isinstance(sources, list) else []):
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url", "")).rstrip("/")
        claim = str(item.get("claim", ""))
        if url not in allowed:
            issues.append(f"source_{index + 1}_not_supplied")
            continue
        url_years = set(_URL_YEAR.findall(url))
        claim_years = set(_URL_YEAR.findall(claim))
        if url_years and claim_years and url_years.isdisjoint(claim_years):
            issues.append(f"source_{index + 1}_year_conflict")

    brief = str(research.get("brief", ""))
    # Search providers commonly return article URLs whose path contains a
    # numeric document identifier.  Those digits are provenance, not factual
    # claims, and must remain byte-for-byte intact for citation matching.
    brief_claim_text = brief
    for allowed_url in supplied_urls:
        brief_claim_text = brief_claim_text.replace(allowed_url, "")
    input_text = _user_input_text(run_input or {})
    numeric_pattern = r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])"
    input_numbers = set(re.findall(numeric_pattern, input_text))
    input_dates = _parsed_chinese_dates(input_text)
    input_numbers.update(
        str(abs((right - left).days))
        for left_index, left in enumerate(input_dates)
        for right in input_dates[left_index + 1 :]
    )
    if input_text:
        for value in sorted(
            set(re.findall(numeric_pattern, brief_claim_text)),
            key=lambda item: (float(item), item),
        ):
            if value not in input_numbers:
                issues.append(f"unsupported_numeric_claim:{value}")
        for value in _quantity_claims(brief_claim_text):
            if value not in input_text:
                issues.append(f"unsupported_quantity_claim:{value}")
    parsed_dates: list[date] = []
    for match in _CHINESE_DATE.finditer(brief):
        try:
            parsed_dates.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            issues.append("invalid_calendar_date")
    if len(parsed_dates) >= 2:
        valid_spans = {
            abs((right - left).days)
            for left_index, left in enumerate(parsed_dates)
            for right in parsed_dates[left_index + 1 :]
        }
        for value in {int(match.group("days")) for match in _DAY_SPAN.finditer(brief)}:
            if value not in valid_spans:
                issues.append(f"day_span_conflict:{value}")
    return tuple(dict.fromkeys(issues))


def _parsed_chinese_dates(value: str) -> list[date]:
    parsed: list[date] = []
    for match in _CHINESE_DATE.finditer(value):
        try:
            parsed.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            continue
    return parsed


def _minimum_research_sources(snapshot: Mapping[str, Any]) -> int:
    framefactory = snapshot.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    skill = framefactory.get("skill_version", {})
    skill = skill if isinstance(skill, Mapping) else {}
    policy = skill.get("research_policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    value = policy.get("minimum_sources", 1)
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 1
    )


def _target_duration_seconds(snapshot: Mapping[str, Any]) -> int | None:
    framefactory = snapshot.get("_framefactory")
    if not isinstance(framefactory, Mapping):
        return None
    composition = framefactory.get("composition_snapshot")
    if not isinstance(composition, Mapping):
        return None
    settings = composition.get("production_settings")
    if not isinstance(settings, Mapping):
        return None
    value = settings.get("target_duration_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    duration = round(value)
    return duration if 15 <= duration <= 3600 else None


def _narration_character_bounds(target_duration: int) -> tuple[int, int]:
    # Mandarin narration varies materially with punctuation, names and pauses.
    # Keep a broad production-safe range instead of rejecting usable scripts at
    # the former 4.2 chars/sec lower bound before TTS can measure real duration.
    return max(20, round(target_duration * 3.5)), round(target_duration * 6.5)


def _effective_writing_snapshot(
    snapshot: Mapping[str, Any], target_duration: int | None
) -> dict[str, Any]:
    """Project immutable input into the effective policy sent to the model.

    The original snapshot remains unchanged for audit. Per-run production settings
    are already resolved by the control plane and must override a Skill's default
    duration in the provider prompt.
    """

    effective = deepcopy(dict(snapshot))
    if target_duration is None:
        return effective
    framefactory = effective.get("_framefactory")
    if not isinstance(framefactory, dict):
        return effective
    skill_version = framefactory.get("skill_version")
    if not isinstance(skill_version, dict):
        return effective
    writing_policy = skill_version.get("writing_policy")
    if not isinstance(writing_policy, dict):
        return effective
    writing_policy["target_duration_seconds"] = target_duration
    return effective


def _narration_character_count(value: str) -> int:
    return len("".join(value.split()))


def _trim_narration(value: str, *, minimum: int, maximum: int) -> str:
    """Deterministically shorten an overlong draft without inventing content."""

    visible = 0
    cutoff = len(value)
    punctuation_cutoff: int | None = None
    for index, character in enumerate(value):
        if not character.isspace():
            visible += 1
        if visible >= minimum and character in "。！？!?；;":
            punctuation_cutoff = index + 1
        if visible >= maximum:
            cutoff = index + 1
            break
    selected = value[: punctuation_cutoff or cutoff].strip()
    if _narration_character_count(selected) < minimum:
        selected = value[:cutoff].strip()
    return selected


_DIRECT_QUOTE = re.compile(r"(?:[‘“][^’”]{1,240}[’”]|\"[^\"]{1,240}\")")
_QUANTITY_CLAIM = re.compile(
    r"(?:\d+(?:\.\d+)?|数[十百千万亿]*|[一二三四五六七八九十百千万亿两]+)"
    r"(?:\s*(?:至|到|[-–—~])\s*"
    r"(?:\d+(?:\.\d+)?|数[十百千万亿]*|[一二三四五六七八九十百千万亿两]+))?"
    r"\s*(?:颗|天|日|年|月|周|小时|分钟|秒|公里|千米|米|摄氏度|华氏度|度|百分比|%|倍|次|人|个)"
)


def _quantity_claims(value: str) -> tuple[str, ...]:
    """Return material measurements, excluding an indefinite classifier.

    Chinese prose routinely uses ``一个`` to introduce a concept rather than
    assert a count (for example “一个连续过程”). Treating it as a factual
    measurement parked valid research behind review. Concrete counts and all
    measured units remain subject to grounding.
    """

    return tuple(
        claim
        for claim in dict.fromkeys(_QUANTITY_CLAIM.findall(value))
        if claim != "一个"
    )


def _qualitatively_redact_research_quantities(
    research: Mapping[str, Any],
    issues: tuple[str, ...],
) -> dict[str, Any]:
    """Downgrade only validator-rejected claims without adding a new fact."""

    rejected_quantities = tuple(
        issue.partition(":")[2]
        for issue in issues
        if issue.startswith("unsupported_quantity_claim:") and issue.partition(":")[2]
    )
    rejected_numbers = tuple(
        issue.partition(":")[2]
        for issue in issues
        if issue.startswith("unsupported_numeric_claim:") and issue.partition(":")[2]
    )
    if not rejected_quantities and not rejected_numbers:
        return dict(research)

    sources = research.get("sources", [])
    source_items = sources if isinstance(sources, list) else []
    protected_urls = tuple(
        dict.fromkeys(
            str(item.get("url", ""))
            for item in source_items
            if isinstance(item, Mapping) and _https_url_key(item.get("url")) is not None
        )
    )

    def redact(value: object) -> str:
        text = str(value or "")
        for claim in rejected_quantities:
            replacement = (
                "一段时间"
                if claim.endswith(("天", "日", "年", "月", "周", "小时", "分钟", "秒"))
                else "较高速度"
                if claim.endswith(("公里", "千米", "米"))
                else "许多"
            )
            if claim.endswith(("公里", "千米", "米")):
                text = text.replace(f"每秒{claim}", replacement)
            text = text.replace(claim, replacement)
        return _redact_unsupported_numbers(
            text,
            rejected_numbers,
            protected_urls=protected_urls,
        )

    result = dict(research)
    result["brief"] = redact(result.get("brief", ""))
    sources = result.get("sources", [])
    if isinstance(sources, list):
        result["sources"] = [
            {
                **dict(item),
                "claim": redact(item.get("claim", "")),
            }
            if isinstance(item, Mapping)
            else item
            for item in sources
        ]
    return result


def _ensure_structured_beats(script: Mapping[str, Any]) -> dict[str, Any]:
    """Publish the same canonical Beat identity consumed by retrieval and TTS.

    Provider Beat narration is accepted only when it is already an exact
    ordered partition of the final narration. Otherwise ``normalize_beats``
    repairs it deterministically while preserving visual intent and evidence
    constraints. This also keeps IDs and sequence coordinates stable across all
    downstream artifacts.
    """

    result = dict(script)
    beats = normalize_beats(result)
    result["beats"] = [
        {
            "id": beat.id,
            "sequence": beat.sequence,
            "narration": beat.narration,
            "visual_description": beat.visual_description,
            "must_match": list(beat.must_match),
            "must_not_match": list(beat.must_not_match),
        }
        for beat in beats
    ]
    return result


def _redact_unsupported_numbers(
    value: str,
    rejected: tuple[str, ...],
    *,
    protected_urls: tuple[str, ...],
) -> str:
    """Redact named numeric issues while preserving normalized source URLs."""

    rejected_set = frozenset(rejected)
    if not rejected_set:
        return value
    protected = value
    placeholders: list[tuple[str, str]] = []
    for index, url in enumerate(sorted(protected_urls, key=len, reverse=True)):
        if not url or url not in protected:
            continue
        placeholder = f"\ue000{chr(0xE100 + index)}\ue001"
        while placeholder in protected:
            placeholder += "\ue002"
        protected = protected.replace(url, placeholder)
        placeholders.append((placeholder, url))
    redacted = _redact_numeric_fragment(protected, rejected_set)
    for placeholder, url in placeholders:
        redacted = redacted.replace(placeholder, url)
    return redacted


def _redact_numeric_fragment(value: str, rejected: frozenset[str]) -> str:
    def redact_aspect(match: re.Match[str]) -> str:
        ratio = re.sub(r"\s+", "", match.group("ratio")).replace("：", ":")
        components = ratio.split(":")
        if not rejected.intersection(components):
            return match.group(0)
        return _ASPECT_LAYOUTS[ratio]

    def redact_policy(match: re.Match[str]) -> str:
        if match.group("number") not in rejected:
            return match.group(0)
        key = match.group("key").casefold()
        return "来源时效要求" if key == "freshness_days" else "相关策略要求"

    def redact_clock(match: re.Match[str]) -> str:
        hour = match.group("hour")
        minute = match.group("minute")
        second = match.group("second")
        if not rejected.intersection(item for item in (hour, minute, second) if item):
            return match.group(0)
        return "具体时刻"

    text = _ASPECT_RATIO.sub(redact_aspect, value)
    text = _POLICY_ASSIGNMENT.sub(redact_policy, text)
    text = _CLOCK_EXPRESSION.sub(redact_clock, text)
    replacements = (
        ("年", "相关年份"),
        ("月", "相关月份"),
        ("日", "相关日期"),
        ("号", "相关日期"),
        ("时", "当天稍后"),
        ("点", "当天稍后"),
    )
    for number in sorted(rejected, key=lambda item: (-len(item), item)):
        escaped = re.escape(number)
        for suffix, replacement in replacements:
            text = re.sub(
                rf"(?<![A-Za-z0-9]){escaped}\s*{suffix}",
                replacement,
                text,
            )
        text = re.sub(
            rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
            "相关数值",
            text,
        )
    return text


def _qualitatively_redact_script_quantities(
    script: Mapping[str, Any],
    issues: tuple[str, ...],
) -> dict[str, Any]:
    """Apply the same fail-safe qualitative downgrade to a final script."""

    rejected = tuple(
        issue.partition(":")[2]
        for issue in issues
        if issue.startswith("unsupported_quantity:") and issue.partition(":")[2]
    )
    if not rejected:
        return dict(script)

    def redact(value: object) -> str:
        text = str(value or "")
        placeholders: list[str] = []
        for claim in rejected:
            replacement = (
                "一段时间"
                if claim.endswith(("天", "日", "年", "月", "周", "小时", "分钟", "秒"))
                else "较高温度"
                if claim.endswith(("摄氏度", "华氏度", "度"))
                else "较高速度"
                if claim.endswith(("公里", "千米", "米"))
                else "许多"
            )
            if replacement not in placeholders:
                placeholders.append(replacement)
            if claim.endswith(("公里", "千米", "米")):
                text = text.replace(f"每秒{claim}", replacement)
            text = text.replace(claim, replacement)
        separator = r"[\s,，、;；:：.!。！?？]*"
        for placeholder in placeholders:
            escaped = re.escape(placeholder)
            text = re.sub(
                rf"{escaped}(?:{separator}{escaped})+",
                placeholder,
                text,
            )
        return text

    result = dict(script)
    result["narration"] = redact(result.get("narration", ""))
    scenes = result.get("scenes", [])
    if isinstance(scenes, list):
        result["scenes"] = [redact(scene) for scene in scenes]
    return result


def _script_grounding_issues(
    script: Mapping[str, Any],
    research: Mapping[str, Any],
    run: Mapping[str, Any],
) -> tuple[str, ...]:
    issues: list[str] = []
    narration = str(script.get("narration", ""))
    sources = research.get("sources")
    has_sources = isinstance(sources, list) and bool(sources)
    input_text = _user_input_text(run)
    dialogue_forbidden = "对白" in input_text and any(
        marker in input_text for marker in ("不得", "禁止", "不要")
    )
    source_claims = (
        "\n".join(
            str(item.get("claim", "")) for item in sources if isinstance(item, Mapping)
        )
        if isinstance(sources, list)
        else ""
    )
    factual_evidence = f"{input_text}\n{research.get('brief', '')}\n{source_claims}"
    direct_quotes = tuple(
        match.group(0).strip('‘’“”"').strip()
        for match in _DIRECT_QUOTE.finditer(narration)
    )
    if direct_quotes and (
        dialogue_forbidden
        or not has_sources
        or any(quote not in factual_evidence for quote in direct_quotes)
    ):
        issues.append("unsupported_direct_quote")
    for term in _FORBIDDEN_GROUNDING_TERMS:
        if term in narration and _explicitly_forbidden(term, input_text):
            issues.append(f"forbidden_term:{term}")
    if not has_sources and _strict_grounding_requested(input_text):
        issues.append("strict_grounding_requires_review")
    for claim in _quantity_claims(narration):
        if claim not in factual_evidence:
            issues.append(f"unsupported_quantity:{claim}")
    evidence_text = f"{factual_evidence}\n{narration}"
    strict_location_anchors = (
        _strict_location_anchors(input_text)
        if _strict_grounding_requested(input_text)
        else ()
    )
    scenes = script.get("scenes", [])
    if isinstance(scenes, list):
        for index, raw_scene in enumerate(scenes):
            scene = str(raw_scene)
            if any(marker in scene for marker in _NON_FOOTAGE_SCENE_MARKERS):
                issues.append(f"non_footage_scene:{index + 1}")
            for marker in _UNSUPPORTED_SHOT_ASSERTIONS:
                if marker in scene and marker not in evidence_text:
                    issues.append(f"unsupported_shot_detail:{index + 1}:{marker}")
    beats = script.get("beats", [])
    if isinstance(beats, list):
        for index, raw_beat in enumerate(beats):
            if not isinstance(raw_beat, Mapping):
                continue
            visual = str(raw_beat.get("visual_description", ""))
            beat_id = str(raw_beat.get("id") or index + 1)
            raw_must_match = raw_beat.get("must_match", ())
            must_match = (
                tuple(str(item).strip() for item in raw_must_match if str(item).strip())
                if isinstance(raw_must_match, list)
                else ()
            )
            if strict_location_anchors and not any(
                anchor in term
                for anchor in strict_location_anchors
                for term in must_match
            ):
                issues.append(f"beat_location_evidence_missing:{beat_id}")
            if any(marker in visual for marker in _NON_FOOTAGE_SCENE_MARKERS):
                issues.append(f"non_footage_beat:{beat_id}")
            for marker in _UNSUPPORTED_SHOT_ASSERTIONS:
                if marker in visual and marker not in evidence_text:
                    issues.append(f"unsupported_beat_detail:{beat_id}:{marker}")
    return tuple(issues)


def _frozen_inventory_representatives(
    inventory: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(inventory, Mapping):
        return ()
    catalog_mode = str(inventory.get("catalog_mode") or "").strip()
    if not catalog_mode:
        catalog_mode = "frozen" if inventory.get("catalog_snapshot_id") else "live"
    if catalog_mode != "frozen":
        return ()
    result: list[dict[str, Any]] = []
    concepts = inventory.get("concepts", [])
    if not isinstance(concepts, list):
        return ()
    for concept in concepts:
        if not isinstance(concept, Mapping):
            continue
        representatives = concept.get("representatives", [])
        if not isinstance(representatives, list):
            continue
        for representative in representatives:
            if isinstance(representative, Mapping):
                result.append(dict(representative))
    return tuple(result)


def _repair_script_visuals_from_inventory(
    script: Mapping[str, Any],
    inventory: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    """Replace non-retrievable model shot prose with verified frozen-catalog B-roll."""
    representatives = _frozen_inventory_representatives(inventory)
    result = dict(script)
    beats = result.get("beats", [])
    if not representatives or not isinstance(beats, list):
        return result, 0

    repaired: list[dict[str, Any]] = []
    repairs = 0
    for index, raw_beat in enumerate(beats):
        if not isinstance(raw_beat, Mapping):
            repaired.append(dict(raw_beat) if isinstance(raw_beat, dict) else {})
            continue
        beat = dict(raw_beat)
        visual = str(beat.get("visual_description") or "").strip()
        existing_match = beat.get("must_match", [])
        existing_terms = (
            [str(item).strip() for item in existing_match if str(item).strip()]
            if isinstance(existing_match, list)
            else []
        )
        evidence_text = "\n".join((visual, *existing_terms)).casefold()

        selected: dict[str, Any] | None = None
        selected_label = ""
        for representative in representatives:
            labels = representative.get("labels", [])
            if not isinstance(labels, list):
                continue
            for label in labels:
                candidate = str(label).strip()
                if len(candidate) >= 2 and candidate.casefold() in evidence_text:
                    selected = representative
                    selected_label = candidate
                    break
            if selected is not None:
                break
        visual_was_supported = selected is not None
        if selected is None:
            selected = representatives[index % len(representatives)]
            labels = selected.get("labels", [])
            if isinstance(labels, list):
                selected_label = next(
                    (
                        str(label).strip()
                        for label in labels
                        if len(str(label).strip()) >= 2 and ":" not in str(label)
                    ),
                    "",
                )

        if not selected_label:
            selected_label = str(selected.get("title") or "").strip()
        if not existing_terms:
            beat["must_match"] = [selected_label] if selected_label else []
            repairs += 1

        single_beat = {"beats": [beat]}
        if not visual_was_supported or _inventory_visual_issues(single_beat, inventory):
            replacement = str(
                selected.get("description") or selected.get("title") or ""
            ).strip()
            if replacement:
                beat["visual_description"] = replacement
                beat["must_match"] = [selected_label] if selected_label else []
                repairs += 1
        repaired.append(beat)

    result["beats"] = repaired
    result["scenes"] = [str(beat.get("visual_description") or "") for beat in repaired]
    return result, repairs


def _inventory_visual_issues(
    script: Mapping[str, Any],
    inventory: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Reject Beats that cannot be tied to a frozen inventory representative."""
    representatives = _frozen_inventory_representatives(inventory)
    if not representatives:
        return ()

    anchors: set[str] = set()
    for representative in representatives:
        labels = representative.get("labels", [])
        if isinstance(labels, list):
            anchors.update(
                str(label).strip().casefold()
                for label in labels
                if len(str(label).strip()) >= 2
            )
        for evidence_field in ("title", "description"):
            text = str(representative.get(evidence_field) or "").casefold()
            anchors.update(
                token
                for token in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text)
                if len(token) >= 3
            )
    anchors.discard("")
    if not anchors:
        return ()

    issues: list[str] = []
    beats = script.get("beats", [])
    if not isinstance(beats, list):
        return ("inventory_beats_missing",)
    for index, beat in enumerate(beats):
        if not isinstance(beat, Mapping):
            issues.append(f"inventory_beat_invalid:{index + 1}")
            continue
        beat_id = str(beat.get("id") or index + 1)
        visual = str(beat.get("visual_description") or "").strip().casefold()
        raw_match = beat.get("must_match", [])
        must_match = (
            [str(item).strip().casefold() for item in raw_match if str(item).strip()]
            if isinstance(raw_match, list)
            else []
        )
        evidence_text = "\n".join((visual, *must_match))
        if not must_match:
            issues.append(f"inventory_must_match_missing:{beat_id}")
        if not any(anchor in evidence_text for anchor in anchors):
            issues.append(f"inventory_visual_unsupported:{beat_id}")
    return tuple(issues)


def _blocking_script_issues(
    script: Mapping[str, Any],
    research: Mapping[str, Any],
    run: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(
        issue
        for issue in _script_grounding_issues(script, research, run)
        if issue != "strict_grounding_requires_review"
    )


def _strict_grounding_requested(topic: str) -> bool:
    return any(
        marker in topic
        for marker in (
            "不得虚构",
            "禁止虚构",
            "只可转述",
            "仅可转述",
            "只使用已确认",
            "必须保留来源证据",
            "只使用运行绑定素材库",
        )
    )


def _strict_location_anchors(input_text: str) -> tuple[str, ...]:
    match = re.search(
        r"(?:地点范围|地域范围|地点)\s*[:：]\s*([^\r\n]+)",
        input_text,
    )
    if match is None:
        return ()
    anchors: list[str] = []
    for raw in re.split(r"[,，、;；及和与或]+", match.group(1)):
        value = raw.strip()
        if not value or any(marker in value for marker in ("典型", "所在地", "不限")):
            continue
        value = re.sub(r"(?:山区|地区|省内|全省|省|市|州|县|区)$", "", value).strip()
        if len(value) >= 2 and value not in anchors:
            anchors.append(value)
    return tuple(anchors[:8])


def _explicitly_forbidden(term: str, input_text: str) -> bool:
    for match in re.finditer(re.escape(term), input_text):
        # A prohibition applies to its nearby object list, not every noun in a
        # long editorial paragraph after the same colon.
        prefix = input_text[max(0, match.start() - 24) : match.start()]
        if any(marker in prefix for marker in ("不得", "禁止", "不要", "不可")):
            return True
    return False


_FORBIDDEN_GROUNDING_TERMS = (
    "纸条",
    "邮件",
    "巴黎",
    "暴雨",
    "图纸",
    "废墟",
    "墙上",
)

# These are editorial instructions, not searchable footage requirements.  They
# can be rendered later as text/graphics over verified B-roll, but must not
# block media coverage or be sent to remote video search as if they were shots.
_NON_FOOTAGE_SCENE_MARKERS = (
    "官网页面",
    "网页截图",
    "页面显示",
    "显示无数据",
    "数据表",
    "黑底白字",
    "文字浮现",
    "镜头慢放",
    "慢放球路",
    "镜头环绕",
    "画面拼接",
    "赛事标志",
    "无生成画面",
    "不使用生成画面",
    "未作虚构",
    "不做虚构",
    "所有事实与画面",
    "与源文件一致",
    "来源一致",
)

# A writer may name an event, athlete and broad action from evidence.  It may
# not invent a particular rally outcome merely to make a shot sound cinematic.
_UNSUPPORTED_SHOT_ASSERTIONS = (
    "接球失误",
    "回球出界",
    "最终得分",
    "观众欢呼",
    "教练指导",
)


def _narration_length_ok(value: str, bounds: tuple[int, int]) -> bool:
    length = _narration_character_count(value)
    return bounds[0] <= length <= bounds[1]


def _validate_schema(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, Mapping):
            raise PermanentStepError(f"provider result {path} must be an object")
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise PermanentStepError(f"provider result {path} is missing {missing[0]}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise PermanentStepError(
                    f"provider result {path} contains unsupported field {min(unknown)}"
                )
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], path=f"{path}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise PermanentStepError(f"provider result {path} must be an array")
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            _validate_schema(item, item_schema, path=f"{path}[{index}]")
        return
    if expected == "string" and not isinstance(value, str):
        raise PermanentStepError(f"provider result {path} must be a string")
