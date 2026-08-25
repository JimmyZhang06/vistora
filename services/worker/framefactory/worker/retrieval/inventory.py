"""Low-cost, read-only inventory summary for frozen Batch catalogs."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from framefactory.runtime import PermanentStepError
from framefactory.steps import StepContext, StepResult
from framefactory.worker.config import AssetLibrarySettings
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact

from .capability import (
    asset_library_ids,
    catalog_snapshot_id,
    rank_candidates,
)
from .catalog import PostgresRetrievalCatalog, RetrievalCatalog, SearchCandidates
from .models import RetrievalBeat, RetrievalCandidate

_CONCEPT_BREAK = re.compile(r"[\r\n,，;；|]+")
_MAX_CONCEPTS = 8
_MAX_REPRESENTATIVES = 3


class DatabaseInventoryCapability:
    """Describe available footage without materializing bytes or mutating a library."""

    operation = "media.inventory"

    def __init__(
        self,
        settings: AssetLibrarySettings,
        storage: ArtifactStorage,
        *,
        catalog: RetrievalCatalog | None = None,
        search_candidates: SearchCandidates | None = None,
        recall_limit: int = 12,
    ) -> None:
        if catalog is not None and search_candidates is not None:
            raise ValueError("configure catalog or search_candidates, not both")
        if not 1 <= recall_limit <= 64:
            raise ValueError("inventory recall_limit must be between 1 and 64")
        resolved_catalog = catalog or PostgresRetrievalCatalog(settings.database_url)
        self._search: SearchCandidates = search_candidates or resolved_catalog.search
        self.settings = settings
        self.storage = storage
        self.recall_limit = recall_limit

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        snapshot = context.input_snapshot.to_dict()
        snapshot_id = catalog_snapshot_id(context.input_snapshot)
        concepts = inventory_concepts(snapshot)
        if snapshot_id is None:
            raise _coverage_error(
                "media.inventory requires a frozen catalog snapshot",
                snapshot_id=None,
                concepts=concepts,
                reason="catalog_snapshot_missing",
            )

        libraries = asset_library_ids(context.input_snapshot)
        concept_rows: list[dict[str, Any]] = []
        unique_assets: set[str] = set()
        unique_segments: set[str] = set()
        total_eligible = 0
        missing: list[str] = []
        for sequence, concept in enumerate(concepts, start=1):
            await context.checkpoint()
            recalled = await asyncio.to_thread(
                self._search,
                context.workspace_id,
                libraries,
                concept,
                self.recall_limit,
                snapshot_id,
            )
            candidates = tuple(recalled)[: self.recall_limit]
            if any(not isinstance(item, RetrievalCandidate) for item in candidates):
                raise PermanentStepError(
                    "frozen catalog returned an invalid inventory candidate"
                )
            beat = RetrievalBeat(
                id=f"inventory-{sequence:02d}",
                sequence=sequence,
                narration=concept,
                visual_description=concept,
            )
            eligible = tuple(
                item
                for item in rank_candidates(
                    beat,
                    candidates,
                    minimum_similarity=self.settings.minimum_similarity,
                )
                if item.eligible
            )
            if not eligible:
                missing.append(concept)
            representatives = []
            for item in eligible[:_MAX_REPRESENTATIVES]:
                candidate = item.candidate
                unique_assets.add(candidate.asset_id)
                unique_segments.add(candidate.segment_id)
                representatives.append(
                    {
                        "asset_id": candidate.asset_id,
                        "segment_id": candidate.segment_id,
                        "title": candidate.title,
                        "description": candidate.description,
                        "labels": list(candidate.labels[:16]),
                        "score": round(candidate.score_breakdown.total, 6),
                    }
                )
            total_eligible += len(eligible)
            concept_rows.append(
                {
                    "concept": concept,
                    "status": "covered" if eligible else "missing",
                    "eligible_candidates": len(eligible),
                    "representatives": representatives,
                }
            )

        if total_eligible == 0:
            raise _coverage_error(
                "the frozen catalog contains no eligible footage for this topic",
                snapshot_id=snapshot_id,
                concepts=concepts,
                reason="no_eligible_candidates",
            )

        inventory = {
            "schema_version": "1.0.0",
            "operation": self.operation,
            "catalog_snapshot_id": snapshot_id,
            "topic": str(snapshot.get("topic") or "").strip(),
            "coverage": {
                "status": "complete" if not missing else "partial",
                "total_concepts": len(concepts),
                "covered_concepts": len(concepts) - len(missing),
                "missing_concepts": missing,
            },
            "eligible_candidates": total_eligible,
            "unique_assets": len(unique_assets),
            "unique_segments": len(unique_segments),
            "concepts": concept_rows,
        }
        artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "inventory",
                "inventory.json",
                "application/json",
                json.dumps(
                    inventory,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            ),
        )
        return StepResult(
            artifacts=(artifact,),
            output_summary={
                "operation": self.operation,
                "provider": "database-asset-library",
                "local_catalog_only": True,
                "catalog_snapshot_id": snapshot_id,
                "coverage_status": inventory["coverage"]["status"],
                "covered_concepts": inventory["coverage"]["covered_concepts"],
                "missing_concepts": missing,
                "eligible_candidates": total_eligible,
                "unique_assets": len(unique_assets),
            },
        )


def inventory_concepts(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Derive a bounded, deterministic query set from explicit Run input only."""

    values: list[object] = []
    for key in ("inventory_concepts", "concepts", "keywords"):
        raw = snapshot.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values.extend(raw)
    if not values:
        values.extend(
            value
            for key in ("topic", "angle")
            if (value := snapshot.get(key)) is not None
        )
    concepts: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in _CONCEPT_BREAK.split(str(raw)):
            concept = " ".join(part.split()).strip()[:500]
            key = concept.casefold()
            if not concept or key in seen:
                continue
            seen.add(key)
            concepts.append(concept)
            if len(concepts) == _MAX_CONCEPTS:
                return tuple(concepts)
    if concepts:
        return tuple(concepts)
    return ("unspecified topic",)


def _coverage_error(
    message: str,
    *,
    snapshot_id: str | None,
    concepts: Sequence[str],
    reason: str,
) -> PermanentStepError:
    return PermanentStepError(
        message,
        code="asset_coverage_insufficient",
        details={
            "operation": "media.inventory",
            "catalog_snapshot_id": snapshot_id,
            "missing_concepts": list(concepts),
            "reason": reason,
        },
    )
