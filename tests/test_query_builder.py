import pytest
from typing import Optional
from pydantic import BaseModel
from memorygraph.query_builder import QueryBuilder, _validate_identifier
from memorygraph.type_registry import NodeTypeRegistry, NodeTypeConfig, get_default_registry


# --- Dummy model for Phase 2 tests (Transaction doesn't exist yet) ---
# DUMMY DATA: QueryBuilder never instantiates or inspects model fields.
# It only reads config.label from the registry. Any BaseModel works.
class DummyTransaction(BaseModel):
    """Stand-in for Transaction. QueryBuilder only needs the label string."""
    id: Optional[str] = None


class TestQueryBuilder:
    def setup_method(self):
        self.registry = get_default_registry()
        # DUMMY DATA: Register a "transaction" type with DummyTransaction
        # instead of calling register_custom_types() which needs real Transaction.
        self.registry.register(NodeTypeConfig(
            name="transaction",
            label="Transaction",
            model=DummyTransaction,
            indexes=["id", "amount", "merchant", "category", "date"],
            fulltext_fields=["merchant", "note"],
        ))
        self.qb = QueryBuilder(self.registry)

    def test_match_memory_unchanged(self):
        assert self.qb.match("memory") == "MATCH (m:Memory)"

    def test_match_transaction(self):
        assert self.qb.match("transaction") == "MATCH (m:Transaction)"

    def test_create_memory(self):
        assert "CREATE (m:Memory" in self.qb.create("memory")

    def test_merge_memory(self):
        assert self.qb.merge("memory") == "MERGE (m:Memory {id: $id})"

    def test_merge_transaction(self):
        assert self.qb.merge("transaction") == "MERGE (m:Transaction {id: $id})"

    def test_custom_alias(self):
        assert self.qb.match("transaction", alias="t") == "MATCH (t:Transaction)"

    def test_return_with_labels(self):
        assert self.qb.return_with_labels() == "RETURN m, labels(m) AS labels"
        assert self.qb.return_with_labels("t") == "RETURN t, labels(t) AS labels"

    def test_search_builds_parameterized_query(self):
        query, params = self.qb.search("transaction", {"category": "groceries"})
        assert "Transaction" in query
        assert "filter_category" in params
        assert params["filter_category"] == "groceries"

    def test_search_with_text_query(self):
        query, params = self.qb.search("memory", {"query": "redis timeout"})
        assert "CONTAINS" in query
        assert params["search_query"] == "redis timeout"

    def test_relationship_match(self):
        result = self.qb.relationship_match("memory", "memory", "RELATED_TO")
        assert "RELATED_TO" in result
        assert ":Memory" in result

    def test_invalid_label_rejected(self):
        with pytest.raises((ValueError, KeyError)):
            self.qb.match("Memory}) DETACH DELETE m //")

    def test_invalid_alias_rejected(self):
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.match("memory", alias="m; DROP")

    def test_merge_always_uses_id(self):
        """merge() always uses id as the key — no key_field parameter."""
        assert self.qb.merge("memory") == "MERGE (m:Memory {id: $id})"
        assert self.qb.merge("transaction") == "MERGE (m:Transaction {id: $id})"

    def test_search_ignores_falsy_special_keys(self):
        """Empty query/tags should be silently skipped, not treated as property filters."""
        query, params = self.qb.search("memory", {"query": "", "tags": [], "category": "test"})
        assert "m.query" not in query
        assert "m.tags =" not in query
        assert "filter_category" in params

    def test_invalid_relationship_type_rejected(self):
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.relationship_match("memory", "memory", "RELATED_TO]->(x) DETACH DELETE x //")
