"""Immutable domain values for the local-catalog retrieval capability."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MEDIA_TYPE = re.compile(r"^(?:image|video)/[a-z0-9!#$&^_.+-]+$")


@dataclass(frozen=True, slots=True)
class RetrievalBeat:
    """A stable, ordered unit of narration and visual intent."""

    id: str
    sequence: int
    narration: str
    visual_description: str
    must_match: tuple[str, ...] = ()
    must_not_match: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("retrieval beat id must not be empty")
        if len(self.id) > 160:
            raise ValueError("retrieval beat id must not exceed 160 characters")
        if self.sequence < 1:
            raise ValueError("retrieval beat sequence must be positive")
        if not self.narration.strip():
            raise ValueError("retrieval beat narration must not be empty")
        if not self.visual_description.strip():
            raise ValueError("retrieval beat visual_description must not be empty")

    def query(self) -> str:
        parts = (self.visual_description, *self.must_match)
        return " ".join(dict.fromkeys(part.strip() for part in parts if part.strip()))


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Auditable local-catalog score components; no opaque model score is hidden."""

    lexical_similarity: float
    label_hits: int = 0
    label_score: float = 0.0
    cut_safe_bonus: float = 0.0
    semantic_complete_bonus: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.lexical_similarity
            + self.label_score
            + self.cut_safe_bonus
            + self.semantic_complete_bonus
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "lexical_similarity": _finite_or_none(self.lexical_similarity),
            "label_hits": self.label_hits,
            "label_score": _finite_or_none(self.label_score),
            "cut_safe_bonus": _finite_or_none(self.cut_safe_bonus),
            "semantic_complete_bonus": _finite_or_none(self.semantic_complete_bonus),
            "total": _finite_or_none(self.total),
        }


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """One immutable analyzed source window recalled from the local catalog."""

    asset_id: str
    asset_file_id: str
    analysis_id: str
    segment_id: str
    title: str
    bucket: str
    object_key: str
    media_type: str
    content_hash: str
    byte_size: int
    original_filename: str
    start_ms: int
    end_ms: int
    description: str
    labels: tuple[str, ...]
    score_breakdown: ScoreBreakdown
    transcript: str = ""
    cut_safe: bool = False
    semantic_complete: bool = False
    copyright_status: str = "unknown"
    rights_evidence: tuple[Mapping[str, Any], ...] = ()
    source_duration_ms: int | None = None
    asset_kind: str = ""

    def __post_init__(self) -> None:
        for name in ("asset_id", "asset_file_id", "analysis_id", "segment_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"retrieval candidate {name} must not be empty")
        if self.byte_size < 0:
            raise ValueError("retrieval candidate byte_size must not be negative")
        media_type = self.media_type.casefold().strip()
        if not _MEDIA_TYPE.fullmatch(media_type):
            raise ValueError("retrieval candidate media_type must be a concrete image/video MIME")
        object.__setattr__(self, "media_type", media_type)
        labels: list[str] = []
        seen_labels: set[str] = set()
        for raw_label in self.labels:
            label = str(raw_label).strip()[:240]
            if not label or label in seen_labels:
                continue
            labels.append(label)
            seen_labels.add(label)
            if len(labels) == 256:
                break
        object.__setattr__(self, "labels", tuple(labels))

    @property
    def candidate_id(self) -> str:
        identity = json.dumps(
            [
                self.asset_id,
                self.asset_file_id,
                self.analysis_id,
                self.segment_id,
                self.start_ms,
                self.end_ms,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"candidate-{hashlib.sha256(identity).hexdigest()[:20]}"

    @property
    def file_identity(self) -> str:
        """Deduplicate immutable bytes while preserving candidate lineage separately."""

        return self.content_hash or self.asset_file_id


@dataclass(frozen=True, slots=True)
class ConstraintEvidence:
    must_match: tuple[tuple[str, bool], ...]
    must_not_match: tuple[tuple[str, bool], ...]

    @property
    def passed(self) -> bool:
        return all(matched for _, matched in self.must_match) and not any(
            matched for _, matched in self.must_not_match
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "must_match": [
                {"term": term, "matched": matched} for term, matched in self.must_match
            ],
            "must_not_match": [
                {"term": term, "matched": matched}
                for term, matched in self.must_not_match
            ],
        }


@dataclass(frozen=True, slots=True)
class EvaluatedCandidate:
    candidate: RetrievalCandidate
    constraints: ConstraintEvidence
    rejection_codes: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.rejection_codes


def stable_beat_id(
    *,
    sequence: int,
    narration: str,
    visual_description: str,
    must_match: tuple[str, ...],
    must_not_match: tuple[str, ...],
) -> str:
    """Derive the same id for the same normalized Beat on every retry."""

    payload = json.dumps(
        {
            "sequence": sequence,
            "narration": narration,
            "visual_description": visual_description,
            "must_match": must_match,
            "must_not_match": must_not_match,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"beat-{sequence:03d}-{hashlib.sha256(payload).hexdigest()[:12]}"


def _finite_or_none(value: float) -> float | None:
    return round(float(value), 6) if math.isfinite(value) else None
