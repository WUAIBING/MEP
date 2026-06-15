"""Canonical nanosecond helpers for MEP financial values.

This module is intentionally centered on the ns-first model. Float helpers are
legacy-boundary adapters only; internal money arithmetic should use integer ns.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

NS_PER_SECOND = 1_000_000_000
NS_STRING_RE = re.compile(r"^(0|-?[1-9][0-9]*)$")


class NanosecondsError(ValueError):
    """Raised when a financial nanosecond value is malformed."""


def _field_label(field_name: str) -> str:
    return field_name or "amount_ns"


def validate_ns_string(value: str, field_name: str = "amount_ns", *, allow_negative: bool = True) -> int:
    """Validate a canonical v2 ns JSON string and return it as an int.

    Encoding rules:
    - JSON type is string
    - value must match ``^(0|-?[1-9][0-9]*)$``
    - no leading zeroes except exactly "0"
    - "-0" is forbidden by the regex
    """

    label = _field_label(field_name)
    if not isinstance(value, str):
        raise NanosecondsError(f"{label} must be a decimal string")
    if not NS_STRING_RE.fullmatch(value):
        raise NanosecondsError(f"{label} must be a canonical integer string")
    parsed = int(value)
    if parsed < 0 and not allow_negative:
        raise NanosecondsError(f"{label} must be non-negative")
    return parsed


def format_ns_string(value: int, field_name: str = "amount_ns") -> str:
    """Format an internal integer ns value as a canonical v2 JSON string."""

    return str(assert_ns_amount(value, field_name))


def assert_ns_amount(value: Any, field_name: str = "amount_ns") -> int:
    """Assert that a value is an integer nanosecond amount for internal use."""

    label = _field_label(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NanosecondsError(f"{label} must be an integer nanosecond amount")
    return value


def legacy_seconds_to_ns(seconds: float | int | str | Decimal, field_name: str = "amount") -> int:
    """Convert a legacy SECONDS value to integer ns at the legacy boundary.

    This helper is not migration truth for existing database rows. Backfills
    should call their own audit path using Decimal(str(value)) and report every
    non-exact legacy value before cleanup.
    """

    label = _field_label(field_name)
    try:
        decimal_seconds = Decimal(str(seconds))
    except (InvalidOperation, ValueError) as exc:
        raise NanosecondsError(f"{label} must be convertible to Decimal seconds") from exc

    ns_value = decimal_seconds * Decimal(NS_PER_SECOND)
    integral = ns_value.to_integral_value(rounding=ROUND_HALF_EVEN)
    return int(integral)


def ns_to_legacy_seconds(ns: int) -> float:
    """Convert integer ns to a legacy float SECONDS response value."""

    return float(Decimal(assert_ns_amount(ns)) / Decimal(NS_PER_SECOND))


def ns_to_seconds_decimal_string(ns: int) -> str:
    """Render integer ns as a human-readable decimal SECONDS string."""

    seconds = Decimal(assert_ns_amount(ns)) / Decimal(NS_PER_SECOND)
    return format(seconds, "f")
