# Phase 5: Database API — store_node / get_node / search_nodes

**Status:** [ ] Not started
**Files:** `src/memorygraph/protocols.py` (MODIFY), `src/memorygraph/database.py` (MODIFY), `src/memorygraph/sqlite_database.py` (MODIFY)
**Gate:** `uv run pytest tests/test_database_api.py -v` — 10 tests

## What this does

The core phase. Adds `store_node`, `get_node`, `search_nodes` to both database classes and the MemoryOperations protocol. Existing `store_memory` / `get_memory` / `search_memories` remain unchanged.

## Dependencies

- Phase 1 (NodeTypeRegistry)
- Phase 2 (QueryBuilder — Neo4j path)
- Phase 3 (Transaction model)
- Phase 4 (NodeFactory)

## 5-pre: MemoryOperations Protocol (protocols.py)

The protocol (protocols.py:7-49) defines the common interface all backends must satisfy. Add the new methods so type checkers enforce them.

```python
# Added to MemoryOperations in protocols.py (after existing methods)

from pydantic import BaseModel  # add to imports

class MemoryOperations(Protocol):
    # ... existing methods (store_memory, get_memory, etc.) unchanged ...

    async def store_node(self, type_name: str, node: BaseModel) -> str:
        """Store any registered node type and return its ID.

        Args:
            type_name: Registered type name (e.g., "memory", "transaction").
            node: Pydantic model instance. Must have an optional `id` field.
                  If id is None, one is generated. The input object is NOT mutated.

        Returns:
            The node's ID (generated or existing).

        Raises:
            KeyError: If type_name is not registered in the NodeTypeRegistry.
        """
        ...

    async def get_node(self, node_id: str) -> Optional[BaseModel]:
        """Retrieve any node by ID, returning the correct Pydantic model.

        Args:
            node_id: The node's unique ID.

        Returns:
            The correctly-typed Pydantic model, or None if not found.
        """
        ...

    async def search_nodes(
        self, type_name: str, filters: dict
    ) -> List[BaseModel]:
        """Search nodes of a specific type with property filters.

        Args:
            type_name: Registered type name to search within.
            filters: Dict of property_name -> value. Special keys:
                     "query" (text search), "tags" (ANY match), "min_importance".
                     Pagination: "limit" (default 100), "offset" (default 0).
                     These are extracted before building WHERE clauses.

        Returns:
            List of correctly-typed Pydantic model instances.

        Raises:
            KeyError: If type_name is not registered.
        """
        ...
```

## 5A: SQLiteMemoryDatabase (sqlite_database.py)

**Required new imports** (add to top of `sqlite_database.py`):
```python
import re  # for field name validation in search_nodes
from .type_registry import get_default_registry, register_custom_types
from .node_factory import NodeFactory
```

```python
# MODIFY SQLiteMemoryDatabase.__init__ — add registry + factory after existing self.backend line.
# The existing __init__ is:
#     def __init__(self, backend: SQLiteFallbackBackend):
#         self.backend = backend
# Add the new lines below it:

def __init__(self, backend: SQLiteFallbackBackend):
    self.backend = backend  # existing
    # --- NEW: wire registry and factory ---
    self.registry = get_default_registry()
    register_custom_types(self.registry)  # Wire Transaction + future custom types
    self.factory = NodeFactory(self.registry)

async def store_node(self, type_name: str, node: BaseModel) -> str:
    """Store any registered node type.

    NOTE: Does NOT mutate the input `node` object. Works on a copy of
    the properties to avoid side effects if the caller reuses the object.
    """
    config = self.registry.get(type_name)  # raises KeyError if unknown
    label = config.label

    # Generate ID FIRST (before existence check) and avoid mutating input
    node_id = node.id if hasattr(node, 'id') and node.id else str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Get properties — prefer to_storage_properties() if available
    if hasattr(node, 'to_storage_properties'):
        properties = node.to_storage_properties()
    else:
        properties = node.model_dump(mode='python')
        for k, v in properties.items():
            if isinstance(v, datetime):
                properties[k] = v.isoformat()

    # Stamp id and updated_at on the PROPERTIES copy, not the input object
    properties['id'] = node_id
    properties['updated_at'] = now.isoformat()

    # MERGE behavior: update if exists, insert if not
    existing = await asyncio.to_thread(
        self.backend.execute_sync,
        "SELECT id, properties FROM nodes WHERE id = ? AND label = ?",
        (node_id, label)
    )

    if existing:
        # IMPORTANT: Preserve original created_at from the existing record.
        # The new Pydantic model has a default created_at of now(), which would
        # silently overwrite the original creation timestamp.
        old_props = json.loads(existing[0]['properties'])
        if 'created_at' in old_props:
            properties['created_at'] = old_props['created_at']

        properties_json = json.dumps(properties)
        await asyncio.to_thread(
            self.backend.execute_sync,
            "UPDATE nodes SET properties = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND label = ?",
            (properties_json, node_id, label)
        )
    else:
        properties_json = json.dumps(properties)
        await asyncio.to_thread(
            self.backend.execute_sync,
            "INSERT INTO nodes (id, label, properties, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (node_id, label, properties_json)
        )

    self.backend.commit()
    return node_id

async def get_node(self, node_id: str) -> Optional[BaseModel]:
    """Retrieve any node by ID, returning the correct model type."""
    result = await asyncio.to_thread(
        self.backend.execute_sync,
        "SELECT label, properties FROM nodes WHERE id = ?",
        (node_id,)
    )

    if not result:
        return None

    label = result[0]['label']
    properties = json.loads(result[0]['properties'])
    return self.factory.from_record(properties, label)

async def search_nodes(self, type_name: str, filters: dict) -> List[BaseModel]:
    """Search nodes of a specific type with property filters.

    Supports pagination via 'limit' (default 100) and 'offset' (default 0)
    keys in the filters dict. These are extracted before building WHERE clauses.
    """
    config = self.registry.get(type_name)
    label = config.label

    # Extract pagination BEFORE iterating filters (these are not WHERE clauses)
    limit = int(filters.pop("limit", 100))
    offset = int(filters.pop("offset", 0))

    where_parts = ["label = ?"]
    params = [label]

    # Keys that have special handling — skip in generic property-filter branch
    _SPECIAL_KEYS = {"query", "tags", "min_importance"}

    for key, value in filters.items():
        if key == "query" and value:
            pattern = f"%{value}%"
            # Use fulltext_fields from registry (not a hard-coded list)
            text_fields = config.fulltext_fields or ["title", "content"]
            field_clauses = [
                f"json_extract(properties, '$.{f}') LIKE ?"
                for f in text_fields
            ]
            where_parts.append(f"({' OR '.join(field_clauses)})")
            params.extend([pattern] * len(text_fields))
        elif key == "tags" and value:
            tag_conditions = []
            for tag in value:
                tag_conditions.append("properties LIKE ?")
                params.append(f'%"{tag}"%')
            where_parts.append(f"({' OR '.join(tag_conditions)})")
        elif key == "min_importance" and value is not None:
            where_parts.append("CAST(json_extract(properties, '$.importance') AS REAL) >= ?")
            params.append(value)
        elif key in _SPECIAL_KEYS:
            continue  # falsy special key — skip silently
        else:
            # Validate field name to prevent JSON path injection
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
                raise ValueError(f"Invalid filter field: {key!r}")
            where_parts.append(f"json_extract(properties, '$.{key}') = ?")
            params.append(value)

    sql = (
        f"SELECT label, properties FROM nodes WHERE {' AND '.join(where_parts)}"
        f" ORDER BY updated_at DESC LIMIT {limit} OFFSET {offset}"
    )
    rows = await asyncio.to_thread(self.backend.execute_sync, sql, tuple(params))

    results = []
    for row in rows:
        props = json.loads(row['properties'])
        node = self.factory.from_record(props, row['label'])
        if node:
            results.append(node)
    return results
```

## 5B: MemoryDatabase (database.py — Neo4j path)

**Required new imports** (add to top of `database.py`):
```python
from .type_registry import get_default_registry, register_custom_types
from .node_factory import NodeFactory
from .query_builder import QueryBuilder, _validate_identifier
```

```python
# MODIFY MemoryDatabase.__init__ — add registry, factory, qb after existing self.connection line.
# The existing __init__ is:
#     def __init__(self, connection):
#         self.connection = connection
# Add the new lines below it:

def __init__(self, connection):
    self.connection = connection  # existing
    # --- NEW: wire registry, factory, and query builder ---
    self.registry = get_default_registry()
    register_custom_types(self.registry)  # Wire Transaction + future custom types
    self.factory = NodeFactory(self.registry)
    self.qb = QueryBuilder(self.registry)

async def store_node(self, type_name: str, node: BaseModel) -> str:
    """Store any registered node type via Cypher MERGE.

    NOTE: Does NOT mutate the input `node` object.
    """
    config = self.registry.get(type_name)

    node_id = node.id if hasattr(node, 'id') and node.id else str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    if hasattr(node, 'to_storage_properties'):
        properties = node.to_storage_properties()
    else:
        properties = node.model_dump(mode='python')
        for k, v in properties.items():
            if isinstance(v, datetime):
                properties[k] = v.isoformat()

    properties['id'] = node_id
    properties['updated_at'] = now.isoformat()

    # IMPORTANT: Remove created_at from the update properties so MERGE ON CREATE
    # sets it but ON MATCH preserves the original. Neo4j's += would overwrite it
    # with the new Pydantic model's default (now()) if we leave it in.
    created_at = properties.pop('created_at', now.isoformat())

    query = f"""
    {self.qb.merge(type_name)}
    ON CREATE SET m += $properties, m.created_at = $created_at
    ON MATCH SET m += $properties
    RETURN m.id as id
    """

    result = await self.connection.execute_write_query(
        query, {"id": node_id, "properties": properties, "created_at": created_at}
    )

    if result:
        return result[0]["id"]
    raise DatabaseConnectionError(f"Failed to store {type_name} node: {node_id}")

async def get_node(self, node_id: str) -> Optional[BaseModel]:
    """Retrieve any node by ID using label-specific UNION for index usage.

    IMPORTANT: A bare `MATCH (m) WHERE m.id = $id` cannot use label-specific
    indexes and causes a full graph scan. Instead, we UNION one MATCH per
    registered label so Neo4j uses the per-label uniqueness index on `id`.
    """
    parts = []
    for config in self.registry.all_types():
        label = _validate_identifier(config.label, "label")
        parts.append(
            f"MATCH (m:{label}) WHERE m.id = $id "
            f"RETURN m, labels(m) AS labels"
        )
    query = " UNION ".join(parts) + " LIMIT 1"

    result = await self.connection.execute_read_query(
        query, {"id": node_id}
    )
    if result:
        return self.factory.from_neo4j_record(result[0])
    return None

async def search_nodes(self, type_name: str, filters: dict) -> List[BaseModel]:
    """Search nodes of a specific type with property filters."""
    # Extract pagination before passing to QueryBuilder (not Cypher WHERE clauses)
    limit = int(filters.pop("limit", 100))
    offset = int(filters.pop("offset", 0))

    query, params = self.qb.search(type_name, filters)
    query += f" ORDER BY m.updated_at DESC SKIP {offset} LIMIT {limit}"
    results = await self.connection.execute_read_query(query, params)
    return [self.factory.from_neo4j_record(r) for r in results]
```

## Tests: `tests/test_database_api.py`

```python
import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction

pytestmark = pytest.mark.asyncio  # All tests in this module are async

class TestDatabaseAPI:
    @pytest_asyncio.fixture
    async def db(self):
        """NOTE: Both backend.initialize_schema() and db.initialize_schema() are
        called because the backend creates core tables and the wrapper adds
        Memory-specific indexes. Both use IF NOT EXISTS so double-init is safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            backend = SQLiteFallbackBackend(db_path=db_path)
            await backend.connect()
            await backend.initialize_schema()
            db = SQLiteMemoryDatabase(backend)
            await db.initialize_schema()
            yield db
            await backend.disconnect()

    async def test_store_and_get_transaction(self, db):
        tx = Transaction(amount=42.50, merchant="Pueblo", category="groceries",
                         payment_method="google_pay", date=datetime.now(timezone.utc))
        tx_id = await db.store_node("transaction", tx)
        retrieved = await db.get_node(tx_id)
        assert isinstance(retrieved, Transaction)
        assert retrieved.amount == 42.50
        assert retrieved.merchant == "Pueblo"

    async def test_store_memory_still_works(self, db):
        memory = Memory(type=MemoryType.SOLUTION, title="Redis fix", content="Increased timeout")
        mid = await db.store_memory(memory)
        result = await db.get_memory(mid)
        assert isinstance(result, Memory)
        assert result.title == "Redis fix"

    async def test_get_node_returns_correct_type(self, db):
        memory = Memory(type=MemoryType.TASK, title="Test", content="Content")
        mid = await db.store_memory(memory)
        tx = Transaction(amount=10, merchant="Cafe", category="dining",
                         date=datetime.now(timezone.utc))
        tid = await db.store_node("transaction", tx)

        mem_result = await db.get_node(mid)
        tx_result = await db.get_node(tid)
        assert isinstance(mem_result, Memory)
        assert isinstance(tx_result, Transaction)

    async def test_search_transactions_only(self, db):
        tx = Transaction(amount=25, merchant="Cafe", category="dining",
                         date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx)
        memory = Memory(type=MemoryType.TASK, title="Cafe review", content="Write review")
        await db.store_memory(memory)

        results = await db.search_nodes("transaction", {"category": "dining"})
        assert len(results) >= 1
        assert all(isinstance(r, Transaction) for r in results)

    async def test_unknown_type_raises(self, db):
        from pydantic import BaseModel
        class Foo(BaseModel):
            id: str = None
        with pytest.raises(KeyError):
            await db.store_node("foo", Foo())

    async def test_search_empty_results(self, db):
        results = await db.search_nodes("transaction", {"category": "nonexistent"})
        assert results == []

    async def test_store_node_does_not_mutate_input(self, db):
        tx = Transaction(amount=10, merchant="Test", category="test",
                         date=datetime.now(timezone.utc))
        original_id = tx.id  # None
        await db.store_node("transaction", tx)
        assert tx.id == original_id  # Still None

    async def test_store_node_idempotent(self, db):
        tx = Transaction(id="fixed-id", amount=10, merchant="Test", category="test",
                         date=datetime.now(timezone.utc))
        id1 = await db.store_node("transaction", tx)
        tx2 = Transaction(id="fixed-id", amount=99, merchant="Updated", category="test",
                          date=datetime.now(timezone.utc))
        id2 = await db.store_node("transaction", tx2)
        assert id1 == id2 == "fixed-id"
        result = await db.get_node("fixed-id")
        assert result.amount == 99

    async def test_store_node_preserves_created_at_on_update(self, db):
        """Updating a node via store_node must NOT overwrite the original created_at."""
        import time
        tx = Transaction(id="ts-test", amount=10, merchant="Test", category="test",
                         date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx)
        original = await db.get_node("ts-test")
        original_created = original.created_at

        time.sleep(0.05)  # ensure clock advances

        tx2 = Transaction(id="ts-test", amount=99, merchant="Updated", category="test",
                          date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx2)
        updated = await db.get_node("ts-test")
        assert updated.amount == 99
        assert updated.created_at == original_created  # must be preserved

    async def test_transaction_with_only_required_fields(self, db):
        tx = Transaction(amount=5.0, merchant="M", category="c",
                         date=datetime.now(timezone.utc))
        tid = await db.store_node("transaction", tx)
        result = await db.get_node(tid)
        assert isinstance(result, Transaction)
        assert result.currency == "USD"
        assert result.payment_method == "unknown"
        assert result.note is None
```

## Implementation Notes

**Concurrent write safety:** The SQLite SELECT-then-INSERT pattern is not atomic. However,
a naive `ON CONFLICT(id) DO UPDATE` UPSERT is **less safe** here because it ignores the
`label` column — a conflicting ID from a different node type would silently overwrite the
wrong node. The current SELECT-then-INSERT uses `WHERE id = ? AND label = ?` which is safer.

If atomicity becomes a concern, the correct UPSERT requires a **composite unique constraint**:
```sql
-- First, add composite unique constraint (in schema migration):
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_id_label ON nodes(id, label);

-- Then UPSERT is safe:
INSERT INTO nodes (id, label, properties, created_at, updated_at)
VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(id, label) DO UPDATE SET properties = excluded.properties, updated_at = CURRENT_TIMESTAMP;
```

For now, the SELECT-then-INSERT pattern is correct and sufficient for single-writer SQLite.

## 5C: Fix `create_relationship` for cross-type nodes

Both backends hardcode `label = 'Memory'` in the existence check inside
`create_relationship`. This is only an existence check (no deserialization), so
removing the label filter is safe — existing Memory nodes are still found by ID.

**SQLite** (sqlite_database.py:1020-1030) — change:
```python
# BEFORE (lines 1023, 1028):
"SELECT id FROM nodes WHERE id = ? AND label = 'Memory'"

# AFTER:
"SELECT id FROM nodes WHERE id = ?"
```

**Neo4j** (database.py:655-660) — change:
```python
# BEFORE (lines 656-657):
query = f"""
MATCH (from:Memory {{id: $from_id}})
MATCH (to:Memory {{id: $to_id}})
CREATE (from)-[r:{relationship_type.value} $properties]->(to)
RETURN r.id as id
"""

# AFTER:
query = f"""
MATCH (from {{id: $from_id}})
MATCH (to {{id: $to_id}})
CREATE (from)-[r:{relationship_type.value} $properties]->(to)
RETURN r.id as id
"""
```

**Why this is safe:**
- `create_relationship` only checks that the two nodes exist — it does NOT
  deserialize them into Memory objects. Removing the label filter lets it find
  Transaction nodes (or any future type) while still finding existing Memory
  nodes by their UUID.
- `id` is PRIMARY KEY (SQLite) / has a uniqueness constraint per label (Neo4j),
  so the lookup is still indexed.
- Existing Memory↔Memory relationships are unaffected — the nodes still exist
  and are still found by ID.

**What is NOT changed here:**
- `delete_memory` — keeps `label = 'Memory'` intentionally. It's `delete_memory`,
  not `delete_node`. Removing the filter would let it delete Transactions, which
  breaks the semantic contract.
- `get_related_memories` — keeps `label = 'Memory'` because it deserializes
  results as Memory objects (line 1175: `self._properties_to_memory()`). Removing
  the filter would return Transaction properties to a Memory parser, producing
  garbage. A future `get_related_nodes` using NodeFactory is needed instead.

## Known Gaps (deferred to follow-up)

**`delete_node` not included:** This phase adds `store_node`, `get_node`, `search_nodes` but
not `delete_node`.

**WARNING — Neo4j `delete_memory` matches `:Memory` label only:**
The existing `delete_memory` in `database.py:604` uses `MATCH (m:Memory {id: $memory_id})`.
This means it will **NOT** delete non-Memory nodes (e.g., Transaction) on the Neo4j backend.
The SQLite `delete_memory` (`sqlite_database.py`) also matches `label = 'Memory'` in its
WHERE clause. **Neither backend deletes custom node types via `delete_memory()`.**

For this release, deleting custom nodes requires direct SQL/Cypher:
```sql
-- SQLite workaround:
DELETE FROM relationships WHERE from_id = '<node_id>' OR to_id = '<node_id>';
DELETE FROM nodes WHERE id = '<node_id>';
```
```cypher
-- Neo4j workaround:
MATCH (n {id: $node_id}) DETACH DELETE n
```
A typed `delete_node(node_id: str) -> bool` that queries by ID without label filtering is
needed as a follow-up. Until then, document this limitation for users.

**`update_node` not included:** The existing `update_memory` is Memory-specific (it expects
Memory fields like `type`, `title`, `content`). For custom types, `store_node` with an
existing ID serves as the update path — the MERGE/upsert behavior overwrites all properties.
A typed `update_node(node_id: str, type_name: str, **updates) -> BaseModel` that supports
partial updates should be added in a follow-up.

## Acceptance Criteria

- [ ] `MemoryOperations` protocol includes `store_node`, `get_node`, `search_nodes`
- [ ] `SQLiteMemoryDatabase` and `MemoryDatabase` satisfy the updated protocol
- [ ] `store_node("transaction", tx)` stores with correct label on both backends
- [ ] `get_node(id)` returns correctly-typed model on both backends
- [ ] `search_nodes("transaction", {"category": "dining"})` returns only Transactions
- [ ] Existing `store_memory` / `get_memory` / `search_memories` unchanged
- [ ] `store_node` does NOT mutate the input object
- [ ] Cross-type `create_relationship` works between Transaction and Memory (label filter
  removed from existence checks in both backends — see 5C)
- [ ] All 10 tests pass
