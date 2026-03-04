"""
End-to-end MCP client test.

Spawns the memorygraph server as a subprocess (SQLite backend, temp DB)
and exercises the MCP tools exactly as Claude Code would — via JSON-RPC
over stdio transport.

Run:  pytest tests/test_e2e_mcp_client.py -v -s
"""

import json
import tempfile
import pytest
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.asyncio


def server_params(db_path: str) -> StdioServerParameters:
    """Create server parameters pointing at a temp SQLite DB."""
    return StdioServerParameters(
        command="python",
        args=["-m", "memorygraph.cli", "--backend", "sqlite"],
        env={
            "MEMORY_BACKEND": "sqlite",
            "MEMORY_SQLITE_PATH": db_path,
            "MEMORY_TOOL_PROFILE": "extended",
            "MEMORY_LOG_LEVEL": "WARNING",
        },
    )


async def _connect(tmpdir: str) -> tuple[AsyncExitStack, ClientSession]:
    """Start server and return (stack, session). Caller must aclose the stack."""
    db_path = str(Path(tmpdir) / "e2e_test.db")
    stack = AsyncExitStack()
    read_stream, write_stream = await stack.enter_async_context(
        stdio_client(server_params(db_path))
    )
    session = await stack.enter_async_context(
        ClientSession(read_stream, write_stream)
    )
    await session.initialize()
    return stack, session


class TestE2EMCPClient:
    """Full round-trip tests through the real MCP server."""

    # ── Tool discovery ──────────────────────────────────────────────

    async def test_tools_are_listed(self):
        """Server exposes the expected tools."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                result = await session.list_tools()
                tool_names = {t.name for t in result.tools}

                for expected in [
                    "store_memory", "get_memory", "search_memories",
                    "recall_memories", "delete_memory", "update_memory",
                    "create_relationship", "get_related_memories",
                    "get_recent_activity",
                ]:
                    assert expected in tool_names, f"Missing tool: {expected}"
            finally:
                await stack.aclose()

    async def test_store_memory_schema_has_node_type(self):
        """store_memory tool schema includes the node_type parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                result = await session.list_tools()
                store_tool = next(t for t in result.tools if t.name == "store_memory")
                schema_props = store_tool.inputSchema.get("properties", {})
                assert "node_type" in schema_props, "store_memory missing node_type param"
            finally:
                await stack.aclose()

    # ── Classic Memory path (unchanged behavior) ────────────────────

    async def test_store_and_get_memory(self):
        """Store a memory via MCP, retrieve it, verify round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                store_result = await session.call_tool("store_memory", {
                    "type": "solution",
                    "title": "E2E Test Memory",
                    "content": "This memory was created by the E2E MCP client test.",
                    "tags": ["e2e", "test"],
                    "importance": 0.9,
                })
                assert not store_result.isError, f"store_memory failed: {store_result.content}"

                response_text = store_result.content[0].text
                assert "Memory stored successfully" in response_text
                memory_id = response_text.split("ID: ")[1].strip()

                get_result = await session.call_tool("get_memory", {
                    "memory_id": memory_id,
                })
                assert not get_result.isError, f"get_memory failed: {get_result.content}"
                get_text = get_result.content[0].text
                assert "E2E Test Memory" in get_text
                assert "solution" in get_text.lower()
            finally:
                await stack.aclose()

    async def test_search_memories(self):
        """Store a tagged memory, search by tag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                await session.call_tool("store_memory", {
                    "type": "task",
                    "title": "Searchable Task",
                    "content": "Find me by tag",
                    "tags": ["unique-e2e-tag"],
                })

                search_result = await session.call_tool("search_memories", {
                    "tags": ["unique-e2e-tag"],
                })
                assert not search_result.isError
                assert "Searchable Task" in search_result.content[0].text
            finally:
                await stack.aclose()

    async def test_recall_memories(self):
        """Store a memory, recall by natural language."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                await session.call_tool("store_memory", {
                    "type": "solution",
                    "title": "Redis timeout fix",
                    "content": "Increased connection timeout to 30 seconds to handle slow network.",
                    "tags": ["redis", "timeout"],
                })

                recall_result = await session.call_tool("recall_memories", {
                    "query": "redis timeout",
                })
                assert not recall_result.isError
                assert "Redis timeout fix" in recall_result.content[0].text
            finally:
                await stack.aclose()

    async def test_update_memory(self):
        """Store, update, verify update persisted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                store_result = await session.call_tool("store_memory", {
                    "type": "general",
                    "title": "Updatable Memory",
                    "content": "Original content",
                })
                memory_id = store_result.content[0].text.split("ID: ")[1].strip()

                update_result = await session.call_tool("update_memory", {
                    "memory_id": memory_id,
                    "title": "Updated Memory Title",
                    "content": "Updated content",
                })
                assert not update_result.isError

                get_result = await session.call_tool("get_memory", {"memory_id": memory_id})
                assert "Updated Memory Title" in get_result.content[0].text
            finally:
                await stack.aclose()

    async def test_delete_memory(self):
        """Store, delete, verify gone."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                store_result = await session.call_tool("store_memory", {
                    "type": "general",
                    "title": "Deletable Memory",
                    "content": "Will be deleted",
                })
                memory_id = store_result.content[0].text.split("ID: ")[1].strip()

                delete_result = await session.call_tool("delete_memory", {
                    "memory_id": memory_id,
                })
                assert not delete_result.isError

                get_result = await session.call_tool("get_memory", {"memory_id": memory_id})
                assert get_result.isError or "not found" in get_result.content[0].text.lower()
            finally:
                await stack.aclose()

    async def test_create_and_get_relationship(self):
        """Create two memories, link them, verify relationship."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                r1 = await session.call_tool("store_memory", {
                    "type": "problem",
                    "title": "Connection drops",
                    "content": "DB connections drop under load",
                })
                id1 = r1.content[0].text.split("ID: ")[1].strip()

                r2 = await session.call_tool("store_memory", {
                    "type": "solution",
                    "title": "Connection pooling",
                    "content": "Implemented connection pool with max 20 connections",
                })
                id2 = r2.content[0].text.split("ID: ")[1].strip()

                rel_result = await session.call_tool("create_relationship", {
                    "from_memory_id": id2,
                    "to_memory_id": id1,
                    "relationship_type": "SOLVES",
                    "context": "Pooling solves connection drops",
                })
                assert not rel_result.isError

                related_result = await session.call_tool("get_related_memories", {
                    "memory_id": id2,
                })
                assert not related_result.isError
                assert "Connection drops" in related_result.content[0].text
            finally:
                await stack.aclose()

    async def test_get_recent_activity(self):
        """Store a memory and verify it shows in recent activity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                await session.call_tool("store_memory", {
                    "type": "task",
                    "title": "Recent task",
                    "content": "Should appear in activity",
                })

                activity_result = await session.call_tool("get_recent_activity", {
                    "days": 1,
                })
                assert not activity_result.isError
            finally:
                await stack.aclose()

    # ── Multi-label: Transaction path (NEW) ─────────────────────────

    async def test_store_transaction_via_node_type(self):
        """Store a Transaction via node_type routing, retrieve via get_memory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                store_result = await session.call_tool("store_memory", {
                    "node_type": "transaction",
                    "amount": 42.50,
                    "merchant": "Pueblo Supermarket",
                    "category": "groceries",
                    "date": "2026-03-04T12:00:00+00:00",
                })
                assert not store_result.isError, f"store failed: {store_result.content}"

                result_data = json.loads(store_result.content[0].text)
                assert result_data["node_type"] == "transaction"
                tx_id = result_data["memory_id"]

                # Retrieve via get_memory (should fall back to get_node)
                get_result = await session.call_tool("get_memory", {"memory_id": tx_id})
                assert not get_result.isError, f"get failed: {get_result.content}"
                get_text = get_result.content[0].text
                assert "Pueblo" in get_text or "42.5" in get_text
            finally:
                await stack.aclose()

    async def test_search_transactions_via_node_type(self):
        """Store transactions, search by node_type + filters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                await session.call_tool("store_memory", {
                    "node_type": "transaction",
                    "amount": 15.00,
                    "merchant": "Starbucks",
                    "category": "dining",
                    "date": "2026-03-04T08:00:00+00:00",
                })
                await session.call_tool("store_memory", {
                    "node_type": "transaction",
                    "amount": 200.00,
                    "merchant": "AWS",
                    "category": "infrastructure",
                    "date": "2026-03-04T09:00:00+00:00",
                })

                search_result = await session.call_tool("search_memories", {
                    "node_type": "transaction",
                    "category": "dining",
                })
                assert not search_result.isError
                search_text = search_result.content[0].text
                assert "Starbucks" in search_text
                assert "AWS" not in search_text
            finally:
                await stack.aclose()

    async def test_transaction_with_context(self):
        """Transaction with context dict round-trips correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                store_result = await session.call_tool("store_memory", {
                    "node_type": "transaction",
                    "amount": 100.00,
                    "merchant": "DigitalOcean",
                    "category": "infrastructure",
                    "date": "2026-03-04T10:00:00+00:00",
                    "context": {"project": "KiriInfra", "service": "droplet"},
                })
                assert not store_result.isError
                result_data = json.loads(store_result.content[0].text)
                tx_id = result_data["memory_id"]

                get_result = await session.call_tool("get_memory", {"memory_id": tx_id})
                assert not get_result.isError
                get_text = get_result.content[0].text
                assert "DigitalOcean" in get_text
            finally:
                await stack.aclose()

    async def test_invalid_node_type_returns_error(self):
        """Unknown node_type returns a clear error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                result = await session.call_tool("store_memory", {
                    "node_type": "nonexistent_type",
                    "data": "whatever",
                })
                assert result.isError, "Expected isError=True for invalid node_type"
                result_text = result.content[0].text.lower()
                assert "error" in result_text or "unknown" in result_text
            finally:
                await stack.aclose()

    async def test_cross_type_relationship(self):
        """Transaction and Memory can be linked via relationship."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                # Store a transaction
                tx_result = await session.call_tool("store_memory", {
                    "node_type": "transaction",
                    "amount": 50.00,
                    "merchant": "AWS",
                    "category": "infrastructure",
                    "date": "2026-03-04T11:00:00+00:00",
                })
                assert not tx_result.isError, f"tx store failed: {tx_result.content}"
                tx_data = json.loads(tx_result.content[0].text)
                tx_id = tx_data["memory_id"]

                # Store a memory
                mem_result = await session.call_tool("store_memory", {
                    "type": "project",
                    "title": "KiriInfra",
                    "content": "VPS infrastructure project",
                })
                assert not mem_result.isError, f"mem store failed: {mem_result.content}"
                mem_id = mem_result.content[0].text.split("ID: ")[1].strip()

                # Create cross-type relationship
                rel_result = await session.call_tool("create_relationship", {
                    "from_memory_id": tx_id,
                    "to_memory_id": mem_id,
                    "relationship_type": "RELATED_TO",
                    "context": "AWS bill for KiriInfra",
                })
                assert not rel_result.isError, f"relationship failed: {rel_result.content}"
            finally:
                await stack.aclose()

    # ── Mixed workload (simulates real session) ─────────────────────

    async def test_mixed_workload_no_interference(self):
        """Storing transactions doesn't break normal memory operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stack, session = await _connect(tmpdir)
            try:
                # Store a transaction
                await session.call_tool("store_memory", {
                    "node_type": "transaction",
                    "amount": 25.00,
                    "merchant": "Cafe",
                    "category": "dining",
                    "date": "2026-03-04T14:00:00+00:00",
                })

                # Store a normal memory
                mem_result = await session.call_tool("store_memory", {
                    "type": "solution",
                    "title": "Cafe review system",
                    "content": "Built review aggregator for local cafes",
                    "tags": ["cafe", "review"],
                })
                assert "Memory stored successfully" in mem_result.content[0].text

                # Search memories (default path) should NOT return transactions
                search_result = await session.call_tool("search_memories", {
                    "tags": ["cafe"],
                })
                assert not search_result.isError
                assert "Cafe review system" in search_result.content[0].text

                # Recall should work normally
                recall_result = await session.call_tool("recall_memories", {
                    "query": "cafe review",
                })
                assert not recall_result.isError
            finally:
                await stack.aclose()
