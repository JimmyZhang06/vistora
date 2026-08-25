"""Deterministic narration-aware edit timeline planning."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from framefactory.worker.semantic_text import semantic_similarity, semantic_tokens

_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])\s*")


@dataclass(frozen=True, slots=True)
class _Fragment:
    narration: str
    duration: float
    sentence_index: int
    sentence_count: int
    intent: frozenset[str]
    narrative_role: str
    time_shift: str | None
    storyboard_scene: str | None


_NARRATIVE_INTENTS: tuple[
    tuple[str, tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        "setup",
        ("开端", "起初", "最初", "初见", "相遇", "小时候", "年轻", "大学", "校园", "学生"),
        ("开场", "环境", "全景", "年轻", "青年", "校园", "学校", "课堂", "学生", "相遇"),
    ),
    (
        "conflict",
        ("但是", "却", "争执", "冲突", "误会", "错过", "拒绝", "分开", "离开", "失去", "独自"),
        ("冲突", "争执", "拒绝", "离开", "门口", "独自", "分离", "夜晚", "特写"),
    ),
    (
        "work",
        ("工作", "事业", "公司", "办公室", "项目", "设计", "职业", "成为"),
        ("工作", "办公室", "公司", "文件", "设计", "图纸", "城市", "职业"),
    ),
    (
        "journey",
        ("来到", "前往", "旅途", "路上", "远方", "故乡"),
        ("道路", "行走", "车站", "户外", "远景"),
    ),
    (
        "journey",
        ("城市", "街道", "都市"),
        ("城市", "街道", "建筑", "道路", "车流"),
    ),
    (
        "journey",
        ("海边", "海洋", "海浪", "礁石", "河流", "湖边"),
        ("海边", "海洋", "海浪", "礁石", "水面", "自然景观"),
    ),
    (
        "home",
        ("家", "房子", "住宅", "老屋", "旧屋", "建筑", "空间", "重建", "改造"),
        ("住宅", "房屋", "建筑", "室内", "门口", "图纸", "结构", "空间"),
    ),
    (
        "resolution",
        ("终于", "最终", "结局", "重新", "再次", "和解", "明白", "完成", "留下"),
        ("重逢", "和解", "完成", "平静", "全景", "远景", "日落", "结尾"),
    ),
    (
        "reflection",
        ("回望", "回忆", "想起", "记得", "原来", "意义", "人生", "时间", "后来"),
        ("回忆", "细节", "空镜", "慢镜头", "特写", "远景", "平静"),
    ),
)

_BACKWARD_TIME_MARKERS = ("回忆", "当年", "曾经", "那时", "往事", "小时候")
_FORWARD_TIME_MARKERS = ("多年后", "后来", "如今", "此后", "长大", "成年", "最终")


def plan_edit_timeline(
    script: Mapping[str, Any],
    assets: Sequence[Mapping[str, Any]],
    duration_seconds: float,
    *,
    minimum_shot_seconds: float = 2.2,
    target_shot_seconds: float = 4.8,
    maximum_shot_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Plan variable-length shots aligned to narration and asset semantics.

    The result covers the measured audio duration exactly. It contains only
    durable manifest identifiers and never leaks local paths.
    """

    if not assets or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return []
    if not 0.25 <= minimum_shot_seconds <= target_shot_seconds <= maximum_shot_seconds:
        raise ValueError("shot duration bounds are invalid")

    narration = str(script.get("narration", "")).strip()
    sentences = [item.strip() for item in _SENTENCE_BREAK.split(narration) if item.strip()]
    if not sentences:
        sentences = [narration or str(script.get("title", "")).strip() or "画面"]
    weights = [_speech_weight(sentence) for sentence in sentences]
    total_weight = sum(weights) or float(len(sentences))
    sentence_durations = [duration_seconds * weight / total_weight for weight in weights]
    raw_scenes = script.get("scenes", ())
    authored_scenes = (
        tuple(str(item).strip() for item in raw_scenes if str(item).strip())
        if isinstance(raw_scenes, Sequence) and not isinstance(raw_scenes, (str, bytes))
        else ()
    )
    selected_scenes = tuple(
        dict.fromkeys(
            str(entry.get("selected_for_scene", "")).strip()
            for entry in assets
            if str(entry.get("selected_for_scene", "")).strip()
        )
    )
    narration_hints: dict[str, tuple[str, ...]] = {}
    for entry in assets:
        scene = str(entry.get("selected_for_scene", "")).strip()
        hint = str(entry.get("selected_for_narration", "")).strip()
        if scene and hint:
            narration_hints[scene] = tuple(
                dict.fromkeys((*narration_hints.get(scene, ()), hint))
            )
    # The selector can recover an explicit narrated beat omitted by the writing
    # model's storyboard.  Its durable manifest is therefore more complete than
    # the original scene list and must drive sentence-to-shot alignment.
    storyboard_scenes = selected_scenes or authored_scenes
    aligned_scenes = _align_storyboard_scenes(
        sentences,
        storyboard_scenes,
        narration_hints=narration_hints,
    )

    fragments: list[_Fragment] = []
    for sentence_index, (sentence, sentence_duration) in enumerate(
        zip(sentences, sentence_durations, strict=True)
    ):
        count = _shot_count(
            sentence_duration,
            minimum=minimum_shot_seconds,
            target=target_shot_seconds,
            maximum=maximum_shot_seconds,
        )
        intent, role, time_shift = _narrative_intent(sentence)
        storyboard_scene = aligned_scenes[sentence_index] if aligned_scenes else None
        for _ in range(count):
            fragments.append(
                _Fragment(
                    narration=sentence,
                    duration=sentence_duration / count,
                    sentence_index=sentence_index,
                    sentence_count=len(sentences),
                    intent=intent,
                    narrative_role=role,
                    time_shift=time_shift,
                    storyboard_scene=storyboard_scene,
                )
            )

    # Sentence-level allocation can leave an overlong fragment when the complete
    # narration is extremely short. Rebalance globally while retaining sentence order.
    minimum_count = max(1, math.ceil(duration_seconds / maximum_shot_seconds))
    if len(fragments) < minimum_count:
        fragments = _split_longest_fragments(fragments, minimum_count)

    path, path_scores = _plan_asset_path(fragments, assets)
    usage = Counter[int]()
    cursor = 0.0
    result: list[dict[str, Any]] = []
    previous: int | None = None
    for ordinal, fragment in enumerate(fragments):
        end = (
            duration_seconds
            if ordinal == len(fragments) - 1
            else cursor + fragment.duration
        )
        shot_duration = max(0.001, end - cursor)
        asset_index = path[ordinal]
        semantic_score = _semantic_match(fragment, assets[asset_index])
        entry = assets[asset_index]
        source_start, source_end = _source_window(
            entry,
            shot_duration,
            usage[asset_index],
        )
        source_duration = max(0.0, source_end - source_start)
        result.append(
            {
                "ordinal": ordinal,
                "timeline_start_seconds": round(cursor, 3),
                "timeline_end_seconds": round(end, 3),
                "duration_seconds": round(shot_duration, 3),
                "asset_index": asset_index,
                "asset_id": str(entry.get("asset_id", "")) or None,
                "artifact_id": str(entry.get("artifact_id", "")) or None,
                "source_start_seconds": round(source_start, 3),
                "source_end_seconds": round(source_end, 3),
                "source_duration_seconds": round(source_duration, 3),
                "padding_seconds": round(max(0.0, shot_duration - source_duration), 3),
                "narration": fragment.narration,
                "semantic_score": round(semantic_score, 3),
                "story_path_score": round(path_scores[ordinal], 3),
                "narrative_role": fragment.narrative_role,
                "time_shift": fragment.time_shift,
                "transition": _transition_label(previous, asset_index, fragment),
                "motion": str(entry.get("motion") or "static")[:32],
                "motion_focus": _motion_focus(entry.get("motion_focus")),
                "cut_evidence": (
                    "semantic_safe"
                    if entry.get("semantic_complete") is True
                    else "audio_safe"
                    if entry.get("cut_safe") is True
                    else "candidate"
                ),
            }
        )
        usage[asset_index] += 1
        previous = asset_index
        cursor = end
    return _coalesce_continuous_image_motion(result)


def _coalesce_continuous_image_motion(
    shots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge adjacent holds so one image motion never resets mid-sentence."""

    merged: list[dict[str, Any]] = []
    for source in shots:
        shot = dict(source)
        previous = merged[-1] if merged else None
        same_motion_hold = (
            previous is not None
            and shot.get("motion") == "zoom_in"
            and previous.get("motion") == shot.get("motion")
            and previous.get("artifact_id") == shot.get("artifact_id")
            and previous.get("motion_focus") == shot.get("motion_focus")
            and previous.get("narration") == shot.get("narration")
            and abs(
                _number(previous.get("timeline_end_seconds"))
                - _number(shot.get("timeline_start_seconds"))
            )
            <= 0.002
        )
        if not same_motion_hold:
            merged.append(shot)
            continue
        assert previous is not None
        end = _number(shot.get("timeline_end_seconds"))
        start = _number(previous.get("timeline_start_seconds"))
        duration = max(0.001, end - start)
        previous["timeline_end_seconds"] = round(end, 3)
        previous["duration_seconds"] = round(duration, 3)
        source_start = _number(previous.get("source_start_seconds"))
        previous["source_end_seconds"] = round(source_start + duration, 3)
        previous["source_duration_seconds"] = round(duration, 3)
        previous["padding_seconds"] = 0.0
    for ordinal, shot in enumerate(merged):
        shot["ordinal"] = ordinal
    return merged


def _shot_count(duration: float, *, minimum: float, target: float, maximum: float) -> int:
    lower = max(1, math.ceil(duration / maximum))
    upper = max(1, math.floor(duration / minimum))
    # A sentence that is noticeably longer than the target shot length should
    # receive another visual beat.  Rounding 6.5 seconds down to one 6.5-second
    # hold made short source clips freeze and produced the rigid edits users
    # reported.  Ceiling retains the configured minimum while favouring motion.
    return min(upper, max(lower, math.ceil(duration / target)))


def _split_longest_fragments(
    fragments: list[_Fragment], target_count: int
) -> list[_Fragment]:
    values = list(fragments)
    while len(values) < target_count:
        index = max(range(len(values)), key=lambda item: values[item].duration)
        fragment = values[index]
        split = _Fragment(
            narration=fragment.narration,
            duration=fragment.duration / 2,
            sentence_index=fragment.sentence_index,
            sentence_count=fragment.sentence_count,
            intent=fragment.intent,
            narrative_role=fragment.narrative_role,
            time_shift=fragment.time_shift,
            storyboard_scene=fragment.storyboard_scene,
        )
        values[index : index + 1] = [split, split]
    return values


def _speech_weight(value: str) -> float:
    visible = len(re.sub(r"\s+", "", value))
    pauses = 0.7 * len(re.findall(r"[，、,:：]", value))
    stops = 1.8 * len(re.findall(r"[。！？!?；;]", value))
    return max(1.0, visible + pauses + stops)


def _semantic_tokens(value: object) -> set[str]:
    return set(semantic_tokens(value))


def _asset_text(entry: Mapping[str, Any]) -> str:
    labels = entry.get("labels", [])
    label_text = (
        " ".join(str(item) for item in labels)
        if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes))
        else ""
    )
    return " ".join(
        (
            str(entry.get("selected_for_scene", "")),
            str(entry.get("description", "")),
            str(entry.get("source_transcript", "")),
            label_text,
        )
    )


def _narrative_intent(sentence: str) -> tuple[frozenset[str], str, str | None]:
    intent: list[str] = []
    roles: list[str] = []
    for role, markers, visual_terms in _NARRATIVE_INTENTS:
        if any(marker in sentence for marker in markers):
            roles.append(role)
            intent.extend(visual_terms)
    time_shift = (
        "flashback"
        if any(marker in sentence for marker in _BACKWARD_TIME_MARKERS)
        else "forward"
        if any(marker in sentence for marker in _FORWARD_TIME_MARKERS)
        else None
    )
    if time_shift == "flashback":
        intent.extend(("过去", "回忆", "年轻"))
    elif time_shift == "forward":
        intent.extend(("成年", "现在", "后来"))
    return frozenset(intent), roles[0] if roles else "continuation", time_shift


def _semantic_match(fragment: _Fragment, entry: Mapping[str, Any]) -> float:
    asset_tokens = _semantic_tokens(_asset_text(entry))
    narration_tokens = _semantic_tokens(fragment.narration)
    narration_overlap = len(narration_tokens & asset_tokens) / max(1, len(narration_tokens))
    intent_tokens = _semantic_tokens(" ".join(fragment.intent))
    intent_overlap = len(intent_tokens & asset_tokens) / max(1, len(intent_tokens))
    return narration_overlap + intent_overlap * 0.7


def _plan_asset_path(
    fragments: Sequence[_Fragment], assets: Sequence[Mapping[str, Any]]
) -> tuple[list[int], list[float]]:
    """Find one coherent asset sequence instead of making isolated greedy choices.

    The storyboard order is a soft prior rather than a hard constraint: explicit
    semantic evidence can override it, while time-shift language controls whether
    moving backwards or forwards through the source story is narratively justified.
    """

    count = len(assets)
    if not fragments or not count:
        return [], []
    scores: list[list[float]] = []
    parents: list[list[int | None]] = []
    for ordinal, fragment in enumerate(fragments):
        row: list[float] = []
        row_parents: list[int | None] = []
        eligible = _eligible_asset_indexes(fragment, assets)
        for asset_index, entry in enumerate(assets):
            emission = (
                _emission_score(fragment, entry, asset_index, count)
                if asset_index in eligible
                else -1_000_000.0
            )
            if ordinal == 0:
                row.append(emission)
                row_parents.append(None)
                continue
            ranked = [
                (
                    scores[ordinal - 1][previous]
                    + _transition_score(
                        assets[previous],
                        entry,
                        previous,
                        asset_index,
                        fragment,
                        count,
                    ),
                    previous,
                )
                for previous in range(count)
            ]
            previous_score, parent = max(ranked, key=lambda value: (value[0], -value[1]))
            row.append(previous_score + emission)
            row_parents.append(parent)
        scores.append(row)
        parents.append(row_parents)

    last = max(range(count), key=lambda index: (scores[-1][index], -index))
    path = [last]
    for ordinal in range(len(fragments) - 1, 0, -1):
        parent = parents[ordinal][path[-1]]
        path.append(0 if parent is None else parent)
    path.reverse()
    incremental = [scores[0][path[0]]]
    for ordinal in range(1, len(path)):
        incremental.append(scores[ordinal][path[ordinal]] - scores[ordinal - 1][path[ordinal - 1]])
    return path, incremental


def _align_storyboard_scenes(
    sentences: Sequence[str],
    scenes: Sequence[str],
    *,
    narration_hints: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Viterbi-align narration to story beats without moving backwards."""

    if not sentences or not scenes:
        return ()
    if len(scenes) == 1:
        return (scenes[0],) * len(sentences)
    scores: list[list[float]] = []
    parents: list[list[int | None]] = []
    for sentence_index, sentence in enumerate(sentences):
        row: list[float] = []
        row_parents: list[int | None] = []
        sentence_position = sentence_index / max(1, len(sentences) - 1)
        for scene_index, scene in enumerate(scenes):
            scene_position = scene_index / max(1, len(scenes) - 1)
            overlap = semantic_similarity(sentence, scene)
            hint_score = max(
                (
                    semantic_similarity(sentence, hint)
                    for hint in (narration_hints or {}).get(scene, ())
                ),
                default=0.0,
            )
            emission = (
                overlap * 5.6
                + hint_score * 7.0
                - abs(sentence_position - scene_position) * 0.32
            )
            if sentence_index == 0:
                row.append(emission - scene_index * 0.45)
                row_parents.append(None)
                continue
            candidates = [
                (
                    scores[sentence_index - 1][previous]
                    - max(0, scene_index - previous - 1) * 0.2,
                    previous,
                )
                for previous in range(scene_index + 1)
            ]
            previous_score, parent = max(
                candidates,
                key=lambda value: (value[0], -value[1]),
            )
            row.append(previous_score + emission)
            row_parents.append(parent)
        scores.append(row)
        parents.append(row_parents)
    last = max(range(len(scenes)), key=lambda index: (scores[-1][index], index))
    path = [last]
    for sentence_index in range(len(sentences) - 1, 0, -1):
        parent = parents[sentence_index][path[-1]]
        path.append(0 if parent is None else parent)
    path.reverse()
    return tuple(scenes[index] for index in path)


def _eligible_asset_indexes(
    fragment: _Fragment,
    assets: Sequence[Mapping[str, Any]],
) -> frozenset[int]:
    """Keep sentence-level editing inside the scene-level semantic decision."""

    if not fragment.storyboard_scene:
        return frozenset(range(len(assets)))
    exact = frozenset(
        index
        for index, entry in enumerate(assets)
        if str(entry.get("selected_for_scene", "")).strip()
        == fragment.storyboard_scene
    )
    return exact or frozenset(range(len(assets)))


def _emission_score(
    fragment: _Fragment,
    entry: Mapping[str, Any],
    asset_index: int,
    asset_count: int,
) -> float:
    semantic = _semantic_match(fragment, entry) * 2.4
    evidence = (
        0.22
        if entry.get("semantic_complete") is True
        else 0.11
        if entry.get("cut_safe") is True
        else -0.22
    )
    story_position = (
        fragment.sentence_index / max(1, fragment.sentence_count - 1)
        if fragment.sentence_count > 1
        else 0.5
    )
    asset_position = asset_index / max(1, asset_count - 1)
    position_penalty = abs(story_position - asset_position) * 0.28
    available = max(0.0, (_number(entry.get("end_ms")) - _number(entry.get("start_ms"))) / 1000)
    padding_penalty = (
        min(0.3, (fragment.duration - available) / max(fragment.duration, 0.001) * 0.3)
        if available and available < fragment.duration
        else 0.0
    )
    return semantic + evidence - position_penalty - padding_penalty


def _transition_score(
    previous_entry: Mapping[str, Any],
    entry: Mapping[str, Any],
    previous: int,
    current: int,
    fragment: _Fragment,
    asset_count: int,
) -> float:
    if asset_count <= 1:
        return 0.0
    delta = current - previous
    # Repetition is undesirable, but a semantically wrong cut is worse than
    # holding a relevant shot until the narration reaches the next story beat.
    score = -0.25 if delta == 0 else 0.03
    if delta < 0:
        score -= abs(delta) * (0.04 if fragment.time_shift == "flashback" else 0.28)
        if fragment.time_shift == "flashback":
            score += 0.22
    elif delta > 0:
        score -= max(0, delta - 2) * 0.08
        if fragment.time_shift == "forward":
            score += min(0.28, delta * 0.08)
    if _visual_identity(previous_entry) == _visual_identity(entry):
        score -= 0.12
    if _chronological_continuation(previous_entry, entry):
        score += 0.16
    return score


def _visual_identity(entry: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(entry.get("asset_id", "")),
            " ".join(str(entry.get("description", "")).lower().split()),
        )
    )


def _chronological_continuation(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    if not previous.get("asset_id") or previous.get("asset_id") != current.get("asset_id"):
        return False
    previous_end = _number(previous.get("end_ms"))
    current_start = _number(current.get("start_ms"))
    return previous_end <= current_start <= previous_end + 30_000


def _transition_label(
    previous: int | None, current: int, fragment: _Fragment
) -> str:
    if previous is None:
        return "opening"
    if fragment.time_shift == "flashback":
        return "flashback"
    if fragment.time_shift == "forward":
        return "time_forward"
    if current == previous:
        return "hold"
    return "continuity_cut"


def _source_window(
    entry: Mapping[str, Any],
    shot_duration: float,
    reuse: int,
) -> tuple[float, float]:
    """Return a deterministic window that never escapes the indexed segment.

    Asset ingestion already creates virtual clips as ``start_ms``/``end_ms``
    ranges.  Renderers must treat those ranges as hard boundaries: reading past
    ``end_ms`` can silently move into a different line, scene, or character.
    A short virtual clip is therefore reported as short and the renderer may
    hold its final frame; it must not continue into unselected source footage.
    """

    start = _number(entry.get("start_ms")) / 1000
    end = _number(entry.get("end_ms")) / 1000
    if end <= start:
        safe_start = max(0.0, start)
        return safe_start, safe_start + max(0.0, shot_duration)
    available = max(0.0, end - start - shot_duration)
    if available <= 0:
        return max(0.0, start), max(start, end)
    # Deterministic offsets prevent repeated use from showing the same opening frames.
    fraction = ((reuse * 0.38196601125) % 1.0) if reuse else 0.0
    source_start = max(0.0, start + available * fraction)
    return source_start, min(end, source_start + shot_duration)


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _motion_focus(value: object) -> dict[str, float]:
    focus = value if isinstance(value, Mapping) else {}
    x = _number(focus.get("x", 0.5)) if focus else 0.5
    y = _number(focus.get("y", 0.5)) if focus else 0.5
    return {
        "x": round(min(1.0, max(0.0, x)), 6),
        "y": round(min(1.0, max(0.0, y)), 6),
    }
