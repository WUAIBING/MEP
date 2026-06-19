"""Tests for v2 financial API endpoints."""

import pytest
import os
import sys
import tempfile

# Point hub DB at a temp file so tests don't pollute anything
_test_db = os.path.join(tempfile.gettempdir(), "mep_test_v2_hub.db")
os.environ["MEP_SQLITE_PATH"] = _test_db
os.environ.setdefault("MEP_DATABASE_URL", "")  # force SQLite
os.environ.setdefault("MEP_ADMIN_KEY", "test-admin-key")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "hub"))

from hub.v2_models import (
    V2BalanceResponse,
    V2RegistrationResponse,
    V2TaskEconomics,
    V2TaskResponse,
    V2EscrowResponse,
    V2LedgerEntryResponse,
    V2EscrowListResponse,
    V2TaskListResponse,
)
from hub.nanoseconds import validate_ns_string, legacy_seconds_to_ns, ns_to_legacy_seconds
import pytest

class TestV2Models:
    """Test v2 model validation and ns string encoding."""
    
    def test_v2_balance_response_valid(self):
        """Test V2BalanceResponse accepts valid ns string."""
        response = V2BalanceResponse(
            node_id="test-node",
            balance_ns="100000000000"
        )
        assert response.node_id == "test-node"
        assert response.balance_ns == "100000000000"
        assert response.currency == "MEP_NS"
    
    def test_v2_balance_response_invalid_negative(self):
        """Test V2BalanceResponse rejects negative balance."""
        with pytest.raises(ValueError):
            V2BalanceResponse(
                node_id="test-node",
                balance_ns="-100000000000"
            )
    
    def test_v2_task_economics_valid(self):
        """Test V2TaskEconomics accepts valid ns string."""
        economics = V2TaskEconomics(
            bounty_ns="50000000000",
            currency="MEP_NS"
        )
        assert economics.bounty_ns == "50000000000"
        assert economics.currency == "MEP_NS"
    
    def test_v2_task_economics_signed_bounty(self):
        """Test V2TaskEconomics accepts signed bounty."""
        economics = V2TaskEconomics(
            bounty_ns="-50000000000",
            currency="MEP_NS"
        )
        assert economics.bounty_ns == "-50000000000"
    
    def test_v2_escrow_response_valid(self):
        """Test V2EscrowResponse accepts valid ns string."""
        escrow = V2EscrowResponse(
            task_id="task-123",
            consumer_id="consumer-1",
            amount_ns="30000000000",
            status="held"
        )
        assert escrow.amount_ns == "30000000000"
        assert escrow.currency == "MEP_NS"
    
    def test_v2_ledger_entry_response_valid(self):
        """Test V2LedgerEntryResponse accepts valid ns string."""
        entry = V2LedgerEntryResponse(
            node_id="node-1",
            amount_ns="10000000000",
            balance_ns="90000000000",
            kind="ESCROW"
        )
        assert entry.amount_ns == "10000000000"
        assert entry.balance_ns == "90000000000"
        assert entry.currency == "MEP_NS"

class TestNsStringValidation:
    """Test ns string validation logic."""
    
    def test_validate_ns_string_valid_positive(self):
        """Test validate_ns_string accepts valid positive ns string."""
        result = validate_ns_string("100000000000", "test_field")
        assert result == 100000000000
        assert isinstance(result, int)
    
    def test_validate_ns_string_valid_zero(self):
        """Test validate_ns_string accepts zero."""
        result = validate_ns_string("0", "test_field")
        assert result == 0
        assert isinstance(result, int)
    
    def test_validate_ns_string_valid_negative(self):
        """Test validate_ns_string accepts negative when allowed."""
        result = validate_ns_string("-100000000000", "test_field", allow_negative=True)
        assert result == -100000000000
        assert isinstance(result, int)
    
    def test_validate_ns_string_invalid_negative(self):
        """Test validate_ns_string rejects negative when not allowed."""
        with pytest.raises(ValueError):
            validate_ns_string("-100000000000", "test_field", allow_negative=False)
    
    def test_validate_ns_string_invalid_leading_zero(self):
        """Test validate_ns_string rejects leading zeros."""
        with pytest.raises(ValueError):
            validate_ns_string("010000000000", "test_field")
    
    def test_validate_ns_string_invalid_negative_zero(self):
        """Test validate_ns_string rejects -0."""
        with pytest.raises(ValueError):
            validate_ns_string("-0", "test_field")
    
    def test_validate_ns_string_invalid_float(self):
        """Test validate_ns_string rejects float format."""
        with pytest.raises(ValueError):
            validate_ns_string("100.5", "test_field")

class TestNsConversion:
    """Test legacy seconds to ns conversion."""
    
    def test_legacy_seconds_to_ns_positive(self):
        """Test converting positive seconds to ns."""
        result = legacy_seconds_to_ns(100.0, "test_field")
        assert result == 100000000000
    
    def test_legacy_seconds_to_ns_zero(self):
        """Test converting zero to ns."""
        result = legacy_seconds_to_ns(0.0, "test_field")
        assert result == 0
    
    def test_legacy_seconds_to_ns_negative(self):
        """Test converting negative seconds to ns."""
        result = legacy_seconds_to_ns(-50.0, "test_field")
        assert result == -50000000000
    
    def test_ns_to_legacy_seconds_positive(self):
        """Test converting positive ns to seconds."""
        result = ns_to_legacy_seconds(100000000000)
        assert result == 100.0
    
    def test_ns_to_legacy_seconds_zero(self):
        """Test converting zero ns to seconds."""
        result = ns_to_legacy_seconds(0)
        assert result == 0.0
    
    def test_ns_to_legacy_seconds_negative(self):
        """Test converting negative ns to seconds."""
        result = ns_to_legacy_seconds(-50000000000)
        assert result == -50.0
    
    def test_roundtrip_conversion(self):
        """Test that seconds -> ns -> seconds roundtrip is accurate."""
        original = 123.456
        ns = legacy_seconds_to_ns(original, "test_field")
        back = ns_to_legacy_seconds(ns)
        assert abs(back - original) < 0.001  # Allow small floating point differences