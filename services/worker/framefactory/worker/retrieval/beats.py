"""Normalize authored and legacy scripts into the retrieval Beat contract."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from framefactory.runtime import PermanentStepError
from framefactory.worker.semantic_text import ordered_visual_beats

from .models import RetrievalBeat, stable_beat_id

_PARTITION_TEXT = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")
_WHITESPACE = re.compile(r"\s+")
_TERMINAL_PUNCTUATION = frozenset("。！？!?；;")
_CLAUSE_PUNCTUATION = frozenset("，,：:")
_MAX_BEATS = 10_000
_MAX_TERM_LENGTH = 240


def normalize_beats(script: Mapping[str, Any]) -> tuple[RetrievalBeat, ...]:
    """Return stable, chronological Beats without inventing visual evidence."""

    raw_beats = script.get("beats")
    if _is_sequence(raw_beats):
        if len(raw_beats) > _MAX_BEATS:
            raise PermanentStepError("script exceeds the 10000 Beat contract limit")
        authored = [item for item in raw_beats if isinstance(item, Mapping)]
        if authored:
            indexed = tuple(enumerate(authored))
            authored = [
                item
                for _, item in sorted(
                    indexed,
                    key=lambda entry: (_authored_sequence(entry[1]), entry[0]),
                )
            ]
            narration = str(script.get("narration") or "").strip()
            authored_narration = tuple(
                str(item.get("narration") or "").strip() for item in authored
            )
            narration_hints = (
                authored_narration
                if _is_exact_narration_partition(authored_narration, narration)
                else partition_narration(narration, len(authored))
                if narration
                else authored_narration
            )
            if len(narration_hints) != len(authored) or any(
                not item.strip() for item in narration_hints
            ):
                raise PermanentStepError(
                    "script narration cannot be partitioned into non-empty authored Beats"
                )
            beats = tuple(
                _normalize_authored_beat(item, index, narration_hints[index - 1])
                for index, item in enumerate(authored, start=1)
            )
            return _ensure_unique_ids(beats)

    raw_scenes = script.get("scenes")
    scenes = raw_scenes if _is_sequence(raw_scenes) else ()
    narration = str(script.get("narration") or "").strip()
    visuals = ordered_visual_beats(scenes, narration)
    if len(visuals) > _MAX_BEATS:
        raise PermanentStepError("script exceeds the 10000 Beat contract limit")
    if not visuals:
        title = str(script.get("title") or "").strip()
        visuals = (title,) if title else ()
    if not visuals:
        raise PermanentStepError("script contains no retrievable Beat")
    narration_hints = partition_narration(narration, len(visuals))
    if len(narration_hints) != len(visuals):
        raise PermanentStepError(
            "script narration cannot be partitioned into non-empty visual Beats"
        )
    beats = tuple(
        _new_beat(
            sequence=index,
            authored_id="",
            narration=narration_hints[index - 1] or visual,
            visual_description=visual,
            must_match=(),
            must_not_match=(),
        )
        for index, visual in enumerate(visuals, start=1)
    )
    return beats


def constraint_terms(value: Any) -> tuple[str, ...]:
    if not _is_sequence(value):
        return ()
    terms = tuple(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )
    if any(len(item) > _MAX_TERM_LENGTH for item in terms):
        raise PermanentStepError("Beat constraint terms must not exceed 240 characters")
    return terms[:12]


def _normalize_authored_beat(
    value: Mapping[str, Any], sequence: int, narration_hint: str
) -> RetrievalBeat:
    narration = narration_hint.strip()
    visual = str(
        value.get("visual_description")
        or value.get("description")
        or value.get("text")
        or ""
    ).strip()
    if not visual:
        visual = narration
    if not visual:
        raise PermanentStepError(f"Beat {sequence} has no visual_description")
    if not narration:
        narration = visual
    return _new_beat(
        sequence=sequence,
        authored_id=str(value.get("id") or "").strip(),
        narration=narration,
        visual_description=visual,
        must_match=constraint_terms(value.get("must_match")),
        must_not_match=constraint_terms(value.get("must_not_match")),
    )


def _new_beat(
    *,
    sequence: int,
    authored_id: str,
    narration: str,
    visual_description: str,
    must_match: tuple[str, ...],
    must_not_match: tuple[str, ...],
) -> RetrievalBeat:
    beat_id = authored_id if 0 < len(authored_id) <= 160 else stable_beat_id(
        sequence=sequence,
        narration=narration,
        visual_description=visual_description,
        must_match=must_match,
        must_not_match=must_not_match,
    )
    return RetrievalBeat(
        id=beat_id,
        sequence=sequence,
        narration=narration,
        visual_description=visual_description,
        must_match=must_match,
        must_not_match=must_not_match,
    )


def _ensure_unique_ids(beats: tuple[RetrievalBeat, ...]) -> tuple[RetrievalBeat, ...]:
    seen: set[str] = set()
    result: list[RetrievalBeat] = []
    for beat in beats:
        if beat.id not in seen:
            seen.add(beat.id)
            result.append(beat)
            continue
        replacement = stable_beat_id(
            sequence=beat.sequence,
            narration=beat.narration,
            visual_description=beat.visual_description,
            must_match=beat.must_match,
            must_not_match=beat.must_not_match,
        )
        if replacement in seen:
            replacement = f"{replacement}-{beat.sequence:03d}"
        seen.add(replacement)
        result.append(
            RetrievalBeat(
                id=replacement,
                sequence=beat.sequence,
                narration=beat.narration,
                visual_description=beat.visual_description,
                must_match=beat.must_match,
                must_not_match=beat.must_not_match,
            )
        )
    return tuple(result)


def partition_narration(narration: str, count: int) -> tuple[str, ...]:
    """Partition narration into ordered, non-overlapping, non-empty Beat text.

    Semantic punctuation is preferred. When there are fewer sentences than
    visual Beats, the longest remaining clause is split deterministically near
    its midpoint instead of copying the complete narration into empty slots.
    The whitespace-normalized concatenation of the returned values is always
    exactly the whitespace-normalized narration, which lets native TTS word
    boundaries prove Beat timing when their token boundaries line up.
    """

    text = narration.strip()
    if count <= 0 or not text:
        return ()
    pieces = [text]
    while len(pieces) < count:
        candidates = [
            (_partition_text_length(piece), -index, index)
            for index, piece in enumerate(pieces)
            if _preferred_split(piece) is not None
        ]
        if not candidates:
            break
        _, _, index = max(candidates)
        cut = _preferred_split(pieces[index])
        if cut is None:
            break
        left = pieces[index][:cut].strip()
        right = pieces[index][cut:].strip()
        if not _partition_text(left) or not _partition_text(right):
            break
        pieces[index : index + 1] = [left, right]
    if len(pieces) != count:
        return ()
    result = tuple(piece.strip() for piece in pieces)
    return result if _is_exact_narration_partition(result, text) else ()


def _preferred_split(value: str) -> int | None:
    midpoint = len(value) / 2
    for predicate in (
        lambda left, _right: left in _TERMINAL_PUNCTUATION,
        lambda left, _right: left in _CLAUSE_PUNCTUATION,
        lambda left, right: left.isspace() or right.isspace(),
        lambda _left, _right: True,
    ):
        cuts = [
            index
            for index in range(1, len(value))
            if predicate(value[index - 1], value[index])
            and _partition_text(value[:index])
            and _partition_text(value[index:])
        ]
        if cuts:
            return min(cuts, key=lambda index: (abs(index - midpoint), index))
    return None


def _is_exact_narration_partition(values: Sequence[str], narration: str) -> bool:
    content = _partition_content(narration)
    return bool(content) and all(_partition_text(value) for value in values) and (
        "".join(_partition_content(value) for value in values) == content
    )


def _partition_text(value: str) -> str:
    return _PARTITION_TEXT.sub("", value).casefold()


def _partition_content(value: str) -> str:
    return _WHITESPACE.sub("", value)


def _partition_text_length(value: str) -> int:
    return len(_partition_text(value))


def _authored_sequence(value: Mapping[str, Any]) -> int:
    raw = value.get("sequence")
    if isinstance(raw, bool):
        return 1_000_000
    try:
        sequence = int(raw)
    except (TypeError, ValueError):
        return 1_000_000
    return sequence if sequence > 0 else 1_000_000


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
