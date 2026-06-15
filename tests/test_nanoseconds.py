import pytest

from nanoseconds import (
    NanosecondsError,
    format_ns_string,
    legacy_seconds_to_ns,
    ns_to_legacy_seconds,
    ns_to_seconds_decimal_string,
    validate_ns_string,
)
from v2_models import V2BalanceResponse, V2TaskEconomics


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("1", 1),
        ("5000000000", 5_000_000_000),
        ("-5000000000", -5_000_000_000),
    ],
)
def test_validate_ns_string_accepts_canonical_values(value, expected):
    assert validate_ns_string(value, "bounty_ns") == expected


@pytest.mark.parametrize("value", ["", "01", "-0", "1.0", 1, None, "+1"])
def test_validate_ns_string_rejects_non_canonical_values(value):
    with pytest.raises(NanosecondsError):
        validate_ns_string(value, "bounty_ns")


def test_validate_ns_string_can_require_non_negative():
    with pytest.raises(NanosecondsError):
        validate_ns_string("-1", "balance_ns", allow_negative=False)


def test_format_ns_string_rejects_float_source_of_truth():
    assert format_ns_string(5_000_000_000, "bounty_ns") == "5000000000"
    with pytest.raises(NanosecondsError):
        format_ns_string(5.0, "bounty_ns")


def test_legacy_seconds_adapters_are_boundary_only():
    assert legacy_seconds_to_ns(5.0) == 5_000_000_000
    assert legacy_seconds_to_ns("0.001") == 1_000_000
    assert ns_to_legacy_seconds(1_000_000) == 0.001
    assert ns_to_seconds_decimal_string(1_000_000) == "0.001"


def test_v2_schema_models_validate_ns_strings():
    balance = V2BalanceResponse(node_id="node_abc", balance_ns="10000000000")
    assert balance.balance_ns == "10000000000"

    economics = V2TaskEconomics(bounty_ns="-500000000", market="data")
    assert economics.currency == "MEP_NS"

    with pytest.raises(ValueError):
        V2BalanceResponse(node_id="node_abc", balance_ns="01")
