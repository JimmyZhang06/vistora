"""Reusable coverage-driven query planning for automatic media acquisition."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_TIMING = re.compile(
    r"(?:持续|时长|约)\s*\d+(?:\.\d+)?\s*秒|"
    r"\d{1,2}(?::\d{2}){1,2}(?:\.\d{1,3})?"
)
_DIRECTION = re.compile(
    r"(?:镜头|画面)(?:切换|切至|转为|转向|聚焦|缓慢拉远|淡出|定格|显示|捕捉)?(?:于|至|为)?|"
    r"背景(?:为|是)?|文字浮现|黑底白字|慢动作|特写|近景|中景|远景|全景"
)
_OVERLAY = re.compile(
    r"(?:字幕|标题|文字|时间轴)(?:显示|标注|写着|为)?\s*[:：]?[‘“\"']?.*$"
)
_PUNCTUATION = re.compile(r"[，。！？；：、,.!?;:‘’“”\"'（）()【】\[\]]+")
_YEAR = re.compile(r"(?:19|20)\d{2}年?")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,30}")
_QUOTED = re.compile(r"[《【‘“\"']([^》】’”\"']{2,40})[》】’”\"']")


@dataclass(frozen=True, slots=True)
class AcquisitionSlot:
    """One story beat that must be represented by independently sourced media."""

    key: str
    scene: str
    query: str
    evidence_terms: tuple[str, ...]


def plan_acquisition_slots(
    topic: str,
    missing_scenes: Sequence[str],
    *,
    limit: int = 12,
) -> tuple[AcquisitionSlot, ...]:
    """Create diverse, scene-specific searches instead of generic topic searches.

    At most one primary query is emitted per missing story beat before any
    fallback query. This lets the acquisition API distribute its download
    budget across the narrative rather than filling it with several variants
    of the first successful search.
    """

    maximum = max(1, min(12, limit))
    subject = _subject(topic)
    slots: list[AcquisitionSlot] = []
    seen_queries: set[str] = set()
    for ordinal, scene in enumerate(missing_scenes):
        cleaned = _clean_scene(scene)
        evidence = _evidence_terms(cleaned)
        scene_subject = _scene_subject(cleaned)
        # Preserve the authored visual request. Translation, embeddings and
        # provider-specific syntax belong behind a provider boundary rather
        # than in a production dictionary of known subjects.
        query = cleaned
        if scene_subject and not query.startswith(scene_subject):
            query = f"{scene_subject} {query}"
        elif subject and len(query) < 8 and subject not in query:
            query = f"{subject} {query}"
        if not query:
            query = subject
        normalized = " ".join(query.casefold().split())
        if not normalized or normalized in seen_queries:
            continue
        seen_queries.add(normalized)
        slots.append(
            AcquisitionSlot(
                key=f"scene-{ordinal + 1:02d}",
                scene=scene,
                query=query[:500],
                evidence_terms=evidence,
            )
        )
        if len(slots) >= maximum:
            return tuple(slots)

    # A topic-level fallback is intentionally last. It can fill atmosphere or
    # transitions, but it must never consume the whole budget before the event
    # and action slots have been searched.
    fallback = " ".join(part for part in (subject, "相关画面") if part).strip()
    if fallback and " ".join(fallback.casefold().split()) not in seen_queries:
        slots.append(
            AcquisitionSlot(
                key="topic-fallback",
                scene=topic,
                query=fallback[:500],
                evidence_terms=tuple(_evidence_terms(topic)),
            )
        )
    return tuple(slots[:maximum])


def _subject(topic: str) -> str:
    value = re.split(r"[，,:：。！？!?；;｜|—–]", topic, maxsplit=1)[0].strip()
    value = re.split(r"(?:从|如何|为什么)", value, maxsplit=1)[0].strip()
    return value.strip("《》【】[]()（）‘’“”\"' ")[:80]


def _clean_scene(scene: str) -> str:
    years = _YEAR.findall(scene)
    value = _OVERLAY.sub(" ", scene)
    value = _TIMING.sub(" ", value)
    value = _DIRECTION.sub(" ", value)
    cleaned = " ".join(_PUNCTUATION.sub(" ", value).split())
    return " ".join(dict.fromkeys((cleaned, *years)))


def _evidence_terms(value: str) -> tuple[str, ...]:
    years = _YEAR.findall(value)
    latin = _LATIN.findall(value)
    quoted = _QUOTED.findall(value)
    # Keep authored phrases as evidence. This deliberately avoids a central
    # list of sports, people, places or scientific concepts.
    chunks = [
        chunk
        for chunk in value.split()
        if 2 <= len(chunk) <= 12
        and chunk not in {"持续", "背景", "画面", "镜头"}
        and not (
            _scene_subject(value)
            and chunk.startswith(_scene_subject(value))
        )
    ]
    return tuple(
        dict.fromkeys((*years, *quoted, *latin, *chunks[:3]))
    )[:8]


def _scene_subject(value: str) -> str:
    match = re.match(
        r"(?P<subject>[\u3400-\u9fff]{2,6})(?=在|进行|参加|出席|获得|赢得|夺得)",
        value,
    )
    return match.group("subject") if match else ""
