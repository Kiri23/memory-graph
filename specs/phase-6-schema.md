# Phase 6: Schema Manager

**Status:** [ ] Not started
**Files:** `src/memorygraph/backends/neo4j_backend.py` (MODIFY), `src/memorygraph/backends/sqlite_fallback.py` (MODIFY)
**Gate:** `uv run pytest tests/test_schema_manager.py tests/test_backward_compatibility.py -v`

## What this does

Replaces 14 hardcoded `:Memory` schema statements in neo4j_backend.py with dynamic generation from the registry. Adds per-type JSON indexes on SQLite.

## Dependencies

- Phase 1 (NodeTypeRegistry)
- Phase 5 (Database API — backends need registry available)

## 6-pre: Inject registry into backend classes

Both backends need access to the registry. Add a `registry` parameter to each backend's
constructor, or set it after construction from the database wrapper.

**Recommended approach:** Pass via constructor.

```python
# In neo4j_backend.py — modify __init__:
from ..type_registry import NodeTypeRegistry, get_default_registry, register_custom_types
from ..query_builder import _validate_identifier

class Neo4jBackend:
    def __init__(self, connection, registry: NodeTypeRegistry = None):
        self.connection = connection
        if registry is None:
            registry = get_default_registry()
            register_custom_types(registry)
        self.registry = registry

# In sqlite_fallback.py — modify __init__:
from ..type_registry import NodeTypeRegistry, get_default_registry, register_custom_types

class SQLiteFallbackBackend:
    def __init__(self, db_path: str, registry: NodeTypeRegistry = None, ...):
        # ... existing init ...
        if registry is None:
            registry = get_default_registry()
            register_custom_types(registry)
        self.registry = registry
```

The default `registry=None` + auto-creation preserves backward compatibility — existing
code that creates backends without a registry argument still works.

**IMPORTANT — Share the same registry instance:** Phase 5 adds `self.registry` to the
database wrapper classes (`SQLiteMemoryDatabase`, `MemoryDatabase`). Phase 6 adds
`self.registry` to the backend classes. To avoid two independent registries (where a
custom type registered on one is invisible to the other), the wrapper should pass its
registry to the backend:

```python
# In SQLiteMemoryDatabase.__init__ (Phase 5):
self.registry = get_default_registry()
register_custom_types(self.registry)
self.backend.registry = self.registry  # Share the same instance with backend

# In MemoryDatabase.__init__ (Phase 5):
self.registry = get_default_registry()
register_custom_types(self.registry)
self.connection.registry = self.registry  # Share with backend (if applicable)
```

Alternatively, pass the registry via constructor when creating the backend. Either way,
ensure one registry instance is shared — not two independent copies.

## 6A: Neo4j — Dynamic schema from registry

Currently has 14 hardcoded statements like `CREATE INDEX FOR (m:Memory)`. Replace with registry loop.

**Required import** (add to top of `neo4j_backend.py`):
```python
from ..query_builder import _validate_identifier
```

```python
# In neo4j_backend.py

async def _execute_schema_statement(self, statement: str) -> None:
    """Execute a single DDL statement, ignoring 'already exists' errors.

    Helper shared by initialize_schema(). If the backend has an existing
    method for this (e.g., execute_query with write=True), use that instead
    and wrap it with the same error handling.

    IMPORTANT: Only suppresses 'already exists' errors (expected for IF NOT
    EXISTS on older Neo4j versions). All other errors (network, auth, syntax)
    are re-raised so callers know schema setup failed.
    """
    try:
        await self.connection.execute_write_query(statement)
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.debug(f"Schema object already exists (OK): {e}")
        else:
            logger.error(f"Schema statement failed: {statement!r} — {e}")
            raise

async def initialize_schema(self) -> None:
    logger.info("Initializing Neo4j schema from type registry...")

    for type_config in self.registry.all_types():
        label = _validate_identifier(type_config.label, "label")

        # 1. Uniqueness constraint on id (required for MERGE)
        await self._execute_schema_statement(
            f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
        )

        # 2. Property indexes
        for field_name in type_config.indexes:
            field_name = _validate_identifier(field_name, "index_field")
            await self._execute_schema_statement(
                f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{field_name})"
            )

        # 3. Fulltext index (if configured)
        if type_config.fulltext_fields:
            fields = ", ".join(
                f"n.{_validate_identifier(f, 'field')}"
                for f in type_config.fulltext_fields
            )
            index_name = f"{label.lower()}_fulltext"
            await self._execute_schema_statement(
                f'CREATE FULLTEXT INDEX {index_name} IF NOT EXISTS '
                f'FOR (n:{label}) ON EACH [{fields}]'
            )

    # Relationship indexes (shared, label-agnostic)
    await self._execute_schema_statement(
        "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.context)"
    )
    # NOTE: This constraint targets a NODE labeled "RELATIONSHIP", NOT a relationship edge.
    # The existing codebase stores relationship metadata as separate :RELATIONSHIP nodes
    # (in addition to the actual graph edges). This constraint is preserved from the
    # hardcoded schema — do NOT confuse it with a constraint on relationship edges.
    await self._execute_schema_statement(
        "CREATE CONSTRAINT relationship_id_unique IF NOT EXISTS FOR (r:RELATIONSHIP) REQUIRE r.id IS UNIQUE"
    )

    logger.info("Schema initialization completed")
```

## 6B: SQLite — Per-type JSON indexes

SQLite already handles any label. Just add performance indexes.

**Required import** (add to top of `sqlite_fallback.py`):
```python
from ..query_builder import _validate_identifier
```

```python
# In sqlite_fallback.py

async def initialize_schema(self) -> None:
    # ... existing table creation stays the same ...

    for type_config in self.registry.all_types():
        # IMPORTANT: Validate label before using in DDL string interpolation.
        # SQLite DDL (CREATE INDEX) does NOT support parameterized WHERE clauses,
        # so string interpolation is required. Validation prevents injection if
        # custom types are ever registered from user input in the future.
        label = _validate_identifier(type_config.label, "label")

        # Label-specific index for filtered queries
        try:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_nodes_{label.lower()} "
                f"ON nodes(label) WHERE label = '{label}'"
            )
        except sqlite3.Error:
            pass

        # Property-specific JSON indexes
        for field_name in type_config.indexes:
            field_name = _validate_identifier(field_name, "index_field")
            try:
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{label.lower()}_{field_name} "
                    f"ON nodes(json_extract(properties, '$.{field_name}')) "
                    f"WHERE label = '{label}'"
                )
            except sqlite3.Error:
                pass

    # FTS5 for text search (existing, keep as-is)
    # ...
```

## Tests: `tests/test_schema_manager.py`

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

class TestSchemaManager:
    @pytest_asyncio.fixture
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

    async def test_memory_schema_exists(self, db):
        memory = Memory(type=MemoryType.TASK, title="Test", content="Content")
        mid = await db.store_memory(memory)
        result = await db.get_memory(mid)
        assert result.title == "Test"

    async def test_transaction_schema_exists(self, db):
        tx = Transaction(amount=10, merchant="Test", category="test",
                         date=datetime.now(timezone.utc))
        tid = await db.store_node("transaction", tx)
        result = await db.get_node(tid)
        assert isinstance(result, Transaction)

    async def test_schema_idempotent(self, db):
        await db.initialize_schema()  # second call
        memory = Memory(type=MemoryType.TASK, title="Test2", content="Content2")
        mid = await db.store_memory(memory)
        assert mid is not None
```

## Acceptance Criteria

- [ ] Each registered type gets its own constraints/indexes on Neo4j
- [ ] Each registered type gets JSON indexes on SQLite
- [ ] Existing `:Memory` indexes reproduced identically
- [ ] Schema creation is idempotent (IF NOT EXISTS)
- [ ] All identifiers in DDL validated (Neo4j path)
- [ ] Tests pass
