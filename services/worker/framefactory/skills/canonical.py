"""Canonical JSON primitives used by immutable Skill snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from .errors import SkillValidationError, ValidationIssue

MAX_DOCUMENT_BYTES = 1_048_576
MAX_DEPTH = 32
MAX_NODES = 10_000
MAX_STRING_LENGTH = 200_000


class FrozenMap(Mapping[str, Any]):
    """A small, recursively immutable and deterministically ordered mapping."""

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[tuple[str, Any]] = ()) -> None:
        collected = tuple(items)
        keys: set[str] = set()
        for key, _ in collected:
            if not isinstance(key, str):
                raise TypeError("FrozenMap keys must be strings")
            if key in keys:
                raise ValueError(f"duplicate FrozenMap key: {key}")
            keys.add(key)
        object.__setattr__(self, "_items", tuple(sorted(collected, key=lambda item: item[0])))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("FrozenMap is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("FrozenMap is immutable")

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self._items)!r})"

    def __hash__(self) -> int:
        return hash(self._items)


def normalize_text(value: str, *, strip: bool = False) -> str:
    """Normalize Unicode and line endings without changing interior Markdown."""

    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip() if strip else normalized


def normalize_markdown(value: str) -> str:
    return normalize_text(value, strip=True)


def _raise(path: str, code: str, message: str) -> None:
    raise SkillValidationError(ValidationIssue(path, code, message))


def normalize_json(value: Any, *, path: str = "$", depth: int = 0, counter: list[int] | None = None) -> Any:
    """Copy and normalize a JSON-compatible value, rejecting ambiguous values."""

    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_NODES:
        _raise(path, "too_many_nodes", f"document exceeds {MAX_NODES} JSON nodes")
    if depth > MAX_DEPTH:
        _raise(path, "too_deep", f"document exceeds depth {MAX_DEPTH}")

    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _raise(path, "non_finite_number", "NaN and Infinity are not allowed")
        return value
    if isinstance(value, str):
        normalized = normalize_text(value)
        if len(normalized) > MAX_STRING_LENGTH:
            _raise(path, "string_too_long", f"string exceeds {MAX_STRING_LENGTH} characters")
        if "\x00" in normalized:
            _raise(path, "nul_character", "NUL characters are not allowed")
        return normalized
    if isinstance(value, Mapping):
        normalized_items: list[tuple[str, Any]] = []
        normalized_keys: set[str] = set()
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                _raise(path, "non_string_key", "object keys must be strings")
            key = normalize_text(raw_key)
            if key in normalized_keys:
                _raise(f"{path}.{key}", "normalized_key_collision", "keys collide after Unicode normalization")
            normalized_keys.add(key)
            normalized_items.append(
                (key, normalize_json(child, path=f"{path}.{key}", depth=depth + 1, counter=counter))
            )
        return FrozenMap(normalized_items)
    if isinstance(value, (list, tuple)):
        return tuple(
            normalize_json(child, path=f"{path}[{index}]", depth=depth + 1, counter=counter)
            for index, child in enumerate(value)
        )
    _raise(path, "non_json_value", f"unsupported value type: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    if isinstance(value, FrozenMap):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    normalized = normalize_json(thaw_json(value))
    rendered = json.dumps(
        thaw_json(normalized),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(rendered) > MAX_DOCUMENT_BYTES:
        _raise("$", "document_too_large", f"canonical document exceeds {MAX_DOCUMENT_BYTES} bytes")
    return rendered


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()
