"""Deterministic owner-policy checks for autonomous MEP purchases.

AI agents may rank providers and negotiate terms, but they must not move MEP
credits outside an owner's explicit limits.  This module keeps that financial
gate local to the requesting runtime and performs every calculation in integer
``MEP_NS`` units.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence


CURRENCY_MEP_NS = "MEP_NS"
NS_PER_MEP_SECOND = 1_000_000_000
_NS_RE = re.compile(r"^(0|[1-9][0-9]*)$")


class PurchasePolicyError(ValueError):
    """Raised when a purchase policy or MEP_NS amount is malformed."""


def parse_non_negative_ns(value: Any, field_name: str) -> int:
    """Return a canonical non-negative integer MEP_NS amount.

    Protocol JSON uses decimal strings. Internal callers may use integers, but
    floats and booleans are rejected so financial policy never depends on
    floating-point rounding.
    """

    if isinstance(value, bool):
        raise PurchasePolicyError(f"{field_name} must be an integer or canonical decimal string")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _NS_RE.fullmatch(value):
        parsed = int(value)
    else:
        raise PurchasePolicyError(f"{field_name} must be an integer or canonical decimal string")
    if parsed < 0:
        raise PurchasePolicyError(f"{field_name} must be non-negative")
    return parsed


def parse_non_negative_int(value: Any, field_name: str) -> int:
    """Parse a canonical non-negative policy count without coercion."""

    return parse_non_negative_ns(value, field_name)


def format_mep_seconds(amount_ns: int) -> str:
    """Render an internal MEP_NS amount for humans without float arithmetic."""

    value = Decimal(parse_non_negative_ns(amount_ns, "amount_ns")) / Decimal(NS_PER_MEP_SECOND)
    return format(value, "f")


@dataclass(frozen=True)
class OwnerPurchasePolicy:
    """Hard local limits for autonomous provider hiring.

    ``max_total_price_ns`` and ``max_price_per_provider_ns`` are hard limits;
    human approval does not silently override them. ``human_approval_above_ns``
    is an optional softer boundary within those limits.
    """

    max_total_price_ns: int
    max_price_per_provider_ns: int
    minimum_reserve_ns: int = 0
    human_approval_above_ns: int | None = None
    max_bargaining_rounds: int = 2
    currency: str = CURRENCY_MEP_NS

    def __post_init__(self) -> None:
        if self.currency != CURRENCY_MEP_NS:
            raise PurchasePolicyError("currency must be MEP_NS")
        for field_name in (
            "max_total_price_ns",
            "max_price_per_provider_ns",
            "minimum_reserve_ns",
        ):
            object.__setattr__(
                self,
                field_name,
                parse_non_negative_ns(getattr(self, field_name), field_name),
            )
        if self.human_approval_above_ns is not None:
            object.__setattr__(
                self,
                "human_approval_above_ns",
                parse_non_negative_ns(
                    self.human_approval_above_ns,
                    "human_approval_above_ns",
                ),
            )
        if isinstance(self.max_bargaining_rounds, bool) or not isinstance(
            self.max_bargaining_rounds, int
        ):
            raise PurchasePolicyError("max_bargaining_rounds must be an integer")
        if self.max_bargaining_rounds < 0:
            raise PurchasePolicyError("max_bargaining_rounds must be non-negative")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OwnerPurchasePolicy":
        currency = str(raw.get("currency") or CURRENCY_MEP_NS)
        max_total = parse_non_negative_ns(
            raw.get("max_total_price_ns", 0),
            "max_total_price_ns",
        )
        max_per_provider = parse_non_negative_ns(
            raw.get("max_price_per_provider_ns", max_total),
            "max_price_per_provider_ns",
        )
        approval_raw = raw.get("human_approval_above_ns")
        approval_threshold = (
            None
            if approval_raw is None
            else parse_non_negative_ns(approval_raw, "human_approval_above_ns")
        )
        bargaining_raw = raw.get("max_bargaining_rounds", 2)
        bargaining_rounds = parse_non_negative_int(
            bargaining_raw,
            "max_bargaining_rounds",
        )
        return cls(
            max_total_price_ns=max_total,
            max_price_per_provider_ns=max_per_provider,
            minimum_reserve_ns=parse_non_negative_ns(
                raw.get("minimum_reserve_ns", 0),
                "minimum_reserve_ns",
            ),
            human_approval_above_ns=approval_threshold,
            max_bargaining_rounds=bargaining_rounds,
            currency=currency,
        )

    def as_wire_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "max_total_price_ns",
            "max_price_per_provider_ns",
            "minimum_reserve_ns",
        ):
            payload[key] = str(payload[key])
        if payload["human_approval_above_ns"] is not None:
            payload["human_approval_above_ns"] = str(payload["human_approval_above_ns"])
        return payload


@dataclass(frozen=True)
class PurchaseDecision:
    status: str
    reason: str
    balance_ns: int
    total_price_ns: int
    minimum_reserve_ns: int
    spendable_balance_ns: int
    remaining_balance_ns: int
    provider_count: int
    full_rounds_affordable: int
    currency: str = CURRENCY_MEP_NS

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def as_wire_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "balance_ns",
            "total_price_ns",
            "minimum_reserve_ns",
            "spendable_balance_ns",
            "remaining_balance_ns",
        ):
            payload[key] = str(payload[key])
        return payload


def evaluate_purchase(
    *,
    balance_ns: Any,
    provider_prices_ns: Sequence[Any],
    policy: OwnerPurchasePolicy,
    bargaining_round: int = 0,
    human_approved: bool = False,
) -> PurchaseDecision:
    """Evaluate an AI-selected set of provider quotes against owner policy."""

    balance = parse_non_negative_ns(balance_ns, "balance_ns")
    prices = tuple(
        parse_non_negative_ns(value, f"provider_prices_ns[{index}]")
        for index, value in enumerate(provider_prices_ns)
    )
    if not prices:
        raise PurchasePolicyError("provider_prices_ns must contain at least one quote")
    if any(price == 0 for price in prices):
        raise PurchasePolicyError("paid provider quotes must be greater than zero")
    if isinstance(bargaining_round, bool) or not isinstance(bargaining_round, int):
        raise PurchasePolicyError("bargaining_round must be an integer")
    if bargaining_round < 0:
        raise PurchasePolicyError("bargaining_round must be non-negative")
    if not isinstance(human_approved, bool):
        raise PurchasePolicyError("human_approved must be a boolean")

    total_price = sum(prices)
    spendable = max(0, balance - policy.minimum_reserve_ns)
    rounds = spendable // total_price
    remaining = max(0, balance - total_price) if total_price <= balance else balance

    def decision(status: str, reason: str) -> PurchaseDecision:
        return PurchaseDecision(
            status=status,
            reason=reason,
            balance_ns=balance,
            total_price_ns=total_price,
            minimum_reserve_ns=policy.minimum_reserve_ns,
            spendable_balance_ns=spendable,
            remaining_balance_ns=remaining,
            provider_count=len(prices),
            full_rounds_affordable=rounds,
        )

    if bargaining_round > policy.max_bargaining_rounds:
        return decision("rejected", "bargaining_round_limit_exceeded")
    if any(price > policy.max_price_per_provider_ns for price in prices):
        return decision("rejected", "per_provider_price_limit_exceeded")
    if total_price > policy.max_total_price_ns:
        return decision("rejected", "total_price_limit_exceeded")
    if total_price > spendable:
        return decision("rejected", "insufficient_spendable_balance")
    if (
        policy.human_approval_above_ns is not None
        and total_price > policy.human_approval_above_ns
        and not human_approved
    ):
        return decision("approval_required", "human_approval_threshold_exceeded")
    return decision("approved", "within_owner_policy")
