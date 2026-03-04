# Phase 2: QueryBuilder (Neo4j path)

**Status:** [ ] Not started
**Files:** `src/memorygraph/query_builder.py` (NEW), `src/memorygraph/database.py` (MODIFY)
**Gate:** `uv run pytest tests/test_query_builder.py tests/test_database.py -v` — 14 QB tests + existing DB tests

## What this does

Replaces ~26 hardcoded `:Memory` Cypher strings in `database.py` with parameterized queries. Also validates relationship types interpolated into Cypher (fixing injection vulnerability at database.py:830).

## Dependencies

- Phase 1 (NodeTypeRegistry)

## Vulnerable code being replaced

```python
# BEFORE (database.py:830) — relationship_type.value interpolated without validation:
query = f"MATCH (from:Memory {{id: $from_id}})-[r:{relationship_type.value}]->(to:Memory {{id: $to_id}}) ..."
# An attacker could pass: "RELATED_TO]->(x) DETACH DELETE x //"

# AFTER — QueryBuilder.relationship_match() validates all identifiers:
query = f"{self.qb.relationship_match('memory', 'memory', relationship_type.value)} ..."
# _validate_identifier() rejects anything not matching [A-Za-z_][A-Za-z0-9_]*
```

## Code

```python
import re
from .type_registry import NodeTypeRegistry

# Allowlist pattern: alphanumeric + underscore only
_VALID_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def _validate_identifier(value: str, kind: str = "identifier") -> str:
    """Validate that a string is safe for Cypher interpolation."""
    if not _VALID_IDENTIFIER.match(value):
        raise ValueError(f"Invalid {kind}: {value!r}. Must match [A-Za-z_][A-Za-z0-9_]*")
    return value

class QueryBuilder:
    def __init__(self, registry: NodeTypeRegistry):
        self.registry = registry

    def match(self, type_name: str, alias: str = "m") -> str:
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"MATCH ({alias}:{label})"

    def create(self, type_name: str, alias: str = "m") -> str:
        """NOTE: Prefer merge() for idempotent operations."""
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"CREATE ({alias}:{label} $props)"

    def merge(self, type_name: str, alias: str = "m") -> str:
        """MERGE = create-or-update (upsert). Primary method for store operations.

        Always merges on `id` — consistent with match() and create() signatures.
        The MERGE key is hardcoded to `id` because all node types use UUID ids
        as their unique identifier.
        """
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"MERGE ({alias}:{label} {{id: $id}})"

    def return_with_labels(self, alias: str = "m") -> str:
        """Generate RETURN clause that includes explicit labels.

        IMPORTANT: Both database.py and neo4j_backend.py call result.data()
        which converts Neo4j records to plain dicts. So record["labels"]
        will be a plain list[str], NOT a frozenset.
        """
        alias = _validate_identifier(alias, "alias")
        return f"RETURN {alias}, labels({alias}) AS labels"

    def relationship_match(
        self, from_type: str, to_type: str, rel_type: str,
        from_alias: str = "from", to_alias: str = "to", rel_alias: str = "r"
    ) -> str:
        """Generate MATCH for relationship queries with validated identifiers."""
        from_label = _validate_identifier(self.registry.get_label(from_type), "label")
        to_label = _validate_identifier(self.registry.get_label(to_type), "label")
        rel_type = _validate_identifier(rel_type, "relationship_type")
        from_alias = _validate_identifier(from_alias, "alias")
        to_alias = _validate_identifier(to_alias, "alias")
        rel_alias = _validate_identifier(rel_alias, "alias")
        return f"MATCH ({from_alias}:{from_label})-[{rel_alias}:{rel_type}]->({to_alias}:{to_label})"

    def search(self, type_name: str, filters: dict) -> tuple[str, dict]:
        """Build a parameterized search query from filters.

        Special keys: "query" (text search), "tags" (ANY match), "min_importance" (threshold).
        All other keys: exact property match.
        Returns: (cypher_query, parameters) tuple
        """
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        conditions = []
        params = {}

        for key, value in filters.items():
            if key == "query" and value:
                conditions.append(
                    "(m.title CONTAINS $search_query OR "
                    "m.content CONTAINS $search_query OR "
                    "m.summary CONTAINS $search_query)"
                )
                params["search_query"] = value
            elif key == "tags" and value:
                conditions.append("ANY(tag IN $filter_tags WHERE tag IN m.tags)")
                params["filter_tags"] = value
            elif key == "min_importance" and value is not None:
                conditions.append("m.importance >= $min_importance")
                params["min_importance"] = value
            else:
                safe_key = _validate_identifier(key, "filter_field")
                param_name = f"filter_{safe_key}"
                conditions.append(f"m.{safe_key} = ${param_name}")
                params[param_name] = value

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"MATCH (m:{label}){where} RETURN m, labels(m) AS labels"
        return query, params
```

## Migration strategy for database.py

```python
# BEFORE:
query = "MATCH (m:Memory) WHERE m.id = $id RETURN m"

# AFTER:
query = f"{self.qb.match(node_type)} WHERE m.id = $id {self.qb.return_with_labels()}"
# Produces: MATCH (m:Memory) WHERE m.id = $id RETURN m, labels(m) AS labels
```

## Tests: `tests/test_query_builder.py`

```python
from memorygraph.query_builder import QueryBuilder, _validate_identifier
from memorygraph.type_registry import NodeTypeConfig, get_default_registry
from memorygraph.models import Transaction

class TestQueryBuilder:
    def setup_method(self):
        from memorygraph.type_registry import register_custom_types
        self.registry = get_default_registry()
        register_custom_types(self.registry)  # Need Transaction for these tests
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

    def test_invalid_relationship_type_rejected(self):
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.relationship_match("memory", "memory", "RELATED_TO]->(x) DETACH DELETE x //")
```

## Acceptance Criteria

- [ ] All ~26 `:Memory` references in database.py use QueryBuilder
- [ ] Existing Memory queries produce identical Cypher output
- [ ] New types produce correct labels
- [ ] No raw `:Memory` strings remain in database.py
- [ ] All identifier inputs validated
- [ ] Queries returning nodes use `return_with_labels()`
- [ ] `search()` returns parameterized (query, params) tuples
- [ ] Relationship type interpolation validated (fixes injection at line 830)
- [ ] All 14 tests pass + existing database tests still pass
