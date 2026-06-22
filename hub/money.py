"""Canonical ns-first internal money path (PR 4 legacy adapter layer).

This module is the single internal path that financial endpoints — both the
legacy float surface and the v2 ns surface — read money through. The integer
nanosecond column is the source of truth; legacy float values are produced only
at the response boundary via :func:`nanoseconds.ns_to_legacy_seconds`.

Design-lock acceptance criterion (issues #223/#224, PR #225):

    "Legacy financial endpoint handlers contain no direct financial float
    arithmetic. Money operations must route through the canonical ns-first
    helpers/internal path, with any legacy float translation confined to
    request/response boundary adaptation."

Endpoints call into this module instead of doing their own seconds<->ns math,
so the canonical value is read from the ns column, not recomputed from floats.
"""

from __future__ import annotations

from typing import Optional

import db
from nanoseconds import (
    NanosecondsError,
    format_ns_string,
    legacy_seconds_to_ns,
    ns_to_legacy_seconds,
)


def canonical_ns(
    legacy_value,
    ns_value,
    field_name: str,
    *,
    allow_negative: bool,
) -> Optional[int]:
    """Resolve the canonical integer-ns amount for a financial row.

    Prefers the ns column (source of truth). For pre-migration rows whose ns
    column is still NULL and whose legacy float is not exactly representable in
    nanoseconds, returns ``None`` so the caller can degrade gracefully at the
    boundary rather than raising. A populated-but-invalid ns column still
    raises, because that is a storage-integrity problem, not a migration gap.
    """

    if ns_value is None and legacy_value is None:
        return None
    try:
        return db.coerce_ns_from_row(
            legacy_value, ns_value, field_name, allow_negative=allow_negative
        )
    except NanosecondsError:
        if ns_value is not None:
            # ns column is populated but malformed — surface the integrity error.
            raise
        # ns column absent and legacy float is not exact ns: no canonical truth.
        return None


def to_legacy_seconds(
    legacy_value,
    ns_value,
    field_name: str,
    *,
    allow_negative: bool,
) -> float:
    """Legacy float boundary value derived from the canonical ns source.

    When a canonical ns value exists it is converted to float here (the only
    place float translation is allowed). Pre-migration rows without a canonical
    ns value fall back to the stored legacy float unchanged.
    """

    ns = canonical_ns(legacy_value, ns_value, field_name, allow_negative=allow_negative)
    if ns is None:
        return legacy_value
    return ns_to_legacy_seconds(ns)


def to_v2_ns_string(
    legacy_value,
    ns_value,
    field_name: str,
    *,
    allow_negative: bool,
) -> str:
    """Canonical v2 ns JSON string derived from the ns source of truth.

    Falls back to an exact boundary conversion of the legacy float only for
    pre-migration rows whose ns column is still NULL.
    """

    ns = canonical_ns(legacy_value, ns_value, field_name, allow_negative=allow_negative)
    if ns is None:
        ns = legacy_seconds_to_ns(legacy_value, field_name)
    return format_ns_string(ns)


def balance_ns(node_id: str) -> Optional[int]:
    """Canonical balance in integer nanoseconds, or None if the node is unknown."""

    row = db.get_balance_row(node_id)
    if row is None:
        return None
    balance, balance_ns_value = row
    return canonical_ns(balance, balance_ns_value, "balance", allow_negative=False)
