"""Domain-neutral text features shared by retrieval and timeline planning."""
from __future__ import annotations

import re
from collections.abc import Iterable

_LATIN_OR_NUMBER = re.compile(r"[a-z][a-z0-9_-]{1,31}|(?:19|20)\d{2}", re.IGNORECASE)
_CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]{2,}")
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])\s*")
_CLAUSE_BREAK = re.compile(r"[，,]")
_TRANSITION = re.compile(
    r"^(?:随后|然后|接着|与此同时|后来|最终|因此|当|直到|于是|转而|另一边|"
    r"after\b|then\b|meanwhile\b|later\b|finally\b|when\b|until\b|therefore\b)",
    re.IGNORECASE,
)
_GENERIC_TOKENS = {
    "画面",
    "镜头",
    "显示",
    "出现",
    "随后",
    "然后",
    "最终",
    "相关",
    "视频",
    "scene",
    "shot",
    "video",
}


def semantic_tokens(value: object) -> frozenset[str]:
    """Return language-agnostic lexical evidence without a topic dictionary."""

    text = str(value or "").casefold()
    tokens = set(_LATIN_OR_NUMBER.findall(text))
    for sequence in _CJK_SEQUENCE.findall(text):
        for size in (2, 3, 4):
            tokens.update(
                sequence[index : index + size]
                for index in range(max(0, len(sequence) - size + 1))
            )
    return frozenset(token for token in tokens if token not in _GENERIC_TOKENS)


def semantic_similarity(left: object, right: object) -> float:
    """Compute weighted lexical overlap for any subject or language.

    Longer n-grams carry more evidence than isolated bigrams. The function is
    deterministic and contains no people, event, genre, language or provider
    vocabulary, so adding a new content vertical does not require code changes.
    """

    left_tokens = semantic_tokens(left)
    right_tokens = semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    shared_weight = sum(_token_weight(token) for token in shared)
    left_weight = sum(_token_weight(token) for token in left_tokens)
    right_weight = sum(_token_weight(token) for token in right_tokens)
    return 2 * shared_weight / max(1.0, left_weight + right_weight)


def split_atomic_visual_beats(value: str) -> tuple[str, ...]:
    """Split a storyboard into independently retrievable chronological beats.

    Sentence/semicolon boundaries are always safe. Commas split only when a
    transition starts a new clause or two substantial clauses share little
    lexical evidence. This is intentionally generic: the decision is based on
    structure and evidence, never on a catalog of known topics.
    """

    sentences = [item.strip() for item in _SENTENCE_BREAK.split(value) if item.strip()]
    result: list[str] = []
    for sentence in sentences or [value.strip()]:
        clauses = [item.strip() for item in _CLAUSE_BREAK.split(sentence) if item.strip()]
        if len(clauses) <= 1:
            if sentence:
                result.append(sentence)
            continue
        group = clauses[0]
        for clause in clauses[1:]:
            independent = bool(_TRANSITION.match(clause))
            if independent:
                result.append(group.rstrip("，,；; ") + "。")
                group = clause
            else:
                group = f"{group}，{clause}"
        if group:
            result.append(group)
    return tuple(dict.fromkeys(item for item in result if item.strip()))


def ordered_visual_beats(
    scenes: Iterable[object], narration: str = ""
) -> tuple[str, ...]:
    """Normalize authored beats, falling back to narration sentences."""

    result = tuple(
        beat
        for raw in scenes
        for beat in split_atomic_visual_beats(_beat_text(raw))
        if beat
    )
    if result:
        return tuple(dict.fromkeys(result))
    return split_atomic_visual_beats(narration)


def _beat_text(value: object) -> str:
    if isinstance(value, dict):
        for key in ("visual_description", "description", "text", "narration"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def _token_weight(token: str) -> float:
    if token.isascii():
        return 2.0 if any(character.isdigit() for character in token) else 1.6
    return min(2.2, max(1.0, len(token) * 0.55))
