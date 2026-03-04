# Phase 6: Schema Manager

**Status:** [ ] Not started
**Files:** `src/memorygraph/backends/neo4j_backend.py` (MODIFY), `src/memorygraph/backends/sqlite_fallback.py` (MODIFY)
**Gate:** `uv run pytest tests/test_schema_manager.py tests/test_backward_compatibility.py -v`

## What this does

Replaces 14 hardcoded `:Memory` schema statements in neo4j_backend.py with dynamic generation from the registry. Adds per-type JSON indexes on SQLite.

## Dependencies

- Phase 1 (NodeTypeRegistry)

## 6A: Neo4j — Dynamic schema from registry

Currently has 14 hardcoded statements like `CREATE INDEX FOR (m:Memory)`. Replace with registry loop.

```python
# In neo4j_backend.py

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

    # Relationship indexes (shared)
    await self._execute_schema_statement(
        "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.context)"
    )
    await self._execute_schema_statement(
        "CREATE CONSTRAINT relationship_id_unique IF NOT EXISTS FOR (r:RELATIONSHIP) REQUIRE r.id IS UNIQUE"
    )

    logger.info("Schema initialization completed")
```

## 6B: SQLite — Per-type JSON indexes

SQLite already handles any label. Just add performance indexes.

```python
# In sqlite_fallback.py

async def initialize_schema(self) -> None:
    # ... existing table creation stays the same ...

    for type_config in self.registry.all_types():
        label = type_config.label  # Safe: from Python code, not user input

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
class TestSchemaManager:
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
