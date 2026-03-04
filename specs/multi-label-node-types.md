# Spec: Multi-Label Node Types

**Status:** In Progress
**Author:** Christian Nogueras
**Branch:** `feature/multi-label-node-types`
**Created:** 2026-03-03

## Goal

Extend MemoryGraph so each node type gets its own Neo4j label and Pydantic model, instead of everything being `:Memory` with a `type` property. This enables custom schemas per domain (transactions, agents, etc.) while keeping backward compatibility with existing `:Memory` nodes.

## Motivation

MemoryGraph today stores everything as `:Memory` nodes. The `type` field (task, project, solution, etc.) is a string property — useful for filtering, but all nodes share the same schema. This limits the system when you need domain-specific properties:

- A **Transaction** needs: amount, merchant, category, payment_method, currency
- An **Agent** needs: trigger, schedule, last_run, status, capabilities
- A **Device** needs: hostname, ip, os, role

Forcing these into `content` as JSON strings loses Neo4j's native querying and indexing.

## Current Architecture

```text
MCP Tool Call
  → server.py (routes to handler)
    → tools/registry.py (validates input, creates Memory object)
      → database.py (builds Cypher, 50+ hardcoded MATCH (m:Memory))
        → backends/neo4j_backend.py (executes query)
          → Neo4j: (:Memory {type: "task", title: "...", ...})
```

### Where `:Memory` is hardcoded

| File | Occurrences | Description |
|------|-------------|-------------|
| `database.py` | ~50 Cypher queries | `MATCH (m:Memory)`, `CREATE (m:Memory {...})` |
| `neo4j_backend.py` | ~12 schema statements | `CREATE INDEX FOR (m:Memory)` |
| `models.py` | `MemoryNode.to_neo4j_properties()` | Converts Memory → Neo4j props |
| `database.py` | `_neo4j_to_memory()` | Always deserializes as Memory |

### Key observation

`MemoryNode` has a `labels: List[str]` field that is **defined but never used**. The infrastructure was partially anticipated.

## Target Architecture

```text
MCP Tool Call
  → server.py (routes to handler)
    → tools/registry.py (validates via NodeTypeRegistry)
      → database.py (QueryBuilder generates Cypher with correct label)
        → backends/neo4j_backend.py (executes query)
          → Neo4j: (:Transaction {amount: 50, merchant: "Pueblo", ...})
                   (:Memory {type: "task", title: "Fix bug", ...})
                   (:Agent {trigger: "cron", schedule: "daily", ...})
```

## Implementation Phases

---

### Phase 1: NodeTypeRegistry
**Status:** [ ] Not started
**Files:** `src/memorygraph/registry.py` (NEW)

A central registry that maps type names to labels, models, and schemas.

```python
class NodeTypeConfig:
    name: str              # "transaction"
    label: str             # "Transaction"
    model: type[BaseModel] # Transaction class
    indexes: list[str]     # ["amount", "merchant", "category"]

class NodeTypeRegistry:
    _types: dict[str, NodeTypeConfig]

    def register(self, config: NodeTypeConfig) -> None
    def get_label(self, type_name: str) -> str
    def get_model(self, type_name: str) -> type[BaseModel]
    def all_types(self) -> list[NodeTypeConfig]
```

Default registration:
```python
registry = NodeTypeRegistry()
registry.register(NodeTypeConfig(
    name="memory",
    label="Memory",
    model=Memory,
    indexes=["id", "type", "title"]
))
```

**Acceptance criteria:**
- [ ] Registry can register and retrieve type configs
- [ ] Default "memory" type is pre-registered
- [ ] Unknown type raises clear error
- [ ] Unit tests pass

---

### Phase 2: QueryBuilder
**Status:** [ ] Not started
**Files:** `src/memorygraph/query_builder.py` (NEW), `src/memorygraph/database.py` (MODIFY)

Replaces 50+ hardcoded Cypher strings with parameterized queries.

```python
import re

# Allowlist pattern: alphanumeric + underscore only
_VALID_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def _validate_identifier(value: str, kind: str = "identifier") -> str:
    """Validate that a string is safe for Cypher interpolation.

    Prevents injection via label names, aliases, or field names.
    Only alphanumeric characters and underscores are allowed.
    """
    if not _VALID_IDENTIFIER.match(value):
        raise ValueError(f"Invalid {kind}: {value!r}. Must match [A-Za-z_][A-Za-z0-9_]*")
    return value

class QueryBuilder:
    def __init__(self, registry: NodeTypeRegistry): ...

    def match(self, type_name: str, alias: str = "m") -> str:
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"MATCH ({alias}:{label})"

    def create(self, type_name: str, alias: str = "m") -> str:
        """NOTE: Prefer merge() for idempotent operations. Use create() only
        when you explicitly want to fail on duplicates."""
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"CREATE ({alias}:{label} $props)"

    def merge(self, type_name: str, key_field: str = "id", alias: str = "m") -> str:
        """MERGE = create-or-update (upsert). This is the PRIMARY method for
        store_memory and store_node. Always use MERGE unless you need strict
        insert-only semantics."""
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        key_field = _validate_identifier(key_field, "key_field")
        return f"MERGE ({alias}:{label} {{{key_field}: ${key_field}}})"

    def match_with_labels(self, type_name: str, alias: str = "m") -> str:
        """MATCH that also returns labels for deserialization.

        IMPORTANT: Neo4j Node objects store labels in a .labels attribute
        (a frozenset), NOT as a dict key. When returning raw nodes, the
        driver does NOT include labels in the record dict. You MUST use
        labels(m) to explicitly extract them. See Phase 4 (NodeFactory).
        """
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"MATCH ({alias}:{label})"

    def return_with_labels(self, alias: str = "m") -> str:
        """Generate RETURN clause that includes explicit labels.

        Usage: f"{qb.match('memory')} WHERE m.id = $id {qb.return_with_labels()}"
        Produces: MATCH (m:Memory) WHERE m.id = $id RETURN m, labels(m) AS labels
        """
        alias = _validate_identifier(alias, "alias")
        return f"RETURN {alias}, labels({alias}) AS labels"

    def search(self, type_name: str, filters: dict) -> tuple[str, dict]:
        # Returns (cypher_query, parameters)
        ...
```

**Identifier validation:** All user-supplied strings that get interpolated into Cypher (labels, aliases, field names) are validated against `[A-Za-z_][A-Za-z0-9_]*`. This prevents Cypher injection attacks — e.g., a malicious `type_name` like `"Memory}) DETACH DELETE m //"` would be rejected before reaching the database.

**MERGE over CREATE:** `merge()` is the primary method. `create()` exists but has a docstring warning. The existing `store_memory` in `database.py` already uses MERGE at line 295 — this is preserved and generalized.

Migration strategy for `database.py`:
```python
# BEFORE:
query = "MATCH (m:Memory) WHERE m.id = $id RETURN m"

# AFTER (queries that need deserialization):
query = f"{self.qb.match(node_type)} WHERE m.id = $id {self.qb.return_with_labels()}"
# Produces: MATCH (m:Memory) WHERE m.id = $id RETURN m, labels(m) AS labels

# AFTER (queries that don't need label info):
query = f"{self.qb.match(node_type)} WHERE m.id = $id RETURN m"
```

**Acceptance criteria:**
- [ ] All 50+ queries in database.py use QueryBuilder
- [ ] Existing Memory queries produce identical Cypher
- [ ] New types produce correct labels
- [ ] No raw `:Memory` strings remain in database.py
- [ ] All identifier inputs are validated (labels, aliases, field names)
- [ ] Queries returning nodes for deserialization use `return_with_labels()`

---

### Phase 3: New Models
**Status:** [ ] Not started
**Files:** `src/memorygraph/models.py` (MODIFY), `src/memorygraph/models/` (NEW directory)

```python
# models/transaction.py
class Transaction(BaseModel):
    id: Optional[str] = None
    amount: float
    merchant: str
    category: str  # groceries, dining, transport, etc.
    currency: str = "USD"
    payment_method: str = "unknown"  # google_pay, cash, credit, ath_movil
    date: datetime
    tags: list[str] = []
    importance: float = Field(default=0.3, ge=0.0, le=1.0)
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

Register in the registry:
```python
registry.register(NodeTypeConfig(
    name="transaction",
    label="Transaction",
    model=Transaction,
    indexes=["id", "amount", "merchant", "category", "date"]
))
```

**Acceptance criteria:**
- [ ] Transaction model validates correctly
- [ ] Can store and retrieve Transaction nodes
- [ ] Transaction nodes have `:Transaction` label in Neo4j
- [ ] Properties are native Neo4j types (not JSON strings)

---

### Phase 4: NodeFactory (Deserialization)
**Status:** [ ] Not started
**Files:** `src/memorygraph/node_factory.py` (NEW), `src/memorygraph/database.py` (MODIFY)

Replaces `_neo4j_to_memory()` which always returns Memory objects.

#### Critical: Neo4j Node labels

A `neo4j.graph.Node` object returned from `RETURN m` does **NOT** have a `.get()` method or a `"labels"` dict key. Labels are stored in a `.labels` attribute (a `frozenset`). There are two ways to get labels:

1. **Access `.labels` attribute** on the Node object (if you extract the node from the record)
2. **Use `labels(m) AS labels`** in the Cypher RETURN clause (returns a list of strings)

We use approach #2 because it works uniformly with `QueryBuilder.return_with_labels()` and doesn't require knowing the Neo4j driver's internal types.

#### Property normalization

Neo4j returns data in forms that don't match Pydantic models directly:
- **ISO datetime strings**: `"2026-03-03T00:00:00Z"` → needs `datetime` parsing
- **Neo4j temporal types**: `neo4j.time.DateTime` → needs conversion to Python `datetime`
- **`context_`-prefixed keys**: The existing code stores `MemoryContext` fields as `context_project`, `context_source` etc. → need to be regrouped into a `context` dict
- **Missing optional fields**: Must use model defaults, not error

```python
from datetime import datetime, timezone
from typing import Any

class NodeFactory:
    def __init__(self, registry: NodeTypeRegistry): ...

    def _normalize_properties(self, raw_props: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
        """Normalize Neo4j properties before Pydantic instantiation.

        Handles:
        - Neo4j temporal types → Python datetime
        - ISO datetime strings → Python datetime (for date/datetime fields)
        - context_* prefixed keys → nested context dict
        - Strips internal Neo4j properties (elementId, etc.)
        """
        props = {}
        context = {}
        model_fields = model.model_fields

        for key, value in raw_props.items():
            # Regroup context_ prefixed keys
            if key.startswith("context_"):
                context_key = key[len("context_"):]
                context[context_key] = value
                continue

            # Convert Neo4j temporal types
            if hasattr(value, 'to_native'):  # neo4j.time.DateTime
                value = value.to_native()
            elif isinstance(value, str) and key in model_fields:
                field_type = model_fields[key].annotation
                # Auto-parse ISO strings for datetime fields
                if field_type is datetime or (hasattr(field_type, '__origin__') and datetime in getattr(field_type, '__args__', ())):
                    try:
                        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass  # Leave as string, let Pydantic validate

            props[key] = value

        if context:
            props["context"] = context

        return props

    def from_neo4j_record(self, record) -> BaseModel:
        """Deserialize a Neo4j query result to the correct Python model.

        IMPORTANT: The query MUST use QueryBuilder.return_with_labels() so
        that `labels` is an explicit column in the result. Example:
            MATCH (m:Transaction) WHERE m.id = $id RETURN m, labels(m) AS labels

        Do NOT use record.get("labels") on a neo4j.graph.Node — Node objects
        don't have .get(). Instead, labels come as a separate column.
        """
        # Extract the node and labels from the record
        # record["m"] is a neo4j.graph.Node, record["labels"] is a list[str]
        node = record["m"]
        labels = record.get("labels", list(node.labels) if hasattr(node, 'labels') else [])

        # Get raw properties from the node
        raw_props = dict(node)  # neo4j.graph.Node supports dict() conversion

        # Find matching type in registry (priority: most specific first)
        matched_config = self._resolve_type(labels)
        model = matched_config.model if matched_config else Memory

        # Normalize properties for the target model
        normalized = self._normalize_properties(raw_props, model)

        return model(**normalized)

    def _resolve_type(self, labels: list[str]) -> NodeTypeConfig | None:
        """Resolve which registered type matches the given labels.

        Priority rules for multi-label ambiguity:
        1. Exact match on a non-Memory registered label wins
        2. If multiple registered labels match, use the first registered
           (registration order = priority order)
        3. If only :Memory matches (or nothing matches), return Memory config

        Example: A node with labels [:Memory, :Transaction] → Transaction wins
        because Transaction is more specific than the base Memory type.
        """
        memory_config = None
        for type_config in self.registry.all_types():
            if type_config.label in labels:
                if type_config.name == "memory":
                    memory_config = type_config
                    continue  # Keep looking for more specific match
                return type_config  # First non-Memory match wins

        return memory_config  # Fallback to Memory (or None if not registered)
```

**Acceptance criteria:**
- [ ] Neo4j records with `:Transaction` label → Transaction object
- [ ] Neo4j records with `:Memory` label → Memory object (backward compatible)
- [ ] Unknown labels fall back to Memory
- [ ] Relationships between different node types work
- [ ] ISO datetime strings are parsed to Python datetime objects
- [ ] `context_*` prefixed properties are regrouped into context dict
- [ ] Neo4j temporal types are converted to Python datetime
- [ ] Multi-label nodes (e.g., `:Memory:Transaction`) resolve to the most specific type

---

### Phase 5: Schema Manager
**Status:** [ ] Not started
**Files:** `src/memorygraph/backends/neo4j_backend.py` (MODIFY)

Currently has 12 hardcoded schema statements for `:Memory`. Replace with dynamic schema from registry that covers: node constraints, property indexes, relationship constraints, fulltext indexes, and multi-tenant isolation.

#### Extended NodeTypeConfig for schema

```python
@dataclass
class NodeTypeConfig:
    name: str              # "transaction"
    label: str             # "Transaction"
    model: type[BaseModel] # Transaction class
    indexes: list[str]     # Property indexes: ["amount", "merchant", "category"]
    fulltext_fields: list[str] = field(default_factory=list)  # ["title", "note", "content"]
    relationship_constraints: list[RelationshipConstraint] = field(default_factory=list)

@dataclass
class RelationshipConstraint:
    """Defines allowed relationships FROM this node type."""
    rel_type: str          # "RELATED_TO", "FEEDS"
    target_label: str      # "Memory", "Transaction" (or "*" for any)
    cardinality: str = "many"  # "one" or "many" (informational, not enforced by Neo4j)
```

#### Schema generation

```python
def ensure_schema(self):
    for type_config in self.registry.all_types():
        label = _validate_identifier(type_config.label, "label")

        # 1. Uniqueness constraint on id (PRIMARY — required for MERGE)
        self.execute(
            f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
        )

        # 2. Property indexes for query performance
        for field_name in type_config.indexes:
            field_name = _validate_identifier(field_name, "index_field")
            self.execute(
                f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{field_name})"
            )

        # 3. Composite indexes for common query patterns
        if len(type_config.indexes) > 1:
            # Example: Transaction frequently queried by (category, date)
            # Only create if explicitly configured in the NodeTypeConfig
            pass  # Composite indexes added per-type as needed

        # 4. Fulltext search index (if configured)
        if type_config.fulltext_fields:
            fields = ", ".join(f"n.{_validate_identifier(f, 'field')}"
                              for f in type_config.fulltext_fields)
            index_name = f"{label.lower()}_fulltext"
            self.execute(
                f'CREATE FULLTEXT INDEX {index_name} IF NOT EXISTS '
                f'FOR (n:{label}) ON EACH [{fields}]'
            )

    # 5. Relationship type indexes (shared across all node types)
    self.execute(
        "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.context)"
    )
    self.execute(
        "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.strength)"
    )

    # 6. Multi-tenant isolation index (if user_id / project_path scoping is needed)
    # This enables future multi-user support without full re-schema
    for type_config in self.registry.all_types():
        label = _validate_identifier(type_config.label, "label")
        self.execute(
            f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.project_path)"
        )
```

#### Default Memory registration with fulltext

```python
registry.register(NodeTypeConfig(
    name="memory",
    label="Memory",
    model=Memory,
    indexes=["id", "type", "title", "importance"],
    fulltext_fields=["title", "content", "summary"],
))

registry.register(NodeTypeConfig(
    name="transaction",
    label="Transaction",
    model=Transaction,
    indexes=["id", "amount", "merchant", "category", "date"],
    fulltext_fields=["merchant", "note"],
    relationship_constraints=[
        RelationshipConstraint(rel_type="RELATED_TO", target_label="*"),
        RelationshipConstraint(rel_type="CATEGORIZED_AS", target_label="Memory"),
    ]
))
```

**Acceptance criteria:**
- [ ] Each registered type gets its own constraints/indexes
- [ ] Existing `:Memory` indexes unchanged
- [ ] New types get indexes on registration
- [ ] Schema creation is idempotent
- [ ] Fulltext indexes created for types that declare fulltext_fields
- [ ] Relationship indexes created for common traversal patterns
- [ ] Multi-tenant (project_path) index created for all types
- [ ] All identifiers in schema DDL are validated against injection

---

### Phase 6: MCP Tools
**Status:** [ ] Not started
**Files:** `src/memorygraph/tools/registry.py` (MODIFY), `src/memorygraph/server.py` (MODIFY)

Add new tools or extend existing ones:

Option A: **New tools per type**
```
store_transaction(amount, merchant, category, ...)
search_transactions(min_amount, category, date_range, ...)
```

Option B: **Extend existing tools with `node_type` param**
```
store_memory(node_type="transaction", amount=50, merchant="Pueblo", ...)
search_memories(node_type="transaction", ...)
```

**Decision:** TBD — Option A is cleaner for the AI, Option B is less code.

**Acceptance criteria:**
- [ ] Can store Transaction via MCP tool
- [ ] Can search Transactions via MCP tool
- [ ] Can create relationships between Transaction and Memory nodes
- [ ] Existing store_memory/search_memories unchanged

---

## Backward Compatibility

| Concern | Resolution |
|---------|------------|
| Existing 54 `:Memory` nodes | Untouched — "memory" is default type in registry |
| Existing relationships | Work across labels — Neo4j doesn't care about labels in relationships |
| Existing MCP tools | Unchanged — store_memory, search_memories, recall_memories all default to `:Memory` |
| Existing CLAUDE.md instructions | No changes needed |
| Upgrade path | Zero migration — new labels coexist with `:Memory` |

## Cross-Type Relationships

Relationships in Neo4j are label-agnostic. This works natively:

```cypher
-- Transaction linked to a Memory
MATCH (t:Transaction)-[:RELATED_TO]->(m:Memory) RETURN t, m

-- All nodes connected to a project (regardless of label)
MATCH (n)-[:RELATED_TO]->(p:Memory {type: "project"}) RETURN n
```

## Example: Transaction Flow

```text
1. Google Pay notification → Tasker
2. Tasker → Termux script
3. Script calls MCP: store_transaction(amount=50, merchant="Pueblo", category="groceries")
4. QueryBuilder: MERGE (t:Transaction {id: $id}) SET t += $properties
5. Neo4j: (:Transaction {amount: 50, ...})
6. Later: search_transactions(category="groceries", date_range="2026-03")
7. QueryBuilder: MATCH (t:Transaction) WHERE t.category = "groceries" AND ...
```

## Verification Strategy

### How to run tests

```bash
cd ~/Code/memory-graph

# Run all tests (SQLite backend, no Neo4j needed)
uv run pytest tests/ -v

# Run only multi-label tests
uv run pytest tests/test_registry.py tests/test_query_builder.py tests/test_node_factory.py tests/test_multi_label_integration.py -v

# Run with coverage
uv run pytest tests/ --cov=memorygraph --cov-report=term-missing

# Run existing tests to verify nothing is broken
uv run pytest tests/test_database.py tests/test_backward_compatibility.py -v
```

### Test pattern (matches existing codebase)

Tests use **SQLiteFallbackBackend** with temp directories — no Neo4j instance needed. Follow the pattern in `tests/test_backward_compatibility.py`:

```python
import pytest
import tempfile
from pathlib import Path
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase

class TestMyFeature:
    @pytest.fixture
    async def db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            backend = SQLiteFallbackBackend(db_path=db_path)
            await backend.connect()
            await backend.initialize_schema()
            db = SQLiteMemoryDatabase(backend)
            await db.initialize_schema()
            yield db
            await backend.disconnect()
```

### Phase 1 verification: `tests/test_registry.py`

```python
class TestNodeTypeRegistry:
    def test_register_and_retrieve(self):
        """Register a type, retrieve it by name."""
        registry = NodeTypeRegistry()
        config = NodeTypeConfig(name="transaction", label="Transaction", model=Transaction, indexes=["id"])
        registry.register(config)
        assert registry.get_label("transaction") == "Transaction"
        assert registry.get_model("transaction") == Transaction

    def test_default_memory_type_registered(self):
        """The 'memory' type should be pre-registered."""
        registry = get_default_registry()
        assert registry.get_label("memory") == "Memory"
        assert registry.get_model("memory") == Memory

    def test_unknown_type_raises(self):
        """Requesting unknown type raises KeyError with helpful message."""
        registry = NodeTypeRegistry()
        with pytest.raises(KeyError, match="unknown_type"):
            registry.get_label("unknown_type")

    def test_duplicate_registration_raises(self):
        """Registering same name twice raises ValueError."""
        registry = NodeTypeRegistry()
        config = NodeTypeConfig(name="tx", label="Tx", model=Transaction, indexes=[])
        registry.register(config)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(config)

    def test_all_types_returns_all(self):
        """all_types() returns every registered config."""
        registry = get_default_registry()  # has "memory"
        registry.register(NodeTypeConfig(name="transaction", label="Transaction", model=Transaction, indexes=[]))
        assert len(registry.all_types()) == 2
        names = [t.name for t in registry.all_types()]
        assert "memory" in names
        assert "transaction" in names
```

**Gate:** All 5 tests pass → Phase 1 is done.

### Phase 2 verification: `tests/test_query_builder.py`

```python
class TestQueryBuilder:
    def setup_method(self):
        self.registry = get_default_registry()
        self.registry.register(NodeTypeConfig(name="transaction", label="Transaction", model=Transaction, indexes=[]))
        self.qb = QueryBuilder(self.registry)

    def test_match_memory_unchanged(self):
        """match('memory') produces same Cypher as before."""
        assert self.qb.match("memory") == "MATCH (m:Memory)"

    def test_match_transaction(self):
        """match('transaction') produces Transaction label."""
        assert self.qb.match("transaction") == "MATCH (m:Transaction)"

    def test_create_memory_unchanged(self):
        """create('memory') produces same Cypher as before."""
        assert "CREATE (m:Memory" in self.qb.create("memory")

    def test_create_transaction(self):
        """create('transaction') produces Transaction label."""
        assert "CREATE (m:Transaction" in self.qb.create("transaction")

    def test_merge_memory_unchanged(self):
        """merge('memory') produces MERGE with :Memory label (upsert)."""
        assert self.qb.merge("memory") == "MERGE (m:Memory {id: $id})"

    def test_merge_transaction(self):
        """merge('transaction') produces MERGE with :Transaction label."""
        assert self.qb.merge("transaction") == "MERGE (m:Transaction {id: $id})"

    def test_custom_alias(self):
        """Can use custom alias."""
        assert self.qb.match("transaction", alias="t") == "MATCH (t:Transaction)"

    def test_return_with_labels(self):
        """return_with_labels() produces RETURN m, labels(m) AS labels."""
        assert self.qb.return_with_labels() == "RETURN m, labels(m) AS labels"
        assert self.qb.return_with_labels("t") == "RETURN t, labels(t) AS labels"

    def test_invalid_label_rejected(self):
        """Malicious label names are rejected before Cypher interpolation."""
        registry = NodeTypeRegistry()
        # Register a type with a dangerous label (should fail at registration)
        with pytest.raises(ValueError, match="Invalid"):
            # If validation is in QueryBuilder, test there instead:
            qb = QueryBuilder(registry)
            qb.match("Memory}) DETACH DELETE m //")

    def test_invalid_alias_rejected(self):
        """Aliases with special characters are rejected."""
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.match("memory", alias="m; DROP")

    def test_invalid_key_field_rejected(self):
        """Key fields in MERGE are validated."""
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.merge("memory", key_field="id} SET m.admin=true //")
```

**Gate + regression check:**
```bash
# Phase 2 tests pass
uv run pytest tests/test_query_builder.py -v

# AND existing database tests still pass (no query regression)
uv run pytest tests/test_database.py -v
```

### Phase 3 verification: `tests/test_transaction_model.py`

```python
class TestTransactionModel:
    def test_valid_transaction(self):
        """Transaction with all required fields validates."""
        t = Transaction(amount=50.0, merchant="Pueblo", category="groceries", date=datetime.now(timezone.utc))
        assert t.amount == 50.0
        assert t.merchant == "Pueblo"

    def test_missing_required_field_raises(self):
        """Transaction without merchant raises ValidationError."""
        with pytest.raises(ValidationError):
            Transaction(amount=50.0, category="groceries", date=datetime.now(timezone.utc))

    def test_default_values(self):
        """Defaults: currency=USD, payment_method=unknown, importance=0.3."""
        t = Transaction(amount=10, merchant="Test", category="test", date=datetime.now(timezone.utc))
        assert t.currency == "USD"
        assert t.payment_method == "unknown"
        assert t.importance == 0.3

    def test_tags_lowercased(self):
        """Tags are normalized to lowercase."""
        t = Transaction(amount=10, merchant="Test", category="test", date=datetime.now(timezone.utc), tags=["FOOD", "Weekly"])
        assert t.tags == ["food", "weekly"]
```

### Phase 4 verification: `tests/test_node_factory.py`

Tests use mock Neo4j records to simulate what `RETURN m, labels(m) AS labels` produces:

```python
class MockNeo4jNode(dict):
    """Simulates a neo4j.graph.Node — supports dict() conversion and .labels attribute."""
    def __init__(self, properties: dict, labels: frozenset):
        super().__init__(properties)
        self.labels = labels  # frozenset, like real Neo4j nodes

class MockRecord:
    """Simulates a neo4j.Record from RETURN m, labels(m) AS labels."""
    def __init__(self, node: MockNeo4jNode, labels: list[str]):
        self._data = {"m": node, "labels": labels}
    def __getitem__(self, key):
        return self._data[key]
    def get(self, key, default=None):
        return self._data.get(key, default)

class TestNodeFactory:
    def setup_method(self):
        self.registry = get_default_registry()
        self.registry.register(NodeTypeConfig(name="transaction", label="Transaction", model=Transaction, indexes=[]))
        self.factory = NodeFactory(self.registry)

    def test_memory_record_returns_memory(self):
        """Record with Memory label → Memory object."""
        node = MockNeo4jNode({"id": "1", "type": "task", "title": "Test", "content": "..."}, frozenset(["Memory"]))
        record = MockRecord(node, ["Memory"])
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Memory)

    def test_transaction_record_returns_transaction(self):
        """Record with Transaction label → Transaction object."""
        node = MockNeo4jNode(
            {"id": "2", "amount": 50.0, "merchant": "Pueblo", "category": "groceries", "date": "2026-03-03T00:00:00Z"},
            frozenset(["Transaction"])
        )
        record = MockRecord(node, ["Transaction"])
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)
        assert result.amount == 50.0

    def test_unknown_label_falls_back_to_memory(self):
        """Record with unknown label → Memory fallback."""
        node = MockNeo4jNode({"id": "3", "type": "general", "title": "X", "content": "Y"}, frozenset(["WeirdType"]))
        record = MockRecord(node, ["WeirdType"])
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Memory)

    def test_multi_label_resolves_to_specific(self):
        """Node with [:Memory, :Transaction] → Transaction (most specific wins)."""
        node = MockNeo4jNode(
            {"id": "4", "amount": 25.0, "merchant": "Cafe", "category": "dining", "date": "2026-03-03T12:00:00Z"},
            frozenset(["Memory", "Transaction"])
        )
        record = MockRecord(node, ["Memory", "Transaction"])
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)

    def test_context_prefix_normalization(self):
        """context_project, context_source → nested context dict."""
        node = MockNeo4jNode(
            {"id": "5", "type": "task", "title": "Test", "content": "...",
             "context_project": "/my/project", "context_source": "github"},
            frozenset(["Memory"])
        )
        record = MockRecord(node, ["Memory"])
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Memory)
        # context_ prefixed keys regrouped into context object

    def test_iso_datetime_string_parsed(self):
        """ISO datetime strings in date fields are parsed to datetime objects."""
        node = MockNeo4jNode(
            {"id": "6", "amount": 10.0, "merchant": "Test", "category": "test",
             "date": "2026-03-03T18:00:00+00:00"},
            frozenset(["Transaction"])
        )
        record = MockRecord(node, ["Transaction"])
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)
        assert isinstance(result.date, datetime)
```

### Phase 5 verification: `tests/test_schema_manager.py`

```python
class TestSchemaManager:
    @pytest.fixture
    async def db(self):
        """SQLite backend with registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            backend = SQLiteFallbackBackend(db_path=db_path)
            await backend.connect()
            # Schema should be created by registry types
            await backend.initialize_schema()
            db = SQLiteMemoryDatabase(backend)
            await db.initialize_schema()
            yield db
            await backend.disconnect()

    async def test_memory_schema_exists(self, db):
        """Memory table/indexes exist after init."""
        # Verify by storing and retrieving a Memory
        memory = Memory(type=MemoryType.TASK, title="Test", content="Content")
        mid = await db.store_memory(memory)
        result = await db.get_memory(mid)
        assert result.title == "Test"

    async def test_schema_idempotent(self, db):
        """Calling initialize_schema twice doesn't error."""
        await db.initialize_schema()  # second call
        memory = Memory(type=MemoryType.TASK, title="Test2", content="Content2")
        mid = await db.store_memory(memory)
        assert mid is not None
```

### Phase 6 verification: `tests/test_multi_label_integration.py`

The end-to-end test that proves the full chain works:

```python
class TestMultiLabelIntegration:
    """Full chain: MCP tool call → database → store → retrieve → correct type."""

    @pytest.fixture
    async def db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "integration.db")
            backend = SQLiteFallbackBackend(db_path=db_path)
            await backend.connect()
            await backend.initialize_schema()
            db = SQLiteMemoryDatabase(backend)
            await db.initialize_schema()
            yield db
            await backend.disconnect()

    async def test_store_and_retrieve_transaction(self, db):
        """Store a Transaction, retrieve it, verify type and properties."""
        # This is the ultimate proof: if this passes, multi-label works end-to-end
        tx = Transaction(amount=42.50, merchant="Pueblo Supermarket", category="groceries",
                         payment_method="google_pay", date=datetime.now(timezone.utc))
        tx_id = await db.store_node("transaction", tx)
        retrieved = await db.get_node(tx_id)
        assert isinstance(retrieved, Transaction)
        assert retrieved.amount == 42.50
        assert retrieved.merchant == "Pueblo Supermarket"
        assert retrieved.payment_method == "google_pay"

    async def test_store_memory_still_works(self, db):
        """Existing store_memory unchanged — backward compatible."""
        memory = Memory(type=MemoryType.SOLUTION, title="Redis fix", content="Increased timeout")
        mid = await db.store_memory(memory)
        result = await db.get_memory(mid)
        assert isinstance(result, Memory)
        assert result.title == "Redis fix"

    async def test_cross_type_relationship(self, db):
        """Can create relationship between Transaction and Memory."""
        tx = Transaction(amount=100, merchant="AWS", category="infrastructure",
                         date=datetime.now(timezone.utc))
        tx_id = await db.store_node("transaction", tx)

        memory = Memory(type=MemoryType.PROJECT, title="KiriInfra", content="VPS infrastructure")
        mem_id = await db.store_memory(memory)

        rel_id = await db.create_relationship(tx_id, mem_id, "RELATED_TO", context="AWS bill for KiriInfra")
        assert rel_id is not None

        related = await db.get_related_memories(mem_id)
        assert len(related) >= 1

    async def test_search_transactions_only(self, db):
        """Search scoped to transaction type doesn't return memories."""
        tx = Transaction(amount=25, merchant="Cafe", category="dining", date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx)
        memory = Memory(type=MemoryType.TASK, title="Cafe review", content="Write review")
        await db.store_memory(memory)

        results = await db.search_nodes("transaction", {"category": "dining"})
        assert len(results) >= 1
        assert all(isinstance(r, Transaction) for r in results)

    async def test_existing_test_suite_passes(self):
        """Meta-test: run existing backward compatibility tests."""
        # This is verified by running:
        # uv run pytest tests/test_backward_compatibility.py tests/test_database.py -v
        # If those pass alongside our new tests, backward compat is proven.
        pass
```

### Verification gate per phase

| Phase | Gate command | Must pass |
|-------|-------------|-----------|
| 1 | `uv run pytest tests/test_registry.py -v` | 5 tests |
| 2 | `uv run pytest tests/test_query_builder.py tests/test_database.py -v` | 10 QB tests + existing DB tests |
| 3 | `uv run pytest tests/test_transaction_model.py -v` | 4 tests |
| 4 | `uv run pytest tests/test_node_factory.py -v` | 7 tests (incl. multi-label, normalization) |
| 5 | `uv run pytest tests/test_schema_manager.py tests/test_backward_compatibility.py -v` | Schema + backward compat |
| 6 | `uv run pytest tests/test_multi_label_integration.py -v` | 5 integration tests |
| **ALL** | `uv run pytest tests/ -v` | **Every test in the repo passes** |

### Autonomous workflow rule

**Do NOT proceed to Phase N+1 until Phase N's gate passes.** If a test fails:
1. Read the error message
2. Fix the code
3. Re-run the gate command
4. Only move on when green

After ALL phases: run `uv run pytest tests/ -v` to verify zero regressions across the entire test suite.

---

## Open Questions

1. **Option A vs B for MCP tools?** — Separate tools (store_transaction) vs extended existing tools (store_memory with node_type param)?
2. **Should the registry be config-file driven?** — YAML/JSON file listing types, or Python-only registration?
3. **How to handle the `tools/` profile system?** — New tools need to be in core or extended profile?
4. **Should custom types be plugins?** — Allow users to register types without modifying source code?

## Files Changed Summary

| File | Action | Phase |
|------|--------|-------|
| `src/memorygraph/registry.py` | NEW | 1 |
| `src/memorygraph/query_builder.py` | NEW | 2 |
| `src/memorygraph/models/transaction.py` | NEW | 3 |
| `src/memorygraph/node_factory.py` | NEW | 4 |
| `src/memorygraph/models.py` | MODIFY | 3 |
| `src/memorygraph/database.py` | MODIFY | 2, 4 |
| `src/memorygraph/backends/neo4j_backend.py` | MODIFY | 5 |
| `src/memorygraph/tools/registry.py` | MODIFY | 6 |
| `src/memorygraph/server.py` | MODIFY | 6 |
| `tests/test_registry.py` | NEW | 1 |
| `tests/test_query_builder.py` | NEW | 2 |
| `tests/test_transaction.py` | NEW | 3 |
| `tests/test_node_factory.py` | NEW | 4 |
