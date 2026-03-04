import json
import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction, RelationshipType

pytestmark = pytest.mark.asyncio


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
        """Cross-type relationship between Transaction and Memory (fixed in Phase 5C)."""
        tx = Transaction(amount=100, merchant="AWS", category="infrastructure",
                         date=datetime.now(timezone.utc))
        tx_id = await db.store_node("transaction", tx)

        memory = Memory(type=MemoryType.PROJECT, title="KiriInfra", content="VPS infrastructure")
        mem_id = await db.store_memory(memory)

        rel_id = await db.create_relationship(tx_id, mem_id, RelationshipType.RELATED_TO, context="AWS bill for KiriInfra")
        assert rel_id is not None

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
    """MCP tool handler tests — verifies the node_type routing logic."""

    @pytest_asyncio.fixture
    async def db(self):
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
        from memorygraph.tools.memory_tools import handle_store_memory
        result = await handle_store_memory(db, {
            "node_type": "transaction",
            "amount": 50.0,
            "merchant": "Pueblo",
            "category": "groceries",
            "date": datetime.now(timezone.utc).isoformat(),
        })
        result_data = json.loads(result.content[0].text)
        assert result_data["node_type"] == "transaction"
        retrieved = await db.get_node(result_data["memory_id"])
        assert isinstance(retrieved, Transaction)

    async def test_handler_default_memory_path_unchanged(self, db):
        from memorygraph.tools.memory_tools import handle_store_memory
        result = await handle_store_memory(db, {
            "type": "task",
            "title": "Test task",
            "content": "Some content",
        })
        result_text = result.content[0].text
        assert "Memory stored successfully" in result_text
        memory_id = result_text.split("ID: ")[1].strip()
        retrieved = await db.get_memory(memory_id)
        assert isinstance(retrieved, Memory)
        assert retrieved.title == "Test task"

    async def test_handler_get_memory_finds_transaction(self, db):
        from memorygraph.tools.memory_tools import handle_store_memory, handle_get_memory
        store_result = await handle_store_memory(db, {
            "node_type": "transaction",
            "amount": 75.0,
            "merchant": "Target",
            "category": "shopping",
            "date": datetime.now(timezone.utc).isoformat(),
        })
        result_data = json.loads(store_result.content[0].text)
        tx_id = result_data["memory_id"]

        get_result = await handle_get_memory(db, {"memory_id": tx_id})
        assert not getattr(get_result, 'isError', False)
        result_text = get_result.content[0].text
        assert "Target" in result_text or "75" in result_text

    async def test_handler_invalid_node_type_returns_error(self, db):
        from memorygraph.tools.memory_tools import handle_store_memory
        result = await handle_store_memory(db, {
            "node_type": "nonexistent_type",
            "data": "whatever",
        })
        result_text = result.content[0].text
        assert "error" in result_text.lower() or "unknown" in result_text.lower()
