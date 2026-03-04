# Phase 7: MCP Tools

**Status:** [ ] Not started
**Files:** `src/memorygraph/tools/memory_tools.py` (MODIFY), `src/memorygraph/tools/search_tools.py` (MODIFY), `src/memorygraph/server.py` (MODIFY)
**Gate:** `uv run pytest tests/test_multi_label_integration.py -v` — 6 integration tests

## What this does

Extends existing MCP tools with an optional `node_type` parameter. When omitted, behavior is identical to today. Option B: extend existing tools (not new tools per type).

## Dependencies

- Phase 5 (store_node/get_node/search_nodes)

## Extending `store_memory`

```python
# In server.py tool definitions, add optional node_type parameter:
{
    "name": "store_memory",
    "description": "Store a new memory or custom node type...",
    "inputSchema": {
        "properties": {
            # ... existing properties ...
            "node_type": {
                "type": "string",
                "description": "Node type to store. Default: 'memory'. Other types: 'transaction'.",
                "default": "memory"
            },
            # For transaction type:
            "amount": {"type": "number", "description": "Transaction amount (required for node_type=transaction)"},
            "merchant": {"type": "string", "description": "Merchant name (required for node_type=transaction)"},
            "category": {"type": "string", "description": "Category (required for node_type=transaction)"},
            "payment_method": {"type": "string", "description": "Payment method"},
            "currency": {"type": "string", "description": "Currency code (default: USD)"},
            "date": {"type": "string", "description": "Transaction date (ISO format)"},
        }
    }
}
```

## Handler logic

```python
# In tools/memory_tools.py

async def handle_store_memory(context, kwargs):
    node_type = kwargs.pop("node_type", "memory")

    if node_type == "memory":
        # Existing code path — unchanged
        memory = Memory(type=kwargs.get("type"), title=kwargs["title"], ...)
        memory_id = await context.db.store_memory(memory)
        ...
    else:
        # Custom node type path
        config = context.db.registry.get(node_type)
        model = config.model
        node = model(**kwargs)  # Pydantic validates
        node_id = await context.db.store_node(node_type, node)
        return CallToolResult(content=[TextContent(
            text=json.dumps({"memory_id": node_id, "node_type": node_type})
        )])
```

## Extending `search_memories`

```python
{
    "name": "search_memories",
    "inputSchema": {
        "properties": {
            # ... existing properties ...
            "node_type": {
                "type": "string",
                "description": "Filter to a specific node type. Default: 'memory'.",
                "default": "memory"
            }
        }
    }
}
```

Handler routes to `search_nodes(type_name, filters)` when `node_type != "memory"`.

## Example flow

```text
1. Google Pay notification -> Tasker
2. Tasker -> Termux script
3. Script calls MCP: store_memory(node_type="transaction", amount=50,
                                   merchant="Pueblo", category="groceries",
                                   payment_method="google_pay", date="2026-03-03")
4. SQLite: INSERT INTO nodes (id, label='Transaction', properties='{amount:50,...}')
5. Later: search_memories(node_type="transaction", category="groceries")
6. Returns: [{amount: 50, merchant: "Pueblo", ...}]
```

## Tests: `tests/test_multi_label_integration.py`

End-to-end test proving the full chain:

```python
class TestMultiLabelIntegration:
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
        tx = Transaction(amount=42.50, merchant="Pueblo Supermarket", category="groceries",
                         payment_method="google_pay", date=datetime.now(timezone.utc))
        tx_id = await db.store_node("transaction", tx)
        retrieved = await db.get_node(tx_id)
        assert isinstance(retrieved, Transaction)
        assert retrieved.amount == 42.50
        assert retrieved.merchant == "Pueblo Supermarket"
        assert retrieved.payment_method == "google_pay"

    async def test_store_memory_still_works(self, db):
        memory = Memory(type=MemoryType.SOLUTION, title="Redis fix", content="Increased timeout")
        mid = await db.store_memory(memory)
        result = await db.get_memory(mid)
        assert isinstance(result, Memory)
        assert result.title == "Redis fix"

    async def test_cross_type_relationship(self, db):
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
        tx = Transaction(amount=25, merchant="Cafe", category="dining",
                         date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx)
        memory = Memory(type=MemoryType.TASK, title="Cafe review", content="Write review")
        await db.store_memory(memory)

        results = await db.search_nodes("transaction", {"category": "dining"})
        assert len(results) >= 1
        assert all(isinstance(r, Transaction) for r in results)

    async def test_backward_compat_old_memory_nodes(self, db):
        """Pre-existing Memory nodes retrievable via get_node."""
        memory = Memory(type=MemoryType.SOLUTION, title="Old node", content="Created before multi-label")
        mid = await db.store_memory(memory)

        result = await db.get_node(mid)
        assert isinstance(result, Memory)
        assert result.title == "Old node"

    async def test_transaction_with_context(self, db):
        """Transaction with context dict stores and retrieves correctly."""
        tx = Transaction(
            amount=100, merchant="AWS", category="infrastructure",
            date=datetime.now(timezone.utc),
            context={"project": "KiriInfra", "service": "EC2"}
        )
        tid = await db.store_node("transaction", tx)
        result = await db.get_node(tid)
        assert isinstance(result, Transaction)
        assert result.context["project"] == "KiriInfra"
        assert result.context["service"] == "EC2"
```

## Acceptance Criteria

- [ ] `store_memory(type="task", ...)` works exactly as before
- [ ] `store_memory(node_type="transaction", amount=50, ...)` stores a Transaction
- [ ] `search_memories(node_type="transaction", category="groceries")` returns Transactions
- [ ] `get_memory(id)` returns correct type regardless (via `get_node`)
- [ ] `create_relationship` works between Transaction and Memory
- [ ] Invalid `node_type` returns clear error message
- [ ] All 6 integration tests pass
- [ ] `uv run pytest tests/ -v` — zero regressions across entire suite
