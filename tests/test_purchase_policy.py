import pytest

from clients.shared.purchase_policy import (
    OwnerPurchasePolicy,
    PurchasePolicyError,
    evaluate_purchase,
    format_mep_seconds,
    parse_non_negative_ns,
)


def _policy(**overrides):
    values = {
        "max_total_price_ns": "20000000000",
        "max_price_per_provider_ns": "5000000000",
        "minimum_reserve_ns": "0",
        "max_bargaining_rounds": 2,
        "currency": "MEP_NS",
    }
    values.update(overrides)
    return OwnerPurchasePolicy.from_mapping(values)


def test_ns_policy_rejects_float_and_wrong_currency():
    with pytest.raises(PurchasePolicyError, match="canonical decimal string"):
        parse_non_negative_ns(1.5, "price_ns")
    with pytest.raises(PurchasePolicyError, match="currency must be MEP_NS"):
        _policy(currency="SECONDS")


def test_ten_seconds_funds_two_default_single_provider_tasks():
    decision = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["5000000000"],
        policy=_policy(),
    )

    assert decision.approved
    assert decision.full_rounds_affordable == 2
    assert decision.remaining_balance_ns == 5_000_000_000
    assert decision.currency == "MEP_NS"


def test_ten_seconds_cannot_fund_three_default_provider_quotes():
    decision = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["5000000000"] * 3,
        policy=_policy(),
    )

    assert decision.status == "rejected"
    assert decision.reason == "insufficient_spendable_balance"
    assert decision.total_price_ns == 15_000_000_000
    assert decision.full_rounds_affordable == 0


def test_three_one_second_providers_are_autonomously_affordable_with_reserve():
    decision = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["1000000000"] * 3,
        policy=_policy(
            max_total_price_ns="3000000000",
            max_price_per_provider_ns="1000000000",
            minimum_reserve_ns="1000000000",
        ),
    )

    assert decision.status == "approved"
    assert decision.full_rounds_affordable == 3
    assert decision.remaining_balance_ns == 7_000_000_000


def test_human_approval_is_soft_boundary_but_hard_caps_remain():
    policy = _policy(
        max_total_price_ns="5000000000",
        human_approval_above_ns="2000000000",
    )
    pending = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["3000000000"],
        policy=policy,
    )
    approved = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["3000000000"],
        policy=policy,
        human_approved=True,
    )
    over_cap = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["6000000000"],
        policy=policy,
        human_approved=True,
    )

    assert pending.status == "approval_required"
    assert approved.status == "approved"
    assert over_cap.status == "rejected"
    assert over_cap.reason == "per_provider_price_limit_exceeded"


def test_bargaining_round_limit_is_deterministic():
    decision = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["1000000000"],
        policy=_policy(max_bargaining_rounds=2),
        bargaining_round=3,
    )

    assert decision.status == "rejected"
    assert decision.reason == "bargaining_round_limit_exceeded"


def test_policy_rejects_fractional_bargaining_round_limit():
    with pytest.raises(PurchasePolicyError, match="max_bargaining_rounds"):
        _policy(max_bargaining_rounds=2.9)


def test_human_approval_requires_real_boolean():
    with pytest.raises(PurchasePolicyError, match="human_approved must be a boolean"):
        evaluate_purchase(
            balance_ns="10000000000",
            provider_prices_ns=["3000000000"],
            policy=_policy(human_approval_above_ns="2000000000"),
            human_approved="false",
        )


def test_wire_policy_and_decision_keep_ns_as_strings():
    policy = _policy(human_approval_above_ns="2000000000")
    decision = evaluate_purchase(
        balance_ns="10000000000",
        provider_prices_ns=["1000000000"],
        policy=policy,
    )

    assert policy.as_wire_dict()["max_total_price_ns"] == "20000000000"
    assert decision.as_wire_dict()["total_price_ns"] == "1000000000"
    assert format_mep_seconds(10_000_000_000) == "10"
