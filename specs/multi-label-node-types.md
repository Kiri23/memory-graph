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
