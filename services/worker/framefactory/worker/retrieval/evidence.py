"""Independent verification of serialized retrieval evidence.

The retrieval capability emits convenient booleans such as ``passed`` and
``verified``.  Downstream editing stages must recompute those claims from the
serialized evidence instead of trusting the booleans that they are meant to
audit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_ALLOWED_RIGHTS = frozenset({"owned", "licensed", "public_domain"})
_LICENSE_EVIDENCE_TYPES = frozenset(
    {
        "verified_license",
        "license_verification",
        "rights_clearance",
        "manual_verification",
        "operator_confirmed_provider_terms",
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


def hard_constraint_evidence_valid(
    beat: Mapping[str, Any],
    candidate: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> bool:
    """Recompute every AND/NOT match from candidate text."""

    must_match = _constraint_checks(evidence.get("must_match"))
    must_not_match = _constraint_checks(evidence.get("must_not_match"))
    if must_match is None or must_not_match is None:
        return False
    expected_must = _terms(beat.get("must_match"))
    expected_must_not = _terms(beat.get("must_not_match"))
    haystack = " ".join(
        (
            str(candidate.get("title") or ""),
            str(candidate.get("description") or ""),
            str(candidate.get("source_transcript") or ""),
            *_terms(candidate.get("labels")),
        )
    ).casefold()
    recomputed_must = tuple((term, term.casefold() in haystack) for term in expected_must)
    recomputed_must_not = tuple(
        (term, term.casefold() in haystack) for term in expected_must_not
    )
    return (
        evidence.get("passed") is True
        and must_match == recomputed_must
        and must_not_match == recomputed_must_not
        and all(matched for _term, matched in recomputed_must)
        and not any(matched for _term, matched in recomputed_must_not)
    )


def rights_evidence_valid(evidence: Mapping[str, Any]) -> bool:
    """Recompute the serialized rights verdict using retrieval's exact rules."""

    status = str(evidence.get("copyright_status") or "").casefold().strip()
    sources_raw = evidence.get("sources")
    if not isinstance(sources_raw, Sequence) or isinstance(sources_raw, (str, bytes)):
        return False
    if any(not isinstance(item, Mapping) for item in sources_raw):
        return False
    sources = tuple(dict(item) for item in sources_raw if _nonempty_source(item))
    expected: dict[str, Any] = {
        "status_allowed": status in _ALLOWED_RIGHTS,
        "evidence_required": status in {"licensed", "public_domain"},
        "evidence_present": bool(sources),
        "verified": False,
        "verification_basis": [],
        "rejection_codes": [],
    }
    if status == "owned":
        expected["verified"] = True
        expected["verification_basis"] = ["copyright_status_owned"]
    elif status not in _ALLOWED_RIGHTS:
        expected["rejection_codes"] = ["rights_not_allowed"]
    elif not sources:
        expected["rejection_codes"] = ["rights_evidence_missing"]
    elif status == "licensed":
        claims = tuple(item for item in sources if _meaningful_license(item))
        if not claims:
            expected["rejection_codes"] = ["rights_evidence_missing"]
        elif not any(
            _verified_evidence(item, _LICENSE_EVIDENCE_TYPES) for item in claims
        ):
            expected["rejection_codes"] = ["rights_evidence_unverified"]
        else:
            expected["verified"] = True
            expected["verification_basis"] = (
                ["operator_confirmed_provider_terms_snapshot"]
                if any(
                    _evidence_type(item) == "operator_confirmed_provider_terms"
                    for item in claims
                )
                else ["verified_license_evidence"]
            )
    else:
        claims = tuple(item for item in sources if _public_domain_claim(item))
        if not claims:
            expected["rejection_codes"] = ["rights_evidence_missing"]
        elif not any(
            _verified_evidence(item, _PUBLIC_DOMAIN_EVIDENCE_TYPES)
            or _official_public_domain_locator(item)
            for item in claims
        ):
            expected["rejection_codes"] = ["rights_evidence_unverified"]
        else:
            expected["verified"] = True
            expected["verification_basis"] = ["verified_public_domain_evidence"]
    return all(evidence.get(key) == value for key, value in expected.items())


def _constraint_checks(value: object) -> tuple[tuple[str, bool], ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    result: list[tuple[str, bool]] = []
    for check in value:
        if not isinstance(check, Mapping):
            return None
        term = str(check.get("term") or "").strip()
        matched = check.get("matched")
        if not term or not isinstance(matched, bool):
            return None
        result.append((term, matched))
    return tuple(result)


def _terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _nonempty_source(source: Mapping[str, Any]) -> bool:
    return bool(source) and any(
        value is not None and str(value).strip() not in {"", "{}", "[]"}
        for value in source.values()
    )


def _meaningful_license(source: Mapping[str, Any]) -> bool:
    if _evidence_type(source) == "operator_confirmed_provider_terms":
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        basis = str(metadata.get("output_rights_license_basis") or "").strip()
        terms_hash = str(metadata.get("terms_content_hash") or "")
        return (
            metadata.get("output_rights_confirmed") is True
            and 0 < len(basis) <= 1_000
            and re.fullmatch(r"[a-f0-9]{64}", terms_hash) is not None
            and terms_hash == str(source.get("content_hash") or "")
        )
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
    return bool(str(source.get("verified_at") or "").strip()) or _evidence_type(
        source
    ) in trusted_types


def _evidence_type(source: Mapping[str, Any]) -> str:
    return re.sub(
        r"[^a-z0-9]+", "_", str(source.get("evidence_type") or "").casefold()
    ).strip("_")


def _public_domain_claim(source: Mapping[str, Any]) -> bool:
    if _evidence_type(source) in {"public_domain", "public_domain_mark"}:
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
