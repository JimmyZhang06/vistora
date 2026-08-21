"""Vendor-neutral OpenAI-compatible structured-output adapter."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.skills.canonical import thaw_json
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class UrllibTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        raw = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(raw, timeout=request.timeout_seconds) as response:
                return HttpResponse(status=response.status, body=response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read())
        except (TimeoutError, urllib.error.URLError) as exc:
            raise RetryableStepError("configured model provider is temporarily unreachable") from exc


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
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": operation.replace(".", "_"), "strict": True, "schema": schema},
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
                            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
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
            raise PermanentStepError("configured model provider response exceeded 4 MiB")
        try:
            envelope = json.loads(response.body)
            content = envelope["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
        except (IndexError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentStepError(
                "configured model provider returned an invalid structured response"
            ) from exc
        if not isinstance(result, Mapping):
            raise PermanentStepError("configured model provider returned a non-object result")
        _validate_schema(result, schema, path="$")
        return result


class OpenAIResearchCapability:
    def __init__(self, client: OpenAICompatibleClient, storage: ArtifactStorage) -> None:
        self.client = client
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        snapshot = context.input_snapshot.to_dict()
        supplied_urls = _supplied_https_urls(snapshot)
        minimum_sources = _minimum_research_sources(snapshot)
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
                "Every URL in supplied_source_urls was explicitly provided for this Run. When that "
                "list is non-empty, cite only those exact URLs and connect each claim to its source. "
                "A URL is not evidence for an unrelated event: keep the cited event, year and claim "
                "consistent with the URL/title. Recalculate every stated day interval from its dates."
            ),
            payload={"run": snapshot, "supplied_source_urls": list(supplied_urls)},
            schema=schema,
        )
        if supplied_urls and not result.get("sources"):
            result = await self.client.structured(
                operation="research.collect",
                system=(
                    "Revise the research brief using the explicitly supplied HTTPS sources. "
                    "Return at least one source entry per relevant supplied URL, preserve each "
                    "URL exactly, and do not add any URL that is not supplied."
                ),
                payload={
                    "run": snapshot,
                    "draft": dict(result),
                    "supplied_source_urls": list(supplied_urls),
                },
                schema=schema,
            )
        sources = result.get("sources", [])
        valid_sources = [
            item
            for item in sources
            if isinstance(item, Mapping)
            and (
                not supplied_urls
                or str(item.get("url", "")).rstrip("/")
                in {value.rstrip("/") for value in supplied_urls}
            )
        ]
        result = {**dict(result), "sources": valid_sources}
        validation_issues = _research_validation_issues(
            result,
            supplied_urls,
            snapshot,
        )
        if validation_issues and supplied_urls:
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
                    "supplied_source_urls": list(supplied_urls),
                },
                schema=schema,
            )
            sources = result.get("sources", [])
            valid_sources = [
                item
                for item in sources
                if isinstance(item, Mapping)
                and str(item.get("url", "")).rstrip("/")
                in {value.rstrip("/") for value in supplied_urls}
            ]
            result = {**dict(result), "sources": valid_sources}
            validation_issues = _research_validation_issues(
                result,
                supplied_urls,
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
                supplied_urls,
                snapshot,
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
                "minimum_sources": minimum_sources,
                "supplied_sources": len(supplied_urls),
                "validation_issues": list(validation_issues),
            },
            requires_review=(
                len(valid_sources) < minimum_sources or bool(validation_issues)
            ),
        )


class OpenAIWritingCapability:
    def __init__(self, client: OpenAICompatibleClient, storage: ArtifactStorage) -> None:
        self.client = client
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        research = _required_artifact(context, "research")
        snapshot = context.input_snapshot.to_dict()
        target_duration = _target_duration_seconds(snapshot)
        prompt_snapshot = _effective_writing_snapshot(snapshot, target_duration)
        research_payload = dict(self.storage.read_json(research))
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
                "as an overlay on conservative, searchable B-roll. Do not invent a point outcome, "
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
        if initial_grounding_issues:
            revision_reasons.append(
                "Remove unsupported direct quotations and any detail not explicitly supported by "
                "the immutable run input or research brief."
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
                    "draft": dict(result),
                    "human_review_feedback": context.review_feedback,
                },
                schema=schema,
            )
            remaining_problems = list(
                _blocking_script_issues(result, research_payload, prompt_snapshot)
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
        actual_length = _narration_character_count(str(result["narration"]))
        duration_fit = narration_bounds is None or _narration_length_ok(
            str(result["narration"]), narration_bounds
        )
        grounding_issues = _script_grounding_issues(
            result,
            research_payload,
            prompt_snapshot,
        )
        artifact = self.storage.publish(
            context,
            ProviderArtifact("script", "script.json", "application/json", _json_bytes(result)),
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
            },
            requires_review=not duration_fit or bool(grounding_issues),
        )


class OpenAIQualityCapability:
    """Metadata-only QC; it always requests human review and never claims AV inspection."""

    def __init__(self, client: OpenAICompatibleClient, storage: ArtifactStorage) -> None:
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
            payload={"run": context.input_snapshot.to_dict(), "video_metadata": video.to_dict()},
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
        report = {**dict(result), "scope": "metadata_only", "verdict": "needs_manual_review"}
        artifact = self.storage.publish(
            context,
            ProviderArtifact("qc_report", "quality.json", "application/json", _json_bytes(report)),
        )
        return StepResult(
            artifacts=(artifact,),
            output_summary={"provider_protocol": "openai-compatible", "scope": "metadata_only"},
            requires_review=True,
        )


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    try:
        return next(item for item in context.input_artifacts if item.kind == kind)
    except StopIteration as exc:
        raise PermanentStepError(f"required {kind} artifact is unavailable") from exc


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(thaw_json(value), ensure_ascii=False, sort_keys=True).encode("utf-8")


def _supplied_https_urls(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    # Sources are an input concern, not a title-formatting concern.  Scanning
    # every user field lets Skills keep a concise topic while placing evidence
    # and editorial constraints in fields such as ``angle`` or ``brief``.
    user_input = _user_input_text(snapshot)
    values = (
        match.rstrip(".,;:!?)]}，。；：！？）】》")
        for match in re.findall(r"https://[^\s<>\"']+", user_input)
    )
    return tuple(dict.fromkeys(value for value in values if len(value) <= 2048))[:10]


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
            set(re.findall(numeric_pattern, brief)),
            key=lambda item: (float(item), item),
        ):
            if value not in input_numbers:
                issues.append(f"unsupported_numeric_claim:{value}")
        for value in _quantity_claims(brief):
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
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


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
    """Downgrade rejected measurements without adding a replacement fact."""

    rejected = tuple(
        issue.partition(":")[2]
        for issue in issues
        if issue.startswith("unsupported_quantity_claim:")
        and issue.partition(":")[2]
    )
    if not rejected:
        return dict(research)

    def redact(value: object) -> str:
        text = str(value or "")
        for claim in rejected:
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
        return text

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
    """Upgrade legacy scene arrays to the generic Beat contract.

    Providers are instructed to author precise Beat evidence. This deterministic
    compatibility path keeps older providers usable without injecting any
    subject vocabulary: narration sentences are distributed over authored
    scenes in order, and evidence arrays remain empty rather than guessed.
    """

    result = dict(script)
    raw_beats = result.get("beats")
    if isinstance(raw_beats, list) and raw_beats:
        return result
    raw_scenes = result.get("scenes", [])
    scenes = (
        [str(item).strip() for item in raw_scenes if str(item).strip()]
        if isinstance(raw_scenes, list)
        else []
    )
    narration = str(result.get("narration", "")).strip()
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])\s*", narration)
        if item.strip()
    ]
    beats: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes):
        start = index * len(sentences) // max(1, len(scenes))
        end = (index + 1) * len(sentences) // max(1, len(scenes))
        hint = "".join(sentences[start:end]) or narration
        beats.append(
            {
                "id": f"beat-{index + 1:03d}",
                "sequence": index + 1,
                "narration": hint,
                "visual_description": scene,
                "must_match": [],
                "must_not_match": [],
            }
        )
    result["beats"] = beats
    return result


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
            if claim.endswith(("公里", "千米", "米")):
                text = text.replace(f"每秒{claim}", replacement)
            text = text.replace(claim, replacement)
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
    if _DIRECT_QUOTE.search(narration) and (not has_sources or dialogue_forbidden):
        issues.append("unsupported_direct_quote")
    for term in _FORBIDDEN_GROUNDING_TERMS:
        if term in narration and _explicitly_forbidden(term, input_text):
            issues.append(f"forbidden_term:{term}")
    if not has_sources and _strict_grounding_requested(input_text):
        issues.append("strict_grounding_requires_review")
    source_claims = "\n".join(
        str(item.get("claim", ""))
        for item in sources if isinstance(item, Mapping)
    ) if isinstance(sources, list) else ""
    factual_evidence = f"{input_text}\n{research.get('brief', '')}\n{source_claims}"
    for claim in _quantity_claims(narration):
        if claim not in factual_evidence:
            issues.append(f"unsupported_quantity:{claim}")
    evidence_text = f"{factual_evidence}\n{narration}"
    scenes = script.get("scenes", [])
    if isinstance(scenes, list):
        for index, raw_scene in enumerate(scenes):
            scene = str(raw_scene)
            if any(marker in scene for marker in _NON_FOOTAGE_SCENE_MARKERS):
                issues.append(f"non_footage_scene:{index + 1}")
            for marker in _UNSUPPORTED_SHOT_ASSERTIONS:
                if marker in scene and marker not in evidence_text:
                    issues.append(f"unsupported_shot_detail:{index + 1}:{marker}")
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
        )
    )


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
