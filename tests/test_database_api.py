import pytest
import pytest_asyncio
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction

pytestmark = pytest.mark.asyncio


class TestDatabaseAPI:
    @pytest_asyncio.fixture
    async def db(self):
        """Both backend.initialize_schema() and db.initialize_schema() are
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
        tx = Transaction(id="ts-test", amount=10, merchant="Test", category="test",
                         date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx)
        original = await db.get_node("ts-test")
        original_created = original.created_at

        time.sleep(0.05)

        tx2 = Transaction(id="ts-test", amount=99, merchant="Updated", category="test",
                          date=datetime.now(timezone.utc))
        await db.store_node("transaction", tx2)
        updated = await db.get_node("ts-test")
        assert updated.amount == 99
        assert updated.created_at == original_created

    async def test_transaction_with_only_required_fields(self, db):
        tx = Transaction(amount=5.0, merchant="M", category="c",
                         date=datetime.now(timezone.utc))
        tid = await db.store_node("transaction", tx)
        result = await db.get_node(tid)
        assert isinstance(result, Transaction)
        assert result.currency == "USD"
        assert result.payment_method == "unknown"
        assert result.note is None
