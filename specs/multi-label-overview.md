# Multi-Label Node Types — Overview

**Status:** In Progress
**Branch:** `feature/multi-label-node-types`
**Full spec:** `multi-label-node-types.md` (canonical reference, ~1700 lines)

## Goal

Extend MemoryGraph so each node type gets its own label and Pydantic model, instead of everything being `:Memory` with a `type` property. Enables custom schemas per domain (transactions, agents, etc.) while keeping backward compatibility.

## Scope

**In:** SQLite (default) and Neo4j (partial — store/get/search/create_relationship work for custom types; delete and get_related_memories still require `:Memory` label).
**Out:** Memgraph, FalkorDB, FalkorDBLite, LadybugDB, Turso, Cloud. (Same patterns apply later.)

## Architecture: Before → After

```text
BEFORE:
  MCP → server.py → tools/registry.py → database.py (hardcoded :Memory)
        → Neo4j: (:Memory {type: "task", ...})
        → SQLite: nodes(label='Memory', properties=JSON)

AFTER:
  MCP → server.py → tools/registry.py → NodeTypeRegistry → database.py
        → NodeFactory (picks correct model from label)
        → Neo4j: (:Transaction {amount: 50, ...})
        → SQLite: nodes(label='Transaction', properties=JSON)
```

## Phase Dependency Graph

```
Phase 1: NodeTypeRegistry ──┐
Phase 2: QueryBuilder ──────┤  (independent — tests use DUMMY models, not real Transaction)
Phase 3: Transaction Model ─┤
                             ▼
Phase 4: NodeFactory (needs 1 + 3)
                             ▼
Phase 5: Database API (needs 1 + 2 + 3 + 4)
                             ▼
Phase 6: Schema Manager (needs 1 + 5 — backends need registry injected via constructor)
Phase 7: MCP Tools (needs 5)
```

> **NOTE on Phase 1/2 independence:** Phase 1 and 2 tests use lightweight
> dummy Pydantic models (not real Transaction) so they can run before Phase 3.
> The dummy models are clearly marked with "DUMMY DATA" comments. After Phase 3
> creates the real Transaction model, `register_custom_types()` wires it into
> the registry — tested in Phase 4+ integration tests.

## Per-Phase Files

| Phase | File | Gate | Tests |
|-------|------|------|-------|
| 1 | [phase-1-registry.md](phase-1-registry.md) | `uv run pytest tests/test_type_registry.py -v` | 7 |
| 2 | [phase-2-query-builder.md](phase-2-query-builder.md) | `uv run pytest tests/test_query_builder.py tests/test_database.py -v` | 15 + existing |
| 3 | [phase-3-transaction.md](phase-3-transaction.md) | `uv run pytest tests/test_transaction_model.py tests/test_type_registry.py tests/test_query_builder.py -v` | 9 + Phase 1/2 green |
| 4 | [phase-4-node-factory.md](phase-4-node-factory.md) | `uv run pytest tests/test_node_factory.py -v` | 7 |
| 5 | [phase-5-database-api.md](phase-5-database-api.md) | `uv run pytest tests/test_database_api.py -v` | 10 |
| 6 | [phase-6-schema.md](phase-6-schema.md) | `uv run pytest tests/test_schema_manager.py tests/test_backward_compatibility.py -v` | 3+ |
| 7 | [phase-7-mcp-tools.md](phase-7-mcp-tools.md) | `uv run pytest tests/test_multi_label_integration.py -v` | 10 |
| **ALL** | — | `uv run pytest tests/ -v` | **Every test passes** |

## Files Changed (all phases)

| File | Action | Phase |
|------|--------|-------|
| `src/memorygraph/type_registry.py` | NEW | 1 |
| `src/memorygraph/query_builder.py` | NEW | 2 |
| `src/memorygraph/node_factory.py` | NEW | 4 |
| `src/memorygraph/protocols.py` | MODIFY | 5 |
| `src/memorygraph/models.py` | MODIFY | 3 |
| `src/memorygraph/database.py` | MODIFY | 2, 5 |
| `src/memorygraph/sqlite_database.py` | MODIFY | 5 |
| `src/memorygraph/backends/neo4j_backend.py` | MODIFY | 6 |
| `src/memorygraph/backends/sqlite_fallback.py` | MODIFY | 6 |
| `src/memorygraph/tools/memory_tools.py` | MODIFY | 7 |
| `src/memorygraph/tools/search_tools.py` | MODIFY | 7 |
| `src/memorygraph/server.py` | MODIFY | 7 |

## Autonomous Workflow Rule

**Do NOT proceed to Phase N+1 until Phase N's gate passes.** If a test fails:
1. Read the error
2. Fix the code
3. Re-run the gate
4. Only move on when green

After ALL phases: `uv run pytest tests/ -v` — zero regressions.

## Backward Compatibility

| Concern | Resolution |
|---------|------------|
| Existing `:Memory` nodes | Untouched — "memory" is default type |
| Existing relationships | `create_relationship` label filter removed in Phase 5C — cross-type relationships work. `get_related_memories` still filters by `label = 'Memory'` (returns `List[Tuple[Memory, Relationship]]`, cannot deserialize non-Memory nodes). |
| Existing MCP tools | `node_type` defaults to "memory" |
| Existing tests | Must all pass unchanged |
| Upgrade path | Zero migration needed |

## Key Codebase Facts

1. `MemoryNode` (models.py:394) has `labels: List[str]` — defined but never used
2. SQLite `nodes` table already has `label TEXT` column (sqlite_fallback.py:159)
3. `database.py:210` and `neo4j_backend.py:116` call `result.data()` → plain dicts, not neo4j.graph.Node
4. **Both backends** hardcode `label = 'Memory'` in relationship and delete queries:
   - **Neo4j** `create_relationship` (database.py:656-657): `MATCH (from:Memory {id: $from_id}) MATCH (to:Memory {id: $to_id})`
   - **Neo4j** `delete_memory` (database.py:594,605): `MATCH (m:Memory {id: $memory_id})`
   - **SQLite** `create_relationship` (sqlite_database.py:1023,1028): `SELECT id FROM nodes WHERE id = ? AND label = 'Memory'`
   - **SQLite** `get_related_memories` (sqlite_database.py:1158): `AND n.label = 'Memory'`
   - **SQLite** `delete_memory` (sqlite_database.py:920,937): `WHERE id = ? AND label = 'Memory'`

   **Status:** `create_relationship` existence checks are fixed in Phase 5C (label filter
   removed — only checks that nodes exist, no deserialization). `delete_memory` and
   `get_related_memories` keep their `label = 'Memory'` filter intentionally:
   - `delete_memory` — semantic contract is to delete Memory nodes only; a separate
     `delete_node` is needed for custom types.
   - `get_related_memories` — returns `List[Tuple[Memory, Relationship]]` and calls
     `_properties_to_memory()` to deserialize; removing the filter would feed Transaction
     properties into a Memory parser. A future `get_related_nodes` using NodeFactory is needed.

## Known Limitations (this release)

1. **`delete_memory` / `get_related_memories`** still filter by `label = 'Memory'` on both
   backends — intentionally kept (see Key Codebase Facts #4 for rationale). Follow-up
   needed: `delete_node` and `get_related_nodes` using NodeFactory.
2. **No `delete_node` or `update_node` API** — use `store_node` with existing ID for
   updates. For deletion, use direct SQL/Cypher (workarounds in Phase 5 spec).
3. **`recall_memories` not type-aware** — fuzzy search can't be scoped to `node_type`.
4. **`get_recent_activity` ignores non-Memory nodes** — filters by `type` property.
5. **Tag search is substring-based** — "food" matches "seafood" (inherited from existing code).

## Implementation Notes

See full spec for details on: SQLite JSON performance, Neo4j temporal types, concurrent write safety, error handling patterns, registry immutability.
