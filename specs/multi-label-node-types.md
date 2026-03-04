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

```
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

```
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
class QueryBuilder:
    def __init__(self, registry: NodeTypeRegistry): ...

    def match(self, type_name: str, alias: str = "m") -> str:
        label = self.registry.get_label(type_name)
        return f"MATCH ({alias}:{label})"

    def create(self, type_name: str, alias: str = "m") -> str:
        label = self.registry.get_label(type_name)
        return f"CREATE ({alias}:{label} $props)"

    def merge(self, type_name: str, key_field: str = "id", alias: str = "m") -> str:
        """MERGE = create-or-update (upsert). Used by store_memory."""
        label = self.registry.get_label(type_name)
        return f"MERGE ({alias}:{label} {{{key_field}: ${key_field}}})"

    def search(self, type_name: str, filters: dict) -> tuple[str, dict]:
        # Returns (cypher_query, parameters)
        ...
```

Migration strategy for `database.py`:
```python
# BEFORE:
query = "MATCH (m:Memory) WHERE m.id = $id RETURN m"

# AFTER:
query = f"{self.qb.match(node_type)} WHERE m.id = $id RETURN m"
```

**Acceptance criteria:**
- [ ] All 50+ queries in database.py use QueryBuilder
- [ ] Existing Memory queries produce identical Cypher
- [ ] New types produce correct labels
- [ ] No raw `:Memory` strings remain in database.py

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

```python
class NodeFactory:
    def __init__(self, registry: NodeTypeRegistry): ...

    def from_neo4j_record(self, record) -> BaseModel:
        """Deserialize Neo4j record to correct Python model."""
        labels = record.get("labels", [])
        # Find matching type in registry
        for type_config in self.registry.all_types():
            if type_config.label in labels:
                return type_config.model(**record)
        # Fallback to Memory
        return Memory(**record)
```

**Acceptance criteria:**
- [ ] Neo4j records with `:Transaction` label → Transaction object
- [ ] Neo4j records with `:Memory` label → Memory object (backward compatible)
- [ ] Unknown labels fall back to Memory
- [ ] Relationships between different node types work

---

### Phase 5: Schema Manager
**Status:** [ ] Not started
**Files:** `src/memorygraph/backends/neo4j_backend.py` (MODIFY)

Currently has 12 hardcoded schema statements for `:Memory`. Replace with dynamic schema from registry.

```python
def ensure_schema(self):
    for type_config in self.registry.all_types():
        label = type_config.label
        # Uniqueness constraint on id
        self.execute(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")
        # Indexes for configured fields
        for field in type_config.indexes:
            self.execute(f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{field})")
```

**Acceptance criteria:**
- [ ] Each registered type gets its own constraints/indexes
- [ ] Existing `:Memory` indexes unchanged
- [ ] New types get indexes on registration
- [ ] Schema creation is idempotent

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

```
1. Google Pay notification → Tasker
2. Tasker → Termux script
3. Script calls MCP: store_transaction(amount=50, merchant="Pueblo", category="groceries")
4. QueryBuilder: CREATE (t:Transaction {amount: 50, merchant: "Pueblo", ...})
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

```python
class TestNodeFactory:
    def setup_method(self):
        self.registry = get_default_registry()
        self.registry.register(NodeTypeConfig(name="transaction", label="Transaction", model=Transaction, indexes=[]))
        self.factory = NodeFactory(self.registry)

    def test_memory_record_returns_memory(self):
        """Record with Memory label → Memory object."""
        record = {"labels": ["Memory"], "id": "1", "type": "task", "title": "Test", "content": "..."}
        result = self.factory.from_record(record)
        assert isinstance(result, Memory)

    def test_transaction_record_returns_transaction(self):
        """Record with Transaction label → Transaction object."""
        record = {"labels": ["Transaction"], "id": "2", "amount": 50.0, "merchant": "Pueblo", "category": "groceries", "date": "2026-03-03T00:00:00Z"}
        result = self.factory.from_record(record)
        assert isinstance(result, Transaction)
        assert result.amount == 50.0

    def test_unknown_label_falls_back_to_memory(self):
        """Record with unknown label → Memory fallback."""
        record = {"labels": ["WeirdType"], "id": "3", "type": "general", "title": "X", "content": "Y"}
        result = self.factory.from_record(record)
        assert isinstance(result, Memory)
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
| 2 | `uv run pytest tests/test_query_builder.py tests/test_database.py -v` | QB tests + existing DB tests |
| 3 | `uv run pytest tests/test_transaction_model.py -v` | 4 tests |
| 4 | `uv run pytest tests/test_node_factory.py -v` | 3 tests |
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
