"""Candidate-preserving media retrieval with fail-closed gap acquisition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.adapters.database_assets import (
    AssetAcquirer,
    AssetAcquisitionResult,
    _asset_acquisition,
)
from framefactory.worker.config import AssetLibrarySettings
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact

from .beats import normalize_beats
from .catalog import (
    PostgresRetrievalCatalog,
    RetrievalCatalog,
    SearchCandidates,
)
from .models import (
    ConstraintEvidence,
    EvaluatedCandidate,
    RetrievalBeat,
    RetrievalCandidate,
)

_ALLOWED_RIGHTS = frozenset({"owned", "licensed", "public_domain"})
_LICENSE_EVIDENCE_TYPES = frozenset(
    {
        "verified_license",
        "license_verification",
        "rights_clearance",
        "manual_verification",
    }
)
_PUBLIC_DOMAIN_EVIDENCE_TYPES = frozenset(
    {
        "verified_public_domain",
        "public_domain_verification",
        "rights_clearance",
        "manual_verification",
    }
)
_PUBLIC_DOMAIN_LICENSE_MARKERS = (
    "public domain",
    "public_domain",
    "public-domain",
    "cc0",
    "cc zero",
    "pdm 1.0",
    "no known copyright",
)
_PUBLIC_DOMAIN_LOCATOR_MARKERS = (
    "creativecommons.org/publicdomain/zero/",
    "creativecommons.org/publicdomain/mark/",
)
_MAX_RIGHTS_SOURCES = 1_000
_MAX_MATERIALIZED_ASSETS = 100_000
_MEDIA_TYPE_SUFFIXES = {
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/matroska": ".mkv",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpg",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}
_TIE_BREAK = (
    "eligible_desc",
    "total_score_desc",
    "asset_id_asc",
    "asset_file_id_asc",
    "analysis_id_asc",
    "segment_id_asc",
    "source_start_ms_asc",
    "source_end_ms_asc",
    "candidate_id_asc",
)


class DatabaseRetrievalCapability:
    """Retrieve auditable Beat candidates and acquire only uncovered Beats."""

    operation = "media.retrieve"

    def __init__(
        self,
        settings: AssetLibrarySettings,
        storage: ArtifactStorage,
        *,
        catalog: RetrievalCatalog | None = None,
        search_candidates: SearchCandidates | None = None,
        object_client: Any | None = None,
        acquire_assets: AssetAcquirer | None = None,
        top_k: int = 5,
        materialize_per_beat: int = 3,
        recall_limit: int = 64,
    ) -> None:
        if catalog is not None and search_candidates is not None:
            raise ValueError("configure catalog or search_candidates, not both")
        if not 1 <= top_k <= 20:
            raise ValueError("retrieval top_k must be between 1 and 20")
        if not 1 <= materialize_per_beat <= top_k:
            raise ValueError("materialize_per_beat must be between 1 and top_k")
        if not top_k <= recall_limit <= 256:
            raise ValueError("recall_limit must be between top_k and 256")
        self.settings = settings
        self.storage = storage
        resolved_catalog = catalog or PostgresRetrievalCatalog(settings.database_url)
        self._search: SearchCandidates = search_candidates or resolved_catalog.search
        self._acquire = acquire_assets
        self._client = object_client or getattr(storage, "client", None)
        self.top_k = top_k
        self.materialize_per_beat = materialize_per_beat
        self.recall_limit = recall_limit
        if (
            not callable(getattr(storage, "publish_copy", None))
            and self._client is None
        ):
            raise ValueError(
                "database retrieval requires publish_copy support or an object-storage client"
            )

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        script_artifact = _required_artifact(context, "script")
        script = self.storage.read_json(script_artifact)
        if not isinstance(script, Mapping):
            raise PermanentStepError("script artifact must contain a JSON object")
        beats = normalize_beats(script)
        library_ids = asset_library_ids(context.input_snapshot)
        snapshot_id = catalog_snapshot_id(context.input_snapshot)
        if not library_ids and snapshot_id is None:
            raise PermanentStepError("run snapshot contains no selected asset library")

        ranked_by_beat = list(
            await self._retrieve_beats(
                context,
                library_ids,
                beats,
                catalog_snapshot_id=snapshot_id,
            )
        )
        missing_beats = _missing_beats(ranked_by_beat)
        if snapshot_id is not None and missing_beats:
            raise PermanentStepError(
                "the frozen catalog snapshot cannot cover every script Beat",
                code="asset_coverage_insufficient",
                details={
                    "operation": self.operation,
                    "catalog_snapshot_id": snapshot_id,
                    "missing_beat_ids": [beat.id for beat in missing_beats],
                    "missing_concepts": [beat.query() for beat in missing_beats],
                },
            )
        missing_before_acquisition = tuple(beat.id for beat in missing_beats)
        acquisition = _asset_acquisition(context.input_snapshot)
        resumed_after_analysis = _resumed_after_asset_analysis(context)
        acquisition_result = AssetAcquisitionResult(0, ())
        acquisition_attempted = False
        acquisition_queries: tuple[str, ...] = ()
        queried_beat_ids: tuple[str, ...] = ()
        acquisition_skipped_reason: str | None = None

        if missing_beats and acquisition["enabled"]:
            if not acquisition["rights_confirmed"]:
                raise PermanentStepError(
                    "automatic asset acquisition requires explicit rights confirmation"
                )
            if context.attempt >= context.maximum_attempts:
                # Starting another asynchronous analysis group without a spare
                # Run attempt would make the system request-changes decision
                # fail the step. Preserve an explainable human review instead.
                acquisition_skipped_reason = "automatic_attempt_budget_exhausted"
            elif self._acquire is None:
                raise RetryableStepError("automatic asset acquisition is not configured")
            else:
                query_plan = _acquisition_query_plan(
                    missing_beats,
                    snapshot=context.input_snapshot,
                )
                acquisition_queries = tuple(query for _beat_id, query in query_plan)
                queried_beat_ids = tuple(beat_id for beat_id, _query in query_plan)
                acquisition_attempted = True
                acquisition_result = await self._acquire.acquire(
                    workspace_id=context.workspace_id,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    library_id=library_ids[0],
                    queries=acquisition_queries,
                    sources=acquisition["sources"],
                    max_assets=acquisition["max_assets"],
                    copyright_status=acquisition["copyright_status"],
                )

                # The control API may return before imported files finish safe
                # analysis. Re-query only the Beats that were missing; already
                # covered choices remain stable across an acquisition round.
                refreshed = await self._retrieve_beats(
                    context,
                    library_ids,
                    missing_beats,
                    catalog_snapshot_id=None,
                )
                refreshed_by_id = {beat.id: ranked for beat, ranked in refreshed}
                ranked_by_beat = [
                    (beat, refreshed_by_id.get(beat.id, ranked))
                    for beat, ranked in ranked_by_beat
                ]
                missing_beats = _missing_beats(ranked_by_beat)
                retryable_provider_error = any(
                    item.get("retryable") is True
                    for item in acquisition_result.provider_errors
                )
                if (
                    missing_beats
                    and not acquisition_result.imported_count
                    and retryable_provider_error
                    and context.attempt < context.maximum_attempts
                ):
                    raise RetryableStepError(
                        "automatic asset provider is temporarily rate-limited or unavailable",
                        retry_after_seconds=30,
                    )
        elif missing_beats:
            acquisition_skipped_reason = "disabled"
        else:
            acquisition_skipped_reason = "local_coverage_complete"

        missing_beat_ids = tuple(beat.id for beat in missing_beats)
        auto_resume_pending = bool(
            missing_beats
            and acquisition_result.imported_count > 0
            and context.attempt < context.maximum_attempts
        )
        provider_errors_total = len(acquisition_result.provider_errors)
        provider_errors = [
            dict(item) for item in acquisition_result.provider_errors[:100]
        ]
        acquisition_audit = {
            "enabled": acquisition["enabled"],
            "attempted": acquisition_attempted,
            "step_attempt": context.attempt,
            "resumed_after_asset_analysis": resumed_after_analysis,
            "library_id": library_ids[0],
            "missing_beat_ids_before": list(missing_before_acquisition),
            "queried_beat_ids": list(queried_beat_ids),
            "queries": list(acquisition_queries),
            "sources": list(acquisition["sources"]),
            "max_assets": acquisition["max_assets"],
            "copyright_status": acquisition["copyright_status"],
            "imported_count": acquisition_result.imported_count,
            "unresolved_queries": list(acquisition_result.unresolved_queries),
            "provider_errors": provider_errors,
            "provider_errors_total": provider_errors_total,
            "provider_errors_truncated": provider_errors_total > len(provider_errors),
            "auto_resume_pending": auto_resume_pending,
            "skipped_reason": acquisition_skipped_reason,
        }

        selected = _selected_candidates(
            ranked_by_beat, per_beat=self.materialize_per_beat
        )
        _ensure_materialized_asset_limit(
            item.candidate.file_identity for _, item in selected
        )
        artifact_by_file: dict[str, ArtifactRef] = {}
        references: list[ArtifactRef] = []
        source_by_file: dict[str, RetrievalCandidate] = {}
        for _, evaluated in selected:
            await context.checkpoint()
            candidate = evaluated.candidate
            source_by_file.setdefault(candidate.file_identity, candidate)
            if candidate.file_identity in artifact_by_file:
                continue
            artifact = await self._materialize(
                context, candidate, ordinal=len(references) + 1
            )
            artifact_by_file[candidate.file_identity] = artifact
            references.append(artifact)

        selected_keys = {
            (beat.id, evaluated.candidate.candidate_id) for beat, evaluated in selected
        }
        rights_status = _materialized_rights_status(
            selected, missing_beat_ids=tuple(missing_beat_ids)
        )
        manifest = _candidate_manifest(
            script_artifact=script_artifact,
            catalog_snapshot_id=snapshot_id,
            ranked_by_beat=ranked_by_beat,
            selected_keys=selected_keys,
            artifact_by_file=artifact_by_file,
            source_by_file=source_by_file,
            top_k=self.top_k,
            materialize_per_beat=self.materialize_per_beat,
            recall_limit=self.recall_limit,
            minimum_similarity=self.settings.minimum_similarity,
            missing_beat_ids=missing_beat_ids,
            rights_status=rights_status,
            acquisition=acquisition_audit,
        )
        manifest_ref = self.storage.publish(
            context,
            ProviderArtifact(
                "candidate_manifest",
                "candidate-manifest.json",
                "application/json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            ),
        )
        requires_review = bool(missing_beat_ids)
        summary: dict[str, Any] = {
            "operation": self.operation,
            "provider": "database-asset-library",
            # Candidate ranking is always against analyzed local catalog rows.
            # Remote acquisition is a separate, explicitly audited side effect.
            "local_catalog_only": True,
            "catalog_snapshot_id": snapshot_id,
            "external_acquisition_involved": (
                acquisition_attempted or resumed_after_analysis
            ),
            "total_beats": len(beats),
            "covered_beats": len(beats) - len(missing_beat_ids),
            "missing_beat_ids": list(missing_beat_ids),
            "saved_candidates": sum(len(items) for _, items in ranked_by_beat),
            "materialized_assets": len(references),
            "candidate_manifest_artifact_id": manifest_ref.id,
            "rights_status": rights_status,
            "auto_acquisition_enabled": acquisition["enabled"],
            "acquisition_attempted": acquisition_attempted,
            "acquired_assets": acquisition_result.imported_count,
            "auto_resume_pending": auto_resume_pending,
            "acquisition": acquisition_audit,
        }
        if requires_review:
            if auto_resume_pending:
                action_required = "auto_resume_after_asset_analysis"
            elif acquisition_result.imported_count:
                action_required = "review_acquired_assets_then_request_changes"
            elif acquisition_attempted:
                action_required = (
                    "review_provider_results_or_upload_assets_then_request_changes"
                )
            else:
                action_required = "upload_or_tag_local_assets_then_request_changes"
            summary.update(
                {
                    "blocking_reason": "asset_coverage",
                    "action_required": action_required,
                }
            )
        return StepResult(
            artifacts=(*references, manifest_ref),
            output_summary=summary,
            requires_review=requires_review,
        )

    async def _retrieve_beats(
        self,
        context: StepContext,
        library_ids: tuple[str, ...],
        beats: Sequence[RetrievalBeat],
        *,
        catalog_snapshot_id: str | None,
    ) -> tuple[tuple[RetrievalBeat, tuple[EvaluatedCandidate, ...]], ...]:
        result: list[tuple[RetrievalBeat, tuple[EvaluatedCandidate, ...]]] = []
        for beat in beats:
            await context.checkpoint()
            arguments: tuple[object, ...] = (
                context.workspace_id,
                library_ids,
                beat.query(),
                self.recall_limit,
            )
            if catalog_snapshot_id is not None:
                arguments += (catalog_snapshot_id,)
            recalled = await asyncio.to_thread(self._search, *arguments)
            candidates = tuple(recalled)[: self.recall_limit]
            if any(not isinstance(item, RetrievalCandidate) for item in candidates):
                raise PermanentStepError(
                    "local retrieval catalog returned an invalid candidate"
                )
            ranked = rank_candidates(
                beat,
                candidates,
                minimum_similarity=self.settings.minimum_similarity,
            )[: self.top_k]
            result.append((beat, ranked))
        return tuple(result)

    async def _materialize(
        self,
        context: StepContext,
        candidate: RetrievalCandidate,
        *,
        ordinal: int,
    ) -> ArtifactRef:
        filename = _materialized_filename(ordinal, candidate.media_type)
        copy = getattr(self.storage, "publish_copy", None)
        if callable(copy):
            artifact = await asyncio.to_thread(
                copy,
                context,
                kind="asset",
                filename=filename,
                media_type=candidate.media_type,
                source_bucket=candidate.bucket,
                source_key=candidate.object_key,
                content_hash=candidate.content_hash,
                byte_size=candidate.byte_size,
            )
        else:
            data = await asyncio.to_thread(self._read, candidate)
            artifact = self.storage.publish(
                context,
                ProviderArtifact("asset", filename, candidate.media_type, data),
            )
        if not isinstance(artifact, ArtifactRef) or artifact.kind != "asset":
            raise PermanentStepError("asset storage returned an invalid asset Artifact")
        return artifact

    def _read(self, candidate: RetrievalCandidate) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=candidate.bucket, Key=candidate.object_key
            )
            body = response["Body"]
            try:
                data = body.read()
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            raise RetryableStepError(
                "catalog asset could not be read from object storage"
            ) from exc
        if len(data) != candidate.byte_size:
            raise PermanentStepError(
                "catalog asset byte size does not match its database record"
            )
        if hashlib.sha256(data).hexdigest() != candidate.content_hash:
            raise PermanentStepError(
                "catalog asset content hash does not match its database record"
            )
        return data


def evaluate_candidate(
    beat: RetrievalBeat,
    candidate: RetrievalCandidate,
    *,
    minimum_similarity: float,
) -> EvaluatedCandidate:
    """Apply authored hard constraints as AND/NOT gates and retain evidence."""

    haystack = " ".join(
        (
            candidate.title,
            candidate.description,
            candidate.transcript,
            *candidate.labels,
        )
    ).casefold()
    must_match = tuple((term, term.casefold() in haystack) for term in beat.must_match)
    must_not_match = tuple(
        (term, term.casefold() in haystack) for term in beat.must_not_match
    )
    constraints = ConstraintEvidence(must_match, must_not_match)
    rejection_codes: list[str] = []
    if not all(matched for _, matched in must_match):
        rejection_codes.append("must_match_missing")
    if any(matched for _, matched in must_not_match):
        rejection_codes.append("must_not_match_hit")
    score = candidate.score_breakdown.total
    if not math.isfinite(score):
        rejection_codes.append("score_not_finite")
    elif score < minimum_similarity:
        rejection_codes.append("score_below_minimum")
    if candidate.start_ms < 0 or candidate.end_ms <= candidate.start_ms:
        rejection_codes.append("source_window_invalid")
    asset_kind = candidate.asset_kind.casefold().strip()
    if not asset_kind:
        if candidate.media_type.casefold().startswith("video/"):
            asset_kind = "video"
        elif candidate.media_type.casefold().startswith("image/"):
            asset_kind = "image"
    if asset_kind not in {"image", "video"}:
        rejection_codes.append("unsupported_asset_kind")
    elif "source_window_invalid" not in rejection_codes and asset_kind == "video":
        if candidate.source_duration_ms is None or candidate.source_duration_ms <= 0:
            rejection_codes.append("source_duration_missing")
        elif candidate.end_ms > candidate.source_duration_ms:
            rejection_codes.append("source_window_out_of_bounds")
    rejection_codes.extend(_rights_evaluation(candidate)["rejection_codes"])
    return EvaluatedCandidate(candidate, constraints, tuple(rejection_codes))


def rank_candidates(
    beat: RetrievalBeat,
    candidates: Sequence[RetrievalCandidate],
    *,
    minimum_similarity: float,
) -> tuple[EvaluatedCandidate, ...]:
    """Rank deterministically, retaining rejected candidates after eligible ones."""

    evaluated = tuple(
        evaluate_candidate(beat, candidate, minimum_similarity=minimum_similarity)
        for candidate in candidates
    )
    ordered = sorted(evaluated, key=_rank_key)
    unique: dict[str, EvaluatedCandidate] = {}
    for item in ordered:
        unique.setdefault(item.candidate.candidate_id, item)
    return tuple(unique.values())


def asset_library_ids(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    framefactory = snapshot.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    raw = composition.get("asset_library_ids", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def catalog_snapshot_id(snapshot: Mapping[str, Any]) -> str | None:
    framefactory = snapshot.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    value = str(composition.get("catalog_snapshot_id") or "").strip()
    return value or None


def _missing_beats(
    ranked_by_beat: Sequence[
        tuple[RetrievalBeat, tuple[EvaluatedCandidate, ...]]
    ],
) -> tuple[RetrievalBeat, ...]:
    return tuple(
        beat
        for beat, ranked in ranked_by_beat
        if not any(item.eligible for item in ranked)
    )


_HTTPS_QUERY_TEXT = re.compile(
    r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    re.IGNORECASE,
)
_LATIN_QUERY_SPAN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<span>[A-Za-z][A-Za-z0-9'-]*"
    r"(?:[ \t]+[A-Za-z0-9][A-Za-z0-9'-]*){1,5})"
    r"(?![A-Za-z0-9])"
)
_LATIN_QUERY_TERM = re.compile(r"[A-Za-z][A-Za-z0-9'-]*|[0-9]+")
_GENERIC_PROVIDER_QUERY_TERMS = frozenset(
    {
        "a",
        "an",
        "and",
        "asset",
        "assets",
        "clip",
        "domain",
        "for",
        "footage",
        "from",
        "in",
        "media",
        "not",
        "of",
        "on",
        "only",
        "or",
        "public",
        "scene",
        "source",
        "sources",
        "the",
        "use",
        "using",
        "video",
        "visual",
        "with",
        "without",
    }
)
_NEGATIVE_PROVIDER_QUERY_TERMS = frozenset(
    {"avoid", "avoiding", "exclude", "excluding", "no", "not", "without"}
)
_NEGATIVE_PROVIDER_QUERY_MARKERS = (
    "不使用",
    "不采用",
    "不包含",
    "不能用",
    "禁止",
    "不得",
    "不要",
    "避免",
    "排除",
    "避开",
    "剔除",
    "拒绝",
    "勿用",
    "别用",
)
_PROVIDER_QUERY_CLAUSE = re.compile(r"[^。；;！？!?，,\n\r]+")


def _acquisition_query_plan(
    missing_beats: Sequence[RetrievalBeat],
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return stable provider queries without turning hints into evidence.

    A user may supply provider-friendly Latin visual entities in the immutable Run
    input while authoring narration in another language.  When a canonical Beat
    query has no useful Latin phrase, one hint is prefixed to that same query.  This
    preserves the Beat text for bilingual providers while allowing Commons to use
    its Latin-only query projection.  Hints never become Beat constraints, asset
    tags, or catalog evidence.
    """

    planned: list[tuple[str, str]] = []
    seen: set[str] = set()
    hints = _provider_query_hints(snapshot or {})
    hint_index = 0
    for beat in missing_beats:
        canonical = " ".join(beat.query().split()).strip()
        query = canonical
        if not _has_provider_latin_phrase(canonical) and hints:
            hint = hints[hint_index % len(hints)]
            hint_index += 1
            query = f"{hint} {canonical}".strip()
        query = query[:500]
        normalized = query.casefold()
        if not query or normalized in seen:
            continue
        seen.add(normalized)
        planned.append((beat.id, query))
        if len(planned) >= 12:
            break
    if not planned:
        raise PermanentStepError("missing Beats contain no acquisition query")
    return tuple(planned)


def _has_provider_latin_phrase(value: str) -> bool:
    terms = _LATIN_QUERY_TERM.findall(value)
    return len(terms) >= 2 and any(term[0].isalpha() for term in terms)


def _provider_query_hints(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract bounded Latin search phrases from user-authored Run fields.

    Only the explicit ``topic`` and ``angle`` creative-intent fields are eligible.
    Run input is otherwise an open dictionary and may contain private notes or
    workflow metadata that must never be sent to a third-party search provider.
    HTTPS locators are provenance rather than media-search phrases.
    """

    values = [
        value
        for key in ("topic", "angle")
        if isinstance((value := snapshot.get(key)), str) and value.strip()
    ]
    hints: list[str] = []
    seen: set[str] = set()
    for value in values:
        without_urls = _HTTPS_QUERY_TEXT.sub(" ", value)
        for clause_match in _PROVIDER_QUERY_CLAUSE.finditer(without_urls):
            clause = clause_match.group(0)
            clause_folded = clause.casefold()
            if any(
                marker in clause_folded
                for marker in _NEGATIVE_PROVIDER_QUERY_MARKERS
            ):
                continue
            for match in _LATIN_QUERY_SPAN.finditer(clause):
                raw_terms = _LATIN_QUERY_TERM.findall(match.group("span"))
                if any(
                    term.casefold() in _NEGATIVE_PROVIDER_QUERY_TERMS
                    for term in raw_terms
                ):
                    continue
                terms = [
                    term
                    for term in raw_terms
                    if term.casefold() not in _GENERIC_PROVIDER_QUERY_TERMS
                ]
                if len(terms) < 2 or not any(term[0].isalpha() for term in terms):
                    continue
                query = " ".join(terms[:6])[:120].strip()
                normalized = query.casefold()
                if not query or normalized in seen:
                    continue
                seen.add(normalized)
                hints.append(query)
                if len(hints) >= 12:
                    return tuple(hints)
    return tuple(hints)


def _resumed_after_asset_analysis(context: StepContext) -> bool:
    """Recognize the audited system resume without guessing from attempt count."""

    feedback = str(context.review_feedback or "")
    return (
        context.attempt > 1
        and "自动补充素材已完成安全分析与打标" in feedback
    )


def _rank_key(item: EvaluatedCandidate) -> tuple[Any, ...]:
    candidate = item.candidate
    score = candidate.score_breakdown.total
    score_key = -score if math.isfinite(score) else math.inf
    return (
        not item.eligible,
        score_key,
        candidate.asset_id,
        candidate.asset_file_id,
        candidate.analysis_id,
        candidate.segment_id,
        candidate.start_ms,
        candidate.end_ms,
        candidate.candidate_id,
    )


def _selected_candidates(
    ranked_by_beat: Sequence[tuple[RetrievalBeat, tuple[EvaluatedCandidate, ...]]],
    *,
    per_beat: int,
) -> tuple[tuple[RetrievalBeat, EvaluatedCandidate], ...]:
    return tuple(
        (beat, item)
        for beat, candidates in ranked_by_beat
        for item in tuple(candidate for candidate in candidates if candidate.eligible)[
            :per_beat
        ]
    )


def _ensure_materialized_asset_limit(file_identities: Iterable[str]) -> None:
    unique: set[str] = set()
    for identity in file_identities:
        unique.add(str(identity))
        if len(unique) > _MAX_MATERIALIZED_ASSETS:
            raise PermanentStepError(
                "retrieval selection exceeds the 100000 materialized asset contract limit"
            )


def _materialized_filename(ordinal: int, media_type: str) -> str:
    suffix = _MEDIA_TYPE_SUFFIXES.get(media_type.casefold().strip(), ".bin")
    filename = f"asset-{ordinal:03d}{suffix}"
    if len(filename) > 255:
        raise PermanentStepError("materialized asset filename exceeds 255 characters")
    return filename


def _materialized_rights_status(
    selected: Sequence[tuple[RetrievalBeat, EvaluatedCandidate]],
    *,
    missing_beat_ids: tuple[str, ...],
) -> str:
    if missing_beat_ids or not selected:
        return "requires_review"
    if all(_rights_evaluation(item.candidate)["verified"] for _, item in selected):
        return "verified"
    return "requires_review"


def _rights_evaluation(candidate: RetrievalCandidate) -> dict[str, Any]:
    status = candidate.copyright_status.casefold().strip()
    sources = tuple(
        dict(item) for item in candidate.rights_evidence if _nonempty_source(item)
    )
    result: dict[str, Any] = {
        "copyright_status": status,
        "status_allowed": status in _ALLOWED_RIGHTS,
        "evidence_required": status in {"licensed", "public_domain"},
        "evidence_present": bool(sources),
        "verified": False,
        "verification_basis": [],
        "rejection_codes": [],
        "sources": [],
    }
    preferred_sources: tuple[Mapping[str, Any], ...] = ()
    if status == "owned":
        result["verified"] = True
        result["verification_basis"] = ["copyright_status_owned"]
    elif status not in _ALLOWED_RIGHTS:
        result["rejection_codes"] = ["rights_not_allowed"]
    elif not sources:
        result["rejection_codes"] = ["rights_evidence_missing"]
    elif status == "licensed":
        claims = tuple(item for item in sources if _meaningful_license(item))
        if not claims:
            result["rejection_codes"] = ["rights_evidence_missing"]
        else:
            verified = tuple(
                item
                for item in claims
                if _verified_evidence(item, _LICENSE_EVIDENCE_TYPES)
            )
            preferred_sources = verified or claims
            if not verified:
                result["rejection_codes"] = ["rights_evidence_unverified"]
            else:
                result["verified"] = True
                result["verification_basis"] = ["verified_license_evidence"]
    else:
        claims = tuple(item for item in sources if _public_domain_claim(item))
        if not claims:
            result["rejection_codes"] = ["rights_evidence_missing"]
        else:
            verified = tuple(
                item
                for item in claims
                if _verified_evidence(item, _PUBLIC_DOMAIN_EVIDENCE_TYPES)
                or _official_public_domain_locator(item)
            )
            preferred_sources = verified or claims
            if not verified:
                result["rejection_codes"] = ["rights_evidence_unverified"]
            else:
                result["verified"] = True
                result["verification_basis"] = [
                    "verified_public_domain_evidence"
                ]
    result["sources"] = list(_bounded_rights_sources(sources, preferred_sources))
    return result


def _bounded_rights_sources(
    sources: Sequence[Mapping[str, Any]],
    preferred: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Bound evidence without hiding the records that justify the emitted verdict."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for collection in (preferred, sources):
        for source in collection:
            fingerprint = json.dumps(
                source,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            result.append(dict(source))
            if len(result) == _MAX_RIGHTS_SOURCES:
                return tuple(result)
    return tuple(result)


def _nonempty_source(source: Mapping[str, Any]) -> bool:
    return bool(source) and any(
        value is not None and str(value).strip() not in {"", "{}", "[]"}
        for value in source.values()
    )


def _meaningful_license(source: Mapping[str, Any]) -> bool:
    value = str(source.get("license") or "").strip()
    return bool(value) and value.casefold() not in {
        "unknown",
        "none",
        "n/a",
        "unverified",
    }


def _verified_evidence(
    source: Mapping[str, Any], trusted_types: frozenset[str]
) -> bool:
    if str(source.get("verified_at") or "").strip():
        return True
    return _evidence_type(source) in trusted_types


def _evidence_type(source: Mapping[str, Any]) -> str:
    return re.sub(
        r"[^a-z0-9]+", "_", str(source.get("evidence_type") or "").casefold()
    ).strip("_")


def _public_domain_claim(source: Mapping[str, Any]) -> bool:
    evidence_type = _evidence_type(source)
    if evidence_type in {"public_domain", "public_domain_mark"}:
        return True
    if _official_public_domain_locator(source):
        return True
    text = " ".join(
        str(source.get(key) or "").casefold() for key in ("license", "locator")
    )
    if "not public domain" in text:
        return False
    if any(marker in text for marker in _PUBLIC_DOMAIN_LICENSE_MARKERS):
        return True
    metadata = source.get("metadata")
    return isinstance(metadata, Mapping) and (
        metadata.get("public_domain") is True
        or str(metadata.get("copyright_status") or "").casefold() == "public_domain"
    )


def _official_public_domain_locator(source: Mapping[str, Any]) -> bool:
    locator = str(source.get("locator") or "").casefold()
    return any(marker in locator for marker in _PUBLIC_DOMAIN_LOCATOR_MARKERS)


def _candidate_manifest(
    *,
    script_artifact: ArtifactRef,
    catalog_snapshot_id: str | None,
    ranked_by_beat: Sequence[tuple[RetrievalBeat, tuple[EvaluatedCandidate, ...]]],
    selected_keys: set[tuple[str, str]],
    artifact_by_file: Mapping[str, ArtifactRef],
    source_by_file: Mapping[str, RetrievalCandidate],
    top_k: int,
    materialize_per_beat: int,
    recall_limit: int,
    minimum_similarity: float,
    missing_beat_ids: tuple[str, ...],
    rights_status: str,
    acquisition: Mapping[str, Any],
) -> dict[str, Any]:
    beats: list[dict[str, Any]] = []
    for beat, candidates in ranked_by_beat:
        items: list[dict[str, Any]] = []
        for rank, evaluated in enumerate(candidates, start=1):
            candidate = evaluated.candidate
            selected = (beat.id, candidate.candidate_id) in selected_keys
            artifact = (
                artifact_by_file.get(candidate.file_identity) if selected else None
            )
            rights = _rights_evaluation(candidate)
            items.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "rank": rank,
                    "eligible": evaluated.eligible,
                    "materialized": artifact is not None,
                    "artifact_id": artifact.id if artifact else None,
                    "artifact_filename": artifact.filename if artifact else None,
                    "asset_id": candidate.asset_id,
                    "asset_file_id": candidate.asset_file_id,
                    "analysis_id": candidate.analysis_id,
                    "segment_id": candidate.segment_id,
                    "title": candidate.title,
                    "description": candidate.description,
                    "labels": list(candidate.labels),
                    "source_transcript": candidate.transcript,
                    "asset_kind": candidate.asset_kind,
                    "media_type": candidate.media_type,
                    "content_hash": candidate.content_hash,
                    "byte_size": candidate.byte_size,
                    "source_duration_ms": candidate.source_duration_ms,
                    "source_window": {
                        "start_ms": candidate.start_ms,
                        "end_ms": candidate.end_ms,
                        "duration_ms": max(0, candidate.end_ms - candidate.start_ms),
                    },
                    "score": (
                        round(candidate.score_breakdown.total, 6)
                        if math.isfinite(candidate.score_breakdown.total)
                        else None
                    ),
                    "score_breakdown": candidate.score_breakdown.to_dict(),
                    "hard_constraints": evaluated.constraints.to_dict(),
                    "rejection_codes": list(evaluated.rejection_codes),
                    "cut_evidence": {
                        "cut_safe": candidate.cut_safe,
                        "semantic_complete": candidate.semantic_complete,
                    },
                    "rights_evidence": rights,
                }
            )
        beats.append(
            {
                "id": beat.id,
                "sequence": beat.sequence,
                "narration": beat.narration,
                "visual_description": beat.visual_description,
                "query": beat.query(),
                "must_match": list(beat.must_match),
                "must_not_match": list(beat.must_not_match),
                "coverage_status": (
                    "covered"
                    if any(item.eligible for item in candidates)
                    else "missing"
                ),
                "candidates": items,
            }
        )
    materialized_assets = []
    for file_identity, artifact in artifact_by_file.items():
        source = source_by_file[file_identity]
        materialized_assets.append(
            {
                "artifact_id": artifact.id,
                "kind": artifact.kind,
                "filename": artifact.filename,
                "media_type": artifact.media_type,
                "content_hash": artifact.content_hash,
                "byte_size": artifact.byte_size,
                "asset_id": source.asset_id,
                "asset_file_id": source.asset_file_id,
            }
        )
    manifest = {
        "schema_version": "1.0.0",
        "operation": "media.retrieve",
        "provider": "database-asset-library",
        "catalog_scope": "local",
        "local_catalog_only": True,
        "source_script_artifact_id": script_artifact.id,
        "retrieval_policy": {
            "top_k": top_k,
            "materialize_per_beat": materialize_per_beat,
            "recall_limit": recall_limit,
            "minimum_similarity": minimum_similarity,
            "tie_break": list(_TIE_BREAK),
        },
        "coverage": {
            "status": "requires_review" if missing_beat_ids else "complete",
            "total_beats": len(ranked_by_beat),
            "covered_beats": len(ranked_by_beat) - len(missing_beat_ids),
            "missing_beat_ids": list(missing_beat_ids),
        },
        "rights_status": rights_status,
        "acquisition": dict(acquisition),
        "beats": beats,
        "materialized_assets": materialized_assets,
    }
    if catalog_snapshot_id is not None:
        manifest["catalog_snapshot_id"] = catalog_snapshot_id
    return manifest


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    artifact = next(
        (item for item in context.input_artifacts if item.kind == kind), None
    )
    if artifact is None:
        raise PermanentStepError(f"required {kind} artifact is unavailable")
    return artifact
