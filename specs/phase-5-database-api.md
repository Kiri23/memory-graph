# Phase 5: Database API — store_node / get_node / search_nodes

**Status:** [ ] Not started
**Files:** `src/memorygraph/protocols.py` (MODIFY), `src/memorygraph/database.py` (MODIFY), `src/memorygraph/sqlite_database.py` (MODIFY)
**Gate:** `uv run pytest tests/test_database_api.py -v` — 9 tests

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

        Returns:
            List of correctly-typed Pydantic model instances.

        Raises:
            KeyError: If type_name is not registered.
        """
        ...
```

## 5A: SQLiteMemoryDatabase (sqlite_database.py)

```python
# Added to SQLiteMemoryDatabase

def __init__(self, backend: SQLiteFallbackBackend):
    self.backend = backend
    self.registry = get_default_registry()
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

    properties_json = json.dumps(properties)

    # MERGE behavior: update if exists, insert if not
    existing = await asyncio.to_thread(
        self.backend.execute_sync,
        "SELECT id FROM nodes WHERE id = ? AND label = ?",
        (node_id, label)
    )

    if existing:
        await asyncio.to_thread(
            self.backend.execute_sync,
            "UPDATE nodes SET properties = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND label = ?",
            (properties_json, node_id, label)
        )
    else:
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
    """Search nodes of a specific type with property filters."""
    config = self.registry.get(type_name)
    label = config.label

    where_parts = ["label = ?"]
    params = [label]

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
        else:
            # Validate field name to prevent JSON path injection
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
                raise ValueError(f"Invalid filter field: {key!r}")
            where_parts.append(f"json_extract(properties, '$.{key}') = ?")
            params.append(value)

    sql = f"SELECT label, properties FROM nodes WHERE {' AND '.join(where_parts)}"
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

```python
# Added to MemoryDatabase

def __init__(self, connection):
    self.connection = connection
    self.registry = get_default_registry()
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

    query = f"""
    {self.qb.merge(type_name)}
    SET m += $properties
    RETURN m.id as id
    """

    result = await self.connection.execute_write_query(
        query, {"id": node_id, "properties": properties}
    )

    if result:
        return result[0]["id"]
    raise DatabaseConnectionError(f"Failed to store {type_name} node: {node_id}")

async def get_node(self, node_id: str) -> Optional[BaseModel]:
    """Retrieve any node by ID. Single query across all registered labels."""
    all_labels = [
        _validate_identifier(c.label, "label")
        for c in self.registry.all_types()
    ]
    query = (
        f"MATCH (m) WHERE m.id = $id "
        f"AND ANY(lbl IN labels(m) WHERE lbl IN $registered_labels) "
        f"RETURN m, labels(m) AS labels LIMIT 1"
    )
    result = await self.connection.execute_read_query(
        query, {"id": node_id, "registered_labels": all_labels}
    )
    if result:
        return self.factory.from_neo4j_record(result[0])
    return None

async def search_nodes(self, type_name: str, filters: dict) -> List[BaseModel]:
    """Search nodes of a specific type with property filters."""
    query, params = self.qb.search(type_name, filters)
    results = await self.connection.execute_read_query(query, params)
    return [self.factory.from_neo4j_record(r) for r in results]
```

## Tests: `tests/test_database_api.py`

```python
import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction

class TestDatabaseAPI:
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

**Concurrent write safety:** The SQLite SELECT-then-INSERT pattern is not atomic. Consider using `INSERT OR REPLACE` (UPSERT) instead:
```sql
INSERT INTO nodes (id, label, properties, created_at, updated_at)
VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(id) DO UPDATE SET properties = excluded.properties, updated_at = CURRENT_TIMESTAMP
```

## Acceptance Criteria

- [ ] `MemoryOperations` protocol includes `store_node`, `get_node`, `search_nodes`
- [ ] `SQLiteMemoryDatabase` and `MemoryDatabase` satisfy the updated protocol
- [ ] `store_node("transaction", tx)` stores with correct label on both backends
- [ ] `get_node(id)` returns correctly-typed model on both backends
- [ ] `search_nodes("transaction", {"category": "dining"})` returns only Transactions
- [ ] Existing `store_memory` / `get_memory` / `search_memories` unchanged
- [ ] `store_node` does NOT mutate the input object
- [ ] Cross-type `create_relationship` works between Transaction and Memory
- [ ] All 9 tests pass
