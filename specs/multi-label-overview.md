# Multi-Label Node Types — Overview

**Status:** In Progress
**Branch:** `feature/multi-label-node-types`
**Full spec:** `multi-label-node-types.md` (canonical reference, ~1700 lines)

## Goal

Extend MemoryGraph so each node type gets its own label and Pydantic model, instead of everything being `:Memory` with a `type` property. Enables custom schemas per domain (transactions, agents, etc.) while keeping backward compatibility.

## Scope

**In:** SQLite (default) and Neo4j backends.
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
| 3 | [phase-3-transaction.md](phase-3-transaction.md) | `uv run pytest tests/test_transaction_model.py -v` | 6 |
| 4 | [phase-4-node-factory.md](phase-4-node-factory.md) | `uv run pytest tests/test_node_factory.py -v` | 7 |
| 5 | [phase-5-database-api.md](phase-5-database-api.md) | `uv run pytest tests/test_database_api.py -v` | 9 |
| 6 | [phase-6-schema.md](phase-6-schema.md) | `uv run pytest tests/test_schema_manager.py tests/test_backward_compatibility.py -v` | 3+ |
| 7 | [phase-7-mcp-tools.md](phase-7-mcp-tools.md) | `uv run pytest tests/test_multi_label_integration.py -v` | 9 |
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
| Existing relationships | Label-agnostic (use node IDs, not labels) |
| Existing MCP tools | `node_type` defaults to "memory" |
| Existing tests | Must all pass unchanged |
| Upgrade path | Zero migration needed |

## Key Codebase Facts

1. `MemoryNode` (models.py:394) has `labels: List[str]` — defined but never used
2. SQLite `nodes` table already has `label TEXT` column (sqlite_fallback.py:159)
3. `database.py:210` and `neo4j_backend.py:116` call `result.data()` → plain dicts, not neo4j.graph.Node
4. **SQLite** relationships table uses IDs only — cross-type relationships work automatically
5. **Neo4j** `create_relationship` and `delete_memory` match `:Memory` label — they will
   **NOT work** for non-Memory nodes. `create_relationship` (database.py:655) uses
   `MATCH (from:Memory {id: $from_id}) MATCH (to:Memory {id: $to_id})`. This is a known
   limitation for this release — Phase 5 integration tests use SQLite only. A follow-up
   must update Neo4j relationship/delete queries to be label-agnostic (match by ID only).

## Implementation Notes

See full spec for details on: SQLite JSON performance, Neo4j temporal types, concurrent write safety, error handling patterns, registry immutability.
