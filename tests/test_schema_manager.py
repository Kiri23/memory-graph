import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction

pytestmark = pytest.mark.asyncio


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
