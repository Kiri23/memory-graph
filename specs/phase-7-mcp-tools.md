# Phase 7: MCP Tools

**Status:** [ ] Not started
**Files:** `src/memorygraph/tools/memory_tools.py` (MODIFY), `src/memorygraph/tools/search_tools.py` (MODIFY), `src/memorygraph/server.py` (MODIFY)
**Gate:** `uv run pytest tests/test_multi_label_integration.py -v` — 9 tests (6 database + 3 MCP handler)

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

**Required new imports** (add to top of `tools/memory_tools.py`):
```python
import json  # needed for custom node type JSON responses
```

```python
# In tools/memory_tools.py — modified handle_store_memory
#
# IMPORTANT: The existing function signature is:
#   handle_store_memory(memory_db: MemoryDatabase, arguments: Dict[str, Any]) -> CallToolResult
# Do NOT change it. The first arg is the database directly, NOT a context wrapper.

@handle_tool_errors("store memory")
async def handle_store_memory(
    memory_db: MemoryDatabase,
    arguments: Dict[str, Any]
) -> CallToolResult:
    # --- NEW: Branch on node_type BEFORE validate_memory_input() ---
    # validate_memory_input() expects Memory fields (type, title, content).
    # Custom node types won't have those, so we must branch first.
    node_type = arguments.pop("node_type", "memory")

    if node_type != "memory":
        # Custom node type path — skip Memory-specific validation
        try:
            config = memory_db.registry.get(node_type)  # raises KeyError if invalid
        except KeyError:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=f"Error: Unknown node type '{node_type}'. "
                         f"Registered types: {[t.name for t in memory_db.registry.all_types()]}"
                )],
                isError=True
            )

        model = config.model

        # IMPORTANT: Filter arguments to only fields the target model accepts.
        # Without this, Memory-specific fields (type, title, content) that the
        # LLM sends alongside node_type would cause Pydantic ValidationError.
        valid_fields = set(model.model_fields.keys())
        filtered_args = {k: v for k, v in arguments.items() if k in valid_fields}

        node = model(**filtered_args)  # Pydantic validates required fields
        node_id = await memory_db.store_node(node_type, node)
        return CallToolResult(content=[TextContent(
            type="text",
            text=json.dumps({"memory_id": node_id, "node_type": node_type})
        )])

    # --- Existing Memory code path — unchanged below this line ---
    validate_memory_input(arguments)
    # ... rest of existing handler ...
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

**Handler code** (modify `tools/search_tools.py`):

```python
# In tools/search_tools.py — modified handle_search_memories
#
# IMPORTANT: The existing function signature is:
#   handle_search_memories(memory_db: MemoryDatabase, arguments: Dict[str, Any]) -> CallToolResult
# Do NOT change it. The first arg is the database directly, NOT a context wrapper.

import json  # add to imports

@handle_tool_errors("search memories")
async def handle_search_memories(
    memory_db: MemoryDatabase,
    arguments: Dict[str, Any]
) -> CallToolResult:
    node_type = arguments.pop("node_type", "memory")

    if node_type != "memory":
        # Custom node type path — use search_nodes() directly
        try:
            config = memory_db.registry.get(node_type)
        except KeyError:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=f"Error: Unknown node type '{node_type}'."
                )],
                isError=True
            )

        # Build filters from remaining arguments (exclude pagination/search meta)
        meta_keys = {"limit", "offset", "search_tolerance", "match_mode",
                     "relationship_filter", "include_relationships"}
        filters = {k: v for k, v in arguments.items() if k not in meta_keys}

        results = await memory_db.search_nodes(node_type, filters)

        if not results:
            return CallToolResult(
                content=[TextContent(type="text", text=f"No {node_type} nodes found.")]
            )

        results_text = f"Found {len(results)} {node_type} nodes:\n\n"
        for i, node in enumerate(results, 1):
            results_text += f"{i}. {node.model_dump_json()}\n\n"

        return CallToolResult(content=[TextContent(type="text", text=results_text)])

    # --- Existing Memory search path — unchanged below ---
    validate_search_input(arguments)
    # ... rest of existing handler ...
```

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

End-to-end tests proving BOTH the database layer AND the MCP handler routing:

```python
import json
import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction

pytestmark = pytest.mark.asyncio  # All tests in this module are async


class TestMultiLabelIntegration:
    """Database-level integration tests."""

    @pytest_asyncio.fixture
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


class TestMCPHandlerRouting:
    """MCP tool handler tests — verifies the node_type routing logic.

    These test the actual handler function, not just the database.

    IMPORTANT: The handler signature is:
        handle_store_memory(memory_db: MemoryDatabase, arguments: Dict[str, Any])
    The first arg is the database DIRECTLY — not a context wrapper.
    """

    @pytest_asyncio.fixture
    async def db(self):
        """Create a real database for handler testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "handler_test.db")
            backend = SQLiteFallbackBackend(db_path=db_path)
            await backend.connect()
            await backend.initialize_schema()
            db = SQLiteMemoryDatabase(backend)
            await db.initialize_schema()
            yield db
            await backend.disconnect()

    async def test_handler_routes_transaction_via_node_type(self, db):
        """store_memory with node_type='transaction' stores a Transaction, not Memory."""
        from memorygraph.tools.memory_tools import handle_store_memory
        result = await handle_store_memory(db, {
            "node_type": "transaction",
            "amount": 50.0,
            "merchant": "Pueblo",
            "category": "groceries",
            "date": datetime.now(timezone.utc).isoformat(),
        })
        # Result should contain JSON with node_id and node_type
        result_data = json.loads(result.content[0].text)
        assert result_data["node_type"] == "transaction"
        # Verify it's actually stored as a Transaction
        retrieved = await db.get_node(result_data["memory_id"])
        assert isinstance(retrieved, Transaction)

    async def test_handler_default_memory_path_unchanged(self, db):
        """store_memory WITHOUT node_type still stores a Memory (backward compat).

        NOTE: The existing Memory handler returns a plain string like
        "Memory stored successfully with ID: <uuid>", NOT JSON.
        We parse the ID from that string — do NOT json.loads() it.
        """
        from memorygraph.tools.memory_tools import handle_store_memory
        result = await handle_store_memory(db, {
            "type": "task",
            "title": "Test task",
            "content": "Some content",
        })
        # Existing handler returns: "Memory stored successfully with ID: <uuid>"
        result_text = result.content[0].text
        assert "Memory stored successfully" in result_text
        # Extract the UUID from the plain-text response
        memory_id = result_text.split("ID: ")[1].strip()
        retrieved = await db.get_memory(memory_id)
        assert isinstance(retrieved, Memory)
        assert retrieved.title == "Test task"

    async def test_handler_invalid_node_type_returns_error(self, db):
        """store_memory with unknown node_type returns a clear error, not a crash."""
        from memorygraph.tools.memory_tools import handle_store_memory
        result = await handle_store_memory(db, {
            "node_type": "nonexistent_type",
            "data": "whatever",
        })
        result_text = result.content[0].text
        assert "error" in result_text.lower() or "unknown" in result_text.lower()
```

## Acceptance Criteria

- [ ] `store_memory(type="task", ...)` works exactly as before
- [ ] `store_memory(node_type="transaction", amount=50, ...)` stores a Transaction
- [ ] `search_memories(node_type="transaction", category="groceries")` returns Transactions
- [ ] `get_memory(id)` returns correct type regardless (via `get_node`)
- [ ] `create_relationship` works between Transaction and Memory
- [ ] Invalid `node_type` returns clear error message (not a crash)
- [ ] Handler filters kwargs to target model fields (no Memory fields leaking to Transaction)
- [ ] All 9 tests pass (6 database integration + 3 MCP handler routing)
- [ ] `uv run pytest tests/ -v` — zero regressions across entire suite
