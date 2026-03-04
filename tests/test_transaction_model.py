import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from memorygraph.models import Transaction
from memorygraph.type_registry import get_default_registry, register_custom_types
from memorygraph.query_builder import QueryBuilder


class TestTransactionModel:
    def test_valid_transaction(self):
        t = Transaction(amount=50.0, merchant="Pueblo", category="groceries", date=datetime.now(timezone.utc))
        assert t.amount == 50.0
        assert t.merchant == "Pueblo"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            Transaction(amount=50.0, category="groceries", date=datetime.now(timezone.utc))
            # Missing: merchant

    def test_default_values(self):
        t = Transaction(amount=10, merchant="Test", category="test", date=datetime.now(timezone.utc))
        assert t.currency == "USD"
        assert t.payment_method == "unknown"
        assert t.importance == 0.3

    def test_tags_lowercased(self):
        t = Transaction(amount=10, merchant="Test", category="test",
                        date=datetime.now(timezone.utc), tags=["FOOD", "Weekly"])
        assert t.tags == ["food", "weekly"]

    def test_to_storage_properties(self):
        t = Transaction(amount=42.50, merchant="Pueblo", category="groceries",
                        date=datetime(2026, 3, 3, tzinfo=timezone.utc))
        props = t.to_storage_properties()
        assert props["amount"] == 42.50
        assert props["merchant"] == "Pueblo"
        assert isinstance(props["date"], str)  # ISO string

    def test_naive_date_gets_utc(self):
        """Naive datetime (no timezone) is auto-converted to UTC."""
        naive_dt = datetime(2026, 3, 3, 12, 0, 0)  # no tzinfo
        t = Transaction(amount=10, merchant="Test", category="test", date=naive_dt)
        assert t.date.tzinfo is not None
        assert t.date.tzinfo == timezone.utc


class TestTransactionRegistryIntegration:
    """Verify real Transaction wires into Phase 1 registry and Phase 2 QueryBuilder."""

    def test_register_custom_types_with_real_transaction(self):
        """register_custom_types() registers the real Transaction model."""
        registry = get_default_registry()
        register_custom_types(registry)
        assert registry.has_type("transaction")
        assert registry.get_label("transaction") == "Transaction"
        assert registry.get_model("transaction") == Transaction

    def test_query_builder_with_real_transaction(self):
        """QueryBuilder generates correct Cypher for the real Transaction type."""
        registry = get_default_registry()
        register_custom_types(registry)
        qb = QueryBuilder(registry)
        assert qb.match("transaction") == "MATCH (m:Transaction)"
        assert qb.merge("transaction") == "MERGE (m:Transaction {id: $id})"

    def test_registry_has_both_types(self):
        """Registry holds Memory + Transaction after register_custom_types()."""
        registry = get_default_registry()
        register_custom_types(registry)
        names = [t.name for t in registry.all_types()]
        assert "memory" in names
        assert "transaction" in names
