"""Bounded, credential-free identities from evidenced Xiaohongshu card sources.

These helpers deliberately do not join cards by title, display order, or timestamp.
Query strings are accepted only while parsing a local link and are never returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

_ID = re.compile(r"^[0-9a-f]{24}$")
_PROFILE_NOTE_PATH = re.compile(r"^/user/profile/([0-9a-f]{24})/([0-9a-f]{24})/?$")
_ORIGIN = "https://www.xiaohongshu.com"


class NoteIdentityError(ValueError):
    """A stable diagnostic that never includes raw provider data or locators."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validated_note_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise NoteIdentityError(
            "BENCHMARK_NOTE_IDENTITY_INVALID", "A note identity has an invalid format"
        )
    return value


def resolve_structured_note_id(raw_item: Any, expected_user_id: str) -> str | None:
    """Resolve a card only when that same structured card verifies its author."""
    if not _ID.fullmatch(expected_user_id):
        raise NoteIdentityError("BENCHMARK_PROFILE_INVALID", "Invalid profile identity")
    if not isinstance(raw_item, Mapping):
        return None
    card = raw_item.get("noteCard")
    if not isinstance(card, Mapping):
        return None
    # Both paths were observed together in the supplied account's authenticated
    # first-page state. Neither is preferred when the two values disagree.
    note_id = reconcile_note_identity(card.get("noteId"), raw_item.get("id"))
    user = card.get("user")
    author = user.get("userId") if isinstance(user, Mapping) else None
    if author not in (None, "", expected_user_id):
        raise NoteIdentityError(
            "BENCHMARK_PROFILE_MISMATCH", "A note card belongs to a different account"
        )
    return note_id if author == expected_user_id else None


def parse_profile_note_link(href: str, expected_user_id: str) -> str | None:
    """Read an exact same-origin author-scoped card path; never emit its query."""
    if not isinstance(href, str) or len(href) > 4096:
        return None
    try:
        parsed = urlsplit(urljoin(_ORIGIN, href))
        match = _PROFILE_NOTE_PATH.fullmatch(parsed.path)
        if match is None:
            return None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.xiaohongshu.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            raise NoteIdentityError(
                "BENCHMARK_NOTE_IDENTITY_INVALID", "A note card has an untrusted source"
            )
    except ValueError as exc:
        if isinstance(exc, NoteIdentityError):
            raise
        raise NoteIdentityError(
            "BENCHMARK_NOTE_IDENTITY_INVALID", "A note card has an invalid source"
        ) from None
    if match[1] != expected_user_id:
        raise NoteIdentityError(
            "BENCHMARK_PROFILE_MISMATCH", "A note card belongs to a different account"
        )
    return match[2]


def reconcile_note_identity(*sources: str | None) -> str | None:
    identities = {identity for value in sources if (identity := validated_note_id(value))}
    if len(identities) > 1:
        raise NoteIdentityError(
            "BENCHMARK_NOTE_IDENTITY_CONFLICT", "Same-card note identity sources disagree"
        )
    return next(iter(identities), None)


def merge_verified_cards(
    profile_user_id: str, *groups: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Merge sanitized records strictly by author and note; no positional joins.

    A later record replaces earlier metadata only after both identity components
    verify. Unidentified entries cannot be merged and are left to callers to label.
    """
    output: dict[str, dict[str, Any]] = {}
    for group in groups:
        for card in group:
            note_id = validated_note_id(card.get("note_id"))
            if card.get("profile_user_id") != profile_user_id:
                raise NoteIdentityError(
                    "BENCHMARK_PROFILE_MISMATCH", "Discovery returned a different account"
                )
            if note_id is None:
                continue
            # Explicit allowlist protects the public boundary even if a provider
            # later accidentally adds a URL, response object, or token field.
            clean = {"note_id": note_id, "profile_user_id": profile_user_id}
            for key, limit in (("title", 300), ("likes_display", 40)):
                value = card.get(key)
                if isinstance(value, str):
                    clean[key] = value[:limit]
            clean["format"] = "unknown"
            clean["published_at"] = None
            output[note_id] = {**output.get(note_id, {}), **clean}
            if len(output) > 64:
                raise NoteIdentityError(
                    "BENCHMARK_SOURCE_INVALID", "Discovery exceeded the bounded card sample"
                )
    return list(output.values())


# Only the demonstrated section.note-item profile cards participate. The browser
# evaluates URLs locally and exports identities, never hrefs or credential data.
_CARD_DISCOVERY_SCRIPT = r"""({profileUserId, clickNoteId}) => {
  const origin = 'https://www.xiaohongshu.com';
  const profilePath = '/user/profile/' + profileUserId;
  if (location.origin !== origin || location.pathname.replace(/\/$/, '') !== profilePath)
    return {error: 'BENCHMARK_PROFILE_MISMATCH'};
  const records = [];
  for (const card of [...document.querySelectorAll('section.note-item')].slice(0, 64)) {
    const identities = new Map();
    for (const link of [...card.querySelectorAll('a[href]')].slice(0, 16)) {
      let url;
      const href = link.getAttribute('href');
      try { url = new URL(href, origin); } catch { continue; }
      const match = url.pathname.match(/^\/user\/profile\/([0-9a-f]{24})\/([0-9a-f]{24})\/?$/);
      if (!match) continue;
      const authority = href.match(/^(?:https?:)?\/\/([^/?#]+)/);
      if (url.origin !== origin || url.username || url.password || url.port || url.hash ||
          (authority && authority[1].includes(':')))
        return {error: 'BENCHMARK_NOTE_IDENTITY_INVALID'};
      if (match[1] !== profileUserId) return {error: 'BENCHMARK_PROFILE_MISMATCH'};
      if (!identities.has(match[2]) || link.getClientRects().length)
        identities.set(match[2], link);
    }
    if (identities.size > 1) return {error: 'BENCHMARK_NOTE_IDENTITY_CONFLICT'};
    if (!identities.size) continue;
    const [noteId, link] = identities.entries().next().value;
    if (clickNoteId) {
      if (noteId === clickNoteId && link.getClientRects().length) {
        link.click();
        return {clicked: true};
      }
      continue;
    }
    records.push({profile_user_id: profileUserId, note_id: noteId,
      title: (card.querySelector('.title')?.textContent || '').trim().slice(0, 300),
      likes_display: (card.querySelector('.count')?.textContent || '').trim().slice(0, 40),
      format: 'unknown', published_at: null});
  }
  return clickNoteId ? {clicked: false} : {cards: records};
}"""


def discover_profile_note_links(page: Any, expected_user_id: str) -> list[dict[str, Any]]:
    payload = _evaluate_profile_cards(page, expected_user_id, None)
    cards = payload.get("cards")
    if not isinstance(cards, list) or any(not isinstance(card, Mapping) for card in cards):
        raise NoteIdentityError("BENCHMARK_SOURCE_CHANGED", "Invalid profile card discovery")
    return merge_verified_cards(expected_user_id, cards)


def click_verified_profile_note(page: Any, expected_user_id: str, note_id: str) -> bool:
    if validated_note_id(note_id) is None:
        raise NoteIdentityError("BENCHMARK_NOTE_IDENTITY_INVALID", "Missing note identity")
    return _evaluate_profile_cards(page, expected_user_id, note_id).get("clicked") is True


def _evaluate_profile_cards(page: Any, profile_user_id: str, note_id: str | None) -> dict:
    if not _ID.fullmatch(profile_user_id):
        raise NoteIdentityError("BENCHMARK_PROFILE_INVALID", "Invalid profile identity")
    payload = page.evaluate(
        _CARD_DISCOVERY_SCRIPT, {"profileUserId": profile_user_id, "clickNoteId": note_id}
    )
    if not isinstance(payload, dict):
        raise NoteIdentityError("BENCHMARK_SOURCE_CHANGED", "Invalid profile card discovery")
    code = payload.get("error")
    if code:
        allowed = {
            "BENCHMARK_PROFILE_MISMATCH",
            "BENCHMARK_NOTE_IDENTITY_INVALID",
            "BENCHMARK_NOTE_IDENTITY_CONFLICT",
        }
        raise NoteIdentityError(
            code if code in allowed else "BENCHMARK_SOURCE_CHANGED",
            "The profile card identities could not be verified",
        )
    return payload
