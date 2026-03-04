# Spec: Multi-Label Node Types

**Status:** In Progress
**Author:** Christian Nogueras
**Branch:** `feature/multi-label-node-types`
**Created:** 2026-03-03
**Last revised:** 2026-03-03

## Goal

Extend MemoryGraph so each node type gets its own label and Pydantic model, instead of everything being `:Memory` with a `type` property. This enables custom schemas per domain (transactions, agents, etc.) while keeping backward compatibility with existing `:Memory` nodes.

## Scope

**In scope:** SQLite (default backend) and Neo4j backends.

**Out of scope:** Memgraph, FalkorDB, FalkorDBLite, LadybugDB, Turso, Cloud backends. These can be extended later following the same patterns — the registry/factory architecture is backend-agnostic.

## Motivation

MemoryGraph today stores everything as `:Memory` nodes. The `type` field (task, project, solution, etc.) is a string property — useful for filtering, but all nodes share the same schema. This limits the system when you need domain-specific properties:

- A **Transaction** needs: amount, merchant, category, payment_method, currency
- An **Agent** needs: trigger, schedule, last_run, status, capabilities
- A **Device** needs: hostname, ip, os, role

Forcing these into `content` as JSON strings loses native querying and indexing on both Neo4j and SQLite.

## Current Architecture

```text
MCP Tool Call
  → server.py (routes to handler)
    → tools/registry.py (validates input, creates Memory object)
      → database.py (builds Cypher, ~26 hardcoded :Memory references)
        → backends/neo4j_backend.py (executes query)
          → Neo4j: (:Memory {type: "task", title: "...", ...})

  OR (default path — SQLite):
    → sqlite_database.py (builds SQL, hardcoded label = 'Memory')
      → backends/sqlite_fallback.py (executes query)
        → SQLite: nodes table with label='Memory', properties=JSON
```

### Where labels are hardcoded

| File | Occurrences | Description |
|------|-------------|-------------|
| `database.py` | ~26 `:Memory` refs (7 MATCH, rest CREATE/MERGE/schema) | Cypher queries for Neo4j path |
| `sqlite_database.py` | ~20 `label = 'Memory'` refs | SQL queries for SQLite path |
| `neo4j_backend.py` | 14 schema statements | `CREATE INDEX/CONSTRAINT FOR (m:Memory)` |
| `sqlite_fallback.py` | Schema + `WHERE label = 'Memory'` | SQLite table indexes |
| `models.py` | `MemoryNode.to_neo4j_properties()` | Converts Memory → storage props |
| `database.py` | `_neo4j_to_memory()` → delegates to `utils/memory_parser.py:parse_memory_from_properties()` | Always deserializes as Memory |

### Key observations

1. `MemoryNode` (models.py:394) has a `labels: List[str]` field that is **defined but never used**. The infrastructure was partially anticipated.
2. SQLite's `nodes` table already has a `label TEXT` column (sqlite_fallback.py:159). Currently always `'Memory'`. Custom types just need different label values.
3. Both `database.py:210` and `neo4j_backend.py:116` call `result.data()` which converts Neo4j records to **plain Python dicts**, not raw `neo4j.graph.Node` objects. The NodeFactory must work with dicts.

## Target Architecture

```text
MCP Tool Call
  → server.py (routes to handler)
    → tools/registry.py (validates via NodeTypeRegistry)
      → database.py OR sqlite_database.py
        → NodeFactory (deserializes to correct model based on label)
          → Neo4j: (:Transaction {amount: 50, merchant: "Pueblo", ...})
          → SQLite: nodes(label='Transaction', properties={amount: 50, ...})
                    nodes(label='Memory', properties={type: "task", ...})
```

## Implementation Phases

---

### Phase 1: NodeTypeRegistry
**Status:** [ ] Not started
**Files:** `src/memorygraph/type_registry.py` (NEW)

> Named `type_registry.py` (not `registry.py`) to avoid confusion with existing `tools/registry.py`.

A central registry that maps type names to labels, models, and schema metadata. All fields are defined upfront with sensible defaults so the API is stable across all phases.

```python
from dataclasses import dataclass, field
from pydantic import BaseModel

@dataclass
class RelationshipConstraint:
    """Defines allowed relationships FROM this node type."""
    rel_type: str          # "RELATED_TO", "FEEDS"
    target_label: str      # "Memory", "Transaction" (or "*" for any)
    cardinality: str = "many"  # "one" or "many" (informational, not enforced)

@dataclass
class NodeTypeConfig:
    name: str              # "transaction"
    label: str             # "Transaction"
    model: type[BaseModel] # Transaction class
    indexes: list[str] = field(default_factory=list)      # ["amount", "merchant"]
    fulltext_fields: list[str] = field(default_factory=list)  # ["merchant", "note"]
    relationship_constraints: list[RelationshipConstraint] = field(default_factory=list)

class NodeTypeRegistry:
    def __init__(self):
        self._types: dict[str, NodeTypeConfig] = {}

    def register(self, config: NodeTypeConfig) -> None:
        if config.name in self._types:
            raise ValueError(f"Type '{config.name}' already registered")
        self._types[config.name] = config

    def get(self, type_name: str) -> NodeTypeConfig:
        if type_name not in self._types:
            raise KeyError(
                f"Unknown node type: '{type_name}'. "
                f"Registered types: {list(self._types.keys())}"
            )
        return self._types[type_name]

    def get_label(self, type_name: str) -> str:
        return self.get(type_name).label

    def get_model(self, type_name: str) -> type[BaseModel]:
        return self.get(type_name).model

    def all_types(self) -> list[NodeTypeConfig]:
        return list(self._types.values())

    def has_type(self, type_name: str) -> bool:
        return type_name in self._types


def get_default_registry() -> NodeTypeRegistry:
    """Create registry with default Memory type pre-registered."""
    from .models import Memory
    registry = NodeTypeRegistry()
    registry.register(NodeTypeConfig(
        name="memory",
        label="Memory",
        model=Memory,
        indexes=["id", "type", "title", "importance"],
        fulltext_fields=["title", "content", "summary"],
    ))
    return registry
```

**Acceptance criteria:**
- [ ] Registry can register and retrieve type configs
- [ ] Default "memory" type is pre-registered via `get_default_registry()`
- [ ] Unknown type raises `KeyError` with helpful message listing registered types
- [ ] Duplicate registration raises `ValueError`
- [ ] `all_types()` returns all registered configs
- [ ] Unit tests pass

---

### Phase 2: QueryBuilder (Neo4j path only)
**Status:** [ ] Not started
**Files:** `src/memorygraph/query_builder.py` (NEW), `src/memorygraph/database.py` (MODIFY)

Replaces ~26 hardcoded `:Memory` Cypher strings with parameterized queries. Also validates relationship types interpolated into Cypher (fixing existing injection vulnerability at database.py:830).

```python
import re
from .type_registry import NodeTypeRegistry

# Allowlist pattern: alphanumeric + underscore only
_VALID_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def _validate_identifier(value: str, kind: str = "identifier") -> str:
    """Validate that a string is safe for Cypher interpolation."""
    if not _VALID_IDENTIFIER.match(value):
        raise ValueError(f"Invalid {kind}: {value!r}. Must match [A-Za-z_][A-Za-z0-9_]*")
    return value

class QueryBuilder:
    def __init__(self, registry: NodeTypeRegistry):
        self.registry = registry

    def match(self, type_name: str, alias: str = "m") -> str:
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"MATCH ({alias}:{label})"

    def create(self, type_name: str, alias: str = "m") -> str:
        """NOTE: Prefer merge() for idempotent operations."""
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"CREATE ({alias}:{label} $props)"

    def merge(self, type_name: str, key_field: str = "id", alias: str = "m") -> str:
        """MERGE = create-or-update (upsert). Primary method for store operations."""
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        key_field = _validate_identifier(key_field, "key_field")
        return f"MERGE ({alias}:{label} {{{key_field}: ${key_field}}})"

    def return_with_labels(self, alias: str = "m") -> str:
        """Generate RETURN clause that includes explicit labels.

        IMPORTANT: Both database.py and neo4j_backend.py call result.data()
        which converts Neo4j records to plain dicts. So record["labels"]
        will be a plain list[str], NOT a frozenset. The NodeFactory works
        with these plain dicts.

        Usage: f"{qb.match('memory')} WHERE m.id = $id {qb.return_with_labels()}"
        Produces: MATCH (m:Memory) WHERE m.id = $id RETURN m, labels(m) AS labels
        """
        alias = _validate_identifier(alias, "alias")
        return f"RETURN {alias}, labels({alias}) AS labels"

    def relationship_match(
        self, from_type: str, to_type: str, rel_type: str,
        from_alias: str = "from", to_alias: str = "to", rel_alias: str = "r"
    ) -> str:
        """Generate MATCH for relationship queries with validated identifiers.

        Fixes existing Cypher injection vulnerability where relationship types
        were interpolated without validation (database.py:830).
        """
        from_label = _validate_identifier(self.registry.get_label(from_type), "label")
        to_label = _validate_identifier(self.registry.get_label(to_type), "label")
        rel_type = _validate_identifier(rel_type, "relationship_type")
        from_alias = _validate_identifier(from_alias, "alias")
        to_alias = _validate_identifier(to_alias, "alias")
        rel_alias = _validate_identifier(rel_alias, "alias")
        return f"MATCH ({from_alias}:{from_label})-[{rel_alias}:{rel_type}]->({to_alias}:{to_label})"

    def search(self, type_name: str, filters: dict) -> tuple[str, dict]:
        """Build a parameterized search query from filters.

        Args:
            type_name: Registered node type name
            filters: Dict of field_name → value to filter by.
                     Special keys:
                       "query" → text search across title/content/summary
                       "tags" → list of tags (ANY match)
                       "min_importance" → float threshold
                     All other keys → exact property match

        Returns:
            (cypher_query, parameters) tuple
        """
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        conditions = []
        params = {}

        for key, value in filters.items():
            if key == "query" and value:
                conditions.append(
                    "(m.title CONTAINS $search_query OR "
                    "m.content CONTAINS $search_query OR "
                    "m.summary CONTAINS $search_query)"
                )
                params["search_query"] = value
            elif key == "tags" and value:
                conditions.append("ANY(tag IN $filter_tags WHERE tag IN m.tags)")
                params["filter_tags"] = value
            elif key == "min_importance" and value is not None:
                conditions.append("m.importance >= $min_importance")
                params["min_importance"] = value
            else:
                # Exact match on a validated property name
                safe_key = _validate_identifier(key, "filter_field")
                param_name = f"filter_{safe_key}"
                conditions.append(f"m.{safe_key} = ${param_name}")
                params[param_name] = value

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"MATCH (m:{label}){where} RETURN m, labels(m) AS labels"
        return query, params
```

Migration strategy for `database.py`:
```python
# BEFORE:
query = "MATCH (m:Memory) WHERE m.id = $id RETURN m"

# AFTER (queries that need deserialization):
query = f"{self.qb.match(node_type)} WHERE m.id = $id {self.qb.return_with_labels()}"
# Produces: MATCH (m:Memory) WHERE m.id = $id RETURN m, labels(m) AS labels

# BEFORE (relationship with injection risk):
query = f"MATCH (from:Memory {{id: $from_id}})-[r:{relationship_type.value}]->(to:Memory ...)"

# AFTER (validated):
query = f"{self.qb.relationship_match('memory', 'memory', relationship_type.value)} ..."
```

**Acceptance criteria:**
- [ ] All ~26 `:Memory` references in database.py use QueryBuilder
- [ ] Existing Memory queries produce identical Cypher output
- [ ] New types produce correct labels
- [ ] No raw `:Memory` strings remain in database.py
- [ ] All identifier inputs validated (labels, aliases, field names, relationship types)
- [ ] Queries returning nodes for deserialization use `return_with_labels()`
- [ ] `search()` returns parameterized (query, params) tuples
- [ ] Relationship type interpolation is validated (fixes injection at line 830)

---

### Phase 3: Transaction Model
**Status:** [ ] Not started
**Files:** `src/memorygraph/models.py` (MODIFY)

> Transaction is added directly to `models.py` — no `models/` package migration needed. This avoids breaking existing imports like `from memorygraph.models import Memory`.

```python
# Added to models.py alongside Memory

class Transaction(BaseModel):
    """Domain-specific model for financial transactions."""
    id: Optional[str] = None
    amount: float
    merchant: str
    category: str  # groceries, dining, transport, etc.
    currency: str = "USD"
    payment_method: str = "unknown"  # google_pay, cash, credit, ath_movil
    date: datetime
    tags: List[str] = Field(default_factory=list)
    importance: float = Field(default=0.3, ge=0.0, le=1.0)
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, v: List[str]) -> List[str]:
        """Normalize tags to lowercase (same behavior as Memory)."""
        return [tag.lower().strip() for tag in v if tag.strip()]

    def to_storage_properties(self) -> Dict[str, Any]:
        """Convert to flat dict for storage (both Neo4j and SQLite).

        Mirrors MemoryNode.to_neo4j_properties() pattern but for Transaction.
        """
        props = {
            'id': self.id,
            'amount': self.amount,
            'merchant': self.merchant,
            'category': self.category,
            'currency': self.currency,
            'payment_method': self.payment_method,
            'date': self.date.isoformat(),
            'tags': self.tags,
            'importance': self.importance,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }
        if self.note:
            props['note'] = self.note
        return props
```

Register in the default registry (in `type_registry.py:get_default_registry()`):
```python
def get_default_registry() -> NodeTypeRegistry:
    from .models import Memory, Transaction
    registry = NodeTypeRegistry()
    registry.register(NodeTypeConfig(
        name="memory",
        label="Memory",
        model=Memory,
        indexes=["id", "type", "title", "importance"],
        fulltext_fields=["title", "content", "summary"],
    ))
    registry.register(NodeTypeConfig(
        name="transaction",
        label="Transaction",
        model=Transaction,
        indexes=["id", "amount", "merchant", "category", "date"],
        fulltext_fields=["merchant", "note"],
        relationship_constraints=[
            RelationshipConstraint(rel_type="RELATED_TO", target_label="*"),
            RelationshipConstraint(rel_type="CATEGORIZED_AS", target_label="Memory"),
        ]
    ))
    return registry
```

**Acceptance criteria:**
- [ ] Transaction model validates correctly (required fields, defaults)
- [ ] Missing required fields raise `ValidationError`
- [ ] Tags are auto-lowercased via `@field_validator`
- [ ] `to_storage_properties()` returns flat dict suitable for both backends
- [ ] Registered in default registry

---

### Phase 4: NodeFactory (Deserialization)
**Status:** [ ] Not started
**Files:** `src/memorygraph/node_factory.py` (NEW)

Provides a unified deserializer that returns the correct Pydantic model based on the node's label. Used by both the Neo4j and SQLite code paths.

#### Critical: How records arrive from backends

Both `database.py:210` and `neo4j_backend.py:116` call `result.data()` which returns **plain Python dicts**, not `neo4j.graph.Node` objects. So:

- `record["m"]` is a `dict` (properties flattened), not a Node with `.labels`
- `record["labels"]` is a `list[str]` (from `labels(m) AS labels` in Cypher)
- There is no `.labels` frozenset to access — it's already a plain list

For SQLite, we control the data directly: properties come from `json.loads()` and the label comes from the `label` column.

The existing `_neo4j_to_memory()` in database.py delegates to `utils/memory_parser.py:parse_memory_from_properties()`. The NodeFactory wraps this: for Memory nodes it delegates to `parse_memory_from_properties` (preserving existing behavior), and for custom types it uses `_normalize_properties` + direct Pydantic instantiation.

```python
from datetime import datetime, timezone
from typing import Any, Optional, Union
from pydantic import BaseModel

from .type_registry import NodeTypeRegistry, NodeTypeConfig
from .models import Memory
from .utils.memory_parser import parse_memory_from_properties


class NodeFactory:
    def __init__(self, registry: NodeTypeRegistry):
        self.registry = registry

    def from_record(self, properties: dict[str, Any], label: str) -> BaseModel:
        """Deserialize a storage record to the correct Python model.

        This is the unified entry point for BOTH backends:

        Neo4j path:
            record = result.data()[0]  # plain dict from .data()
            properties = record["m"]   # dict of node properties
            labels = record["labels"]  # list[str] from labels(m) AS labels
            node = factory.from_record(properties, labels[0])

        SQLite path:
            row = cursor.fetchone()
            properties = json.loads(row["properties"])
            label = row["label"]  # e.g. "Memory" or "Transaction"
            node = factory.from_record(properties, label)

        Args:
            properties: Flat dict of node properties
            label: The node's label string (e.g. "Memory", "Transaction")
        """
        config = self._resolve_type(label)

        # For Memory types, delegate to existing parser (preserves current behavior)
        if config and config.name == "memory":
            return parse_memory_from_properties(properties, source="NodeFactory")

        # For custom types, normalize and instantiate directly
        if config:
            normalized = self._normalize_properties(properties, config.model)
            return config.model(**normalized)

        # Unknown label — fall back to Memory parser
        return parse_memory_from_properties(properties, source="NodeFactory-fallback")

    def from_neo4j_record(self, record: dict[str, Any]) -> BaseModel:
        """Convenience method for Neo4j .data() results.

        Expects record to have "m" (properties dict) and "labels" (list[str])
        from a query like: RETURN m, labels(m) AS labels

        IMPORTANT: record["m"] is a plain dict (from .data()), NOT a
        neo4j.graph.Node. Do NOT call dict() on it or access .labels attribute.
        """
        properties = record["m"]  # already a dict from .data()
        labels = record.get("labels", [])

        # Pick the most specific label
        label = self._pick_specific_label(labels)
        return self.from_record(properties, label)

    def _resolve_type(self, label: str) -> Optional[NodeTypeConfig]:
        """Find registered type config matching a label."""
        for config in self.registry.all_types():
            if config.label == label:
                return config
        return None

    def _pick_specific_label(self, labels: list[str]) -> str:
        """From a list of labels, pick the most specific registered one.

        Priority: non-Memory registered label > Memory > first label > "Memory"

        Example: ["Memory", "Transaction"] → "Transaction"
        """
        memory_label = None
        for label in labels:
            config = self._resolve_type(label)
            if config:
                if config.name == "memory":
                    memory_label = label
                    continue
                return label  # First non-Memory registered label wins
        return memory_label or (labels[0] if labels else "Memory")

    def _normalize_properties(
        self, raw_props: dict[str, Any], model: type[BaseModel]
    ) -> dict[str, Any]:
        """Normalize storage properties before Pydantic instantiation.

        Handles:
        - ISO datetime strings → Python datetime (for date/datetime fields)
        - Neo4j temporal types → Python datetime (if neo4j driver types present)
        - context_* prefixed keys → nested context dict (Memory-specific)
        - Strips unknown keys not in the model
        """
        props = {}
        context = {}
        model_fields = model.model_fields

        for key, value in raw_props.items():
            # Regroup context_ prefixed keys (Memory convention)
            if key.startswith("context_"):
                context_key = key[len("context_"):]
                context[context_key] = value
                continue

            # Convert Neo4j temporal types (if present despite .data() call)
            if hasattr(value, 'to_native'):
                value = value.to_native()
            elif isinstance(value, str) and key in model_fields:
                field_type = model_fields[key].annotation
                if field_type is datetime or (
                    hasattr(field_type, '__origin__')
                    and datetime in getattr(field_type, '__args__', ())
                ):
                    try:
                        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass

            props[key] = value

        if context:
            props["context"] = context

        return props
```

**Acceptance criteria:**
- [ ] `from_record(props, "Transaction")` → Transaction object
- [ ] `from_record(props, "Memory")` → Memory object (delegates to existing parser)
- [ ] `from_record(props, "UnknownType")` → Memory fallback
- [ ] `from_neo4j_record({"m": {...}, "labels": [...]})` → correct model
- [ ] Multi-label records (e.g., `["Memory", "Transaction"]`) → most specific type
- [ ] ISO datetime strings parsed to datetime objects
- [ ] `context_*` prefixed properties regrouped into context dict
- [ ] Works with plain dicts (no dependency on neo4j.graph.Node)

---

### Phase 5: Database API — `store_node` / `get_node` / `search_nodes`
**Status:** [ ] Not started
**Files:** `src/memorygraph/database.py` (MODIFY), `src/memorygraph/sqlite_database.py` (MODIFY)

This is the core phase that makes everything work end-to-end. Adds three new methods to **both** database classes. Existing `store_memory` / `get_memory` / `search_memories` remain unchanged (backward compatible).

#### 5A: SQLiteMemoryDatabase (sqlite_database.py)

SQLite already has a `label` column in the `nodes` table. Custom types just use a different label value and store type-specific properties as JSON.

```python
# Added to SQLiteMemoryDatabase

def __init__(self, backend: SQLiteFallbackBackend):
    self.backend = backend
    self.registry = get_default_registry()
    self.factory = NodeFactory(self.registry)

async def store_node(self, type_name: str, node: BaseModel) -> str:
    """Store any registered node type.

    Args:
        type_name: Registered type name (e.g., "transaction")
        node: Pydantic model instance with a to_storage_properties() method
              or model_dump() fallback

    Returns:
        Node ID
    """
    config = self.registry.get(type_name)  # raises KeyError if unknown
    label = config.label

    if not hasattr(node, 'id') or not node.id:
        node.id = str(uuid.uuid4())

    node.updated_at = datetime.now(timezone.utc)

    # Get properties — prefer to_storage_properties() if available
    if hasattr(node, 'to_storage_properties'):
        properties = node.to_storage_properties()
    else:
        properties = node.model_dump(mode='python')
        # Convert datetimes to ISO strings for JSON storage
        for k, v in properties.items():
            if isinstance(v, datetime):
                properties[k] = v.isoformat()

    properties_json = json.dumps(properties)

    # MERGE behavior: update if exists, insert if not
    existing = await asyncio.to_thread(
        self.backend.execute_sync,
        "SELECT id FROM nodes WHERE id = ? AND label = ?",
        (node.id, label)
    )

    if existing:
        await asyncio.to_thread(
            self.backend.execute_sync,
            "UPDATE nodes SET properties = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND label = ?",
            (properties_json, node.id, label)
        )
    else:
        await asyncio.to_thread(
            self.backend.execute_sync,
            "INSERT INTO nodes (id, label, properties, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (node.id, label, properties_json)
        )

    self.backend.commit()
    return node.id

async def get_node(self, node_id: str) -> Optional[BaseModel]:
    """Retrieve any node by ID, returning the correct model type.

    Looks up the node across ALL labels, then uses NodeFactory
    to return the right Pydantic model.
    """
    result = await asyncio.to_thread(
        self.backend.execute_sync,
        "SELECT label, properties FROM nodes WHERE id = ?",
        (node_id,)
    )

    if not result:
        return None

    label = result[0]['label']
    properties = json.loads(result[0]['properties'])
    return self.factory.from_record(properties, label)

async def search_nodes(self, type_name: str, filters: dict) -> List[BaseModel]:
    """Search nodes of a specific type with property filters.

    Args:
        type_name: Registered type name (e.g., "transaction")
        filters: Dict of property_name → value for exact match filtering.
                 Special keys:
                   "query" → text search across common text fields
                   "tags" → list of tags (ANY match)
                   "min_importance" → float threshold

    Returns:
        List of correctly-typed Pydantic model instances
    """
    config = self.registry.get(type_name)
    label = config.label

    where_parts = ["label = ?"]
    params = [label]

    for key, value in filters.items():
        if key == "query" and value:
            pattern = f"%{value}%"
            # Search common text fields in JSON
            where_parts.append(
                "(json_extract(properties, '$.title') LIKE ? OR "
                "json_extract(properties, '$.content') LIKE ? OR "
                "json_extract(properties, '$.merchant') LIKE ? OR "
                "json_extract(properties, '$.note') LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])
        elif key == "tags" and value:
            # SQLite JSON array containment
            tag_conditions = []
            for tag in value:
                tag_conditions.append("properties LIKE ?")
                params.append(f'%"{tag}"%')
            where_parts.append(f"({' OR '.join(tag_conditions)})")
        elif key == "min_importance" and value is not None:
            where_parts.append("CAST(json_extract(properties, '$.importance') AS REAL) >= ?")
            params.append(value)
        else:
            where_parts.append(f"json_extract(properties, '$.{key}') = ?")
            params.append(value)

    sql = f"SELECT label, properties FROM nodes WHERE {' AND '.join(where_parts)}"

    rows = await asyncio.to_thread(
        self.backend.execute_sync, sql, tuple(params)
    )

    results = []
    for row in rows:
        props = json.loads(row['properties'])
        node = self.factory.from_record(props, row['label'])
        if node:
            results.append(node)
    return results
```

#### 5B: MemoryDatabase (database.py — Neo4j path)

```python
# Added to MemoryDatabase

def __init__(self, connection):
    self.connection = connection
    self.registry = get_default_registry()
    self.factory = NodeFactory(self.registry)
    self.qb = QueryBuilder(self.registry)

async def store_node(self, type_name: str, node: BaseModel) -> str:
    """Store any registered node type via Cypher MERGE."""
    config = self.registry.get(type_name)

    if not hasattr(node, 'id') or not node.id:
        node.id = str(uuid.uuid4())

    node.updated_at = datetime.now(timezone.utc)

    if hasattr(node, 'to_storage_properties'):
        properties = node.to_storage_properties()
    else:
        properties = node.model_dump(mode='python')
        for k, v in properties.items():
            if isinstance(v, datetime):
                properties[k] = v.isoformat()

    query = f"""
    {self.qb.merge(type_name)}
    SET m += $properties
    RETURN m.id as id
    """

    result = await self.connection.execute_write_query(
        query, {"id": node.id, "properties": properties}
    )

    if result:
        return result[0]["id"]
    raise DatabaseConnectionError(f"Failed to store {type_name} node: {node.id}")

async def get_node(self, node_id: str) -> Optional[BaseModel]:
    """Retrieve any node by ID, returning the correct model type.

    Queries across all registered labels and uses NodeFactory for deserialization.
    """
    # Try each registered type (most queries will hit on first try)
    for config in self.registry.all_types():
        query = f"{self.qb.match(config.name)} WHERE m.id = $id {self.qb.return_with_labels()}"
        result = await self.connection.execute_read_query(query, {"id": node_id})
        if result:
            return self.factory.from_neo4j_record(result[0])

    return None

async def search_nodes(self, type_name: str, filters: dict) -> List[BaseModel]:
    """Search nodes of a specific type with property filters."""
    query, params = self.qb.search(type_name, filters)
    results = await self.connection.execute_read_query(query, params)
    return [self.factory.from_neo4j_record(r) for r in results]
```

**Acceptance criteria:**
- [ ] `store_node("transaction", tx)` stores with correct label on both backends
- [ ] `get_node(id)` returns correctly-typed model on both backends
- [ ] `search_nodes("transaction", {"category": "dining"})` returns only Transactions
- [ ] Existing `store_memory` / `get_memory` / `search_memories` unchanged
- [ ] Cross-type `create_relationship` works between Transaction and Memory nodes
- [ ] `get_related_memories` returns mixed types correctly (via NodeFactory)

---

### Phase 6: Schema Manager
**Status:** [ ] Not started
**Files:** `src/memorygraph/backends/neo4j_backend.py` (MODIFY), `src/memorygraph/backends/sqlite_fallback.py` (MODIFY)

#### 6A: Neo4j — Dynamic schema from registry

Currently has 14 hardcoded schema statements for `:Memory`. Replace with dynamic generation from registry.

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

    # Relationship uniqueness constraint
    await self._execute_schema_statement(
        "CREATE CONSTRAINT relationship_id_unique IF NOT EXISTS FOR (r:RELATIONSHIP) REQUIRE r.id IS UNIQUE"
    )

    logger.info("Schema initialization completed")
```

#### 6B: SQLite — Add indexes for new label types

SQLite already handles any label via the `nodes` table. Just add performance indexes per type.

```python
# In sqlite_fallback.py

async def initialize_schema(self) -> None:
    # ... existing table creation stays the same ...

    # After creating base tables, add per-type indexes from registry
    for type_config in self.registry.all_types():
        label = type_config.label  # Safe: comes from Python code, not user input

        # Label-specific index for filtered queries
        try:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_nodes_{label.lower()} "
                f"ON nodes(label) WHERE label = '{label}'"
            )
        except sqlite3.Error:
            pass  # Index may already exist

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

**Acceptance criteria:**
- [ ] Each registered type gets its own constraints/indexes on Neo4j
- [ ] Each registered type gets JSON indexes on SQLite
- [ ] Existing `:Memory` indexes are reproduced identically
- [ ] Schema creation is idempotent (IF NOT EXISTS)
- [ ] All identifiers in DDL are validated against injection (Neo4j path)

---

### Phase 7: MCP Tools
**Status:** [ ] Not started
**Files:** `src/memorygraph/tools/memory_tools.py` (MODIFY), `src/memorygraph/tools/search_tools.py` (MODIFY), `src/memorygraph/server.py` (MODIFY)

**Decision: Option B — extend existing tools with `node_type` parameter.**

Rationale: Less code, works with both backends, doesn't explode the tool count as new types are added. The AI agent uses the same familiar tools. When `node_type` is omitted, behavior is identical to today.

#### Extending `store_memory`

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

#### Handler logic

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

#### Extending `search_memories`

```python
# search_memories gains optional node_type parameter
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

**Acceptance criteria:**
- [ ] `store_memory(type="task", ...)` works exactly as before (backward compatible)
- [ ] `store_memory(node_type="transaction", amount=50, merchant="Pueblo", ...)` stores a Transaction
- [ ] `search_memories(node_type="transaction", category="groceries")` returns Transactions
- [ ] `get_memory(id)` returns correct type regardless (via `get_node`)
- [ ] `create_relationship` works between Transaction and Memory nodes
- [ ] Invalid `node_type` returns clear error message

---

## Backward Compatibility

| Concern | Resolution |
|---------|------------|
| Existing `:Memory` nodes | Untouched — "memory" is default type in registry |
| Existing relationships | Work across labels — both Neo4j and SQLite use label-agnostic relationship tables |
| Existing MCP tools | Unchanged — `node_type` defaults to "memory", all params optional |
| Existing CLAUDE.md instructions | No changes needed |
| Existing tests | Must all pass — QueryBuilder produces identical Cypher for "memory" type |
| Upgrade path | Zero migration — new labels coexist with existing `'Memory'` label rows |

## Cross-Type Relationships

Relationships are label-agnostic on both backends:

**Neo4j:**
```cypher
MATCH (t:Transaction)-[:RELATED_TO]->(m:Memory) RETURN t, m
```

**SQLite:**
```sql
-- relationships table uses node IDs, not labels
SELECT * FROM relationships WHERE from_id = ? OR to_id = ?
```

## Example: Transaction Flow

```text
1. Google Pay notification → Tasker
2. Tasker → Termux script
3. Script calls MCP: store_memory(node_type="transaction", amount=50,
                                   merchant="Pueblo", category="groceries",
                                   payment_method="google_pay", date="2026-03-03")
4. SQLite: INSERT INTO nodes (id, label='Transaction', properties='{amount:50,...}')
   OR Neo4j: MERGE (t:Transaction {id: $id}) SET t += $properties
5. Later: search_memories(node_type="transaction", category="groceries")
6. Returns: [{amount: 50, merchant: "Pueblo", ...}]
```

## Verification Strategy

### How to run tests

```bash
cd ~/Code/memory-graph

# Run all tests (SQLite backend, no Neo4j needed)
uv run pytest tests/ -v

# Run only multi-label tests
uv run pytest tests/test_type_registry.py tests/test_query_builder.py tests/test_node_factory.py tests/test_multi_label_integration.py -v

# Run with coverage
uv run pytest tests/ --cov=memorygraph --cov-report=term-missing

# Run existing tests to verify nothing is broken
uv run pytest tests/test_database.py tests/test_backward_compatibility.py -v
```

### Test pattern (matches existing codebase)

Tests use **SQLiteFallbackBackend** with temp directories — no Neo4j instance needed:

```python
import pytest
import tempfile
from pathlib import Path
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase

class TestMyFeature:
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
```

### Phase 1 verification: `tests/test_type_registry.py`

```python
from memorygraph.type_registry import (
    NodeTypeRegistry, NodeTypeConfig, get_default_registry
)
from memorygraph.models import Memory, Transaction

class TestNodeTypeRegistry:
    def test_register_and_retrieve(self):
        """Register a type, retrieve it by name."""
        registry = NodeTypeRegistry()
        config = NodeTypeConfig(name="transaction", label="Transaction", model=Transaction, indexes=["id"])
        registry.register(config)
        assert registry.get_label("transaction") == "Transaction"
        assert registry.get_model("transaction") == Transaction

    def test_default_memory_type_registered(self):
        """The 'memory' type should be pre-registered."""
        registry = get_default_registry()
        assert registry.get_label("memory") == "Memory"
        assert registry.get_model("memory") == Memory

    def test_unknown_type_raises(self):
        """Requesting unknown type raises KeyError with helpful message."""
        registry = NodeTypeRegistry()
        with pytest.raises(KeyError, match="unknown_type"):
            registry.get_label("unknown_type")

    def test_duplicate_registration_raises(self):
        """Registering same name twice raises ValueError."""
        registry = NodeTypeRegistry()
        config = NodeTypeConfig(name="tx", label="Tx", model=Transaction, indexes=[])
        registry.register(config)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(config)

    def test_all_types_returns_all(self):
        """all_types() returns every registered config."""
        registry = get_default_registry()
        assert len(registry.all_types()) == 2  # memory + transaction
        names = [t.name for t in registry.all_types()]
        assert "memory" in names
        assert "transaction" in names

    def test_has_type(self):
        """has_type() returns True/False."""
        registry = get_default_registry()
        assert registry.has_type("memory") is True
        assert registry.has_type("nonexistent") is False
```

**Gate:** All 6 tests pass → Phase 1 is done.

### Phase 2 verification: `tests/test_query_builder.py`

```python
from memorygraph.query_builder import QueryBuilder, _validate_identifier
from memorygraph.type_registry import NodeTypeConfig, get_default_registry
from memorygraph.models import Transaction

class TestQueryBuilder:
    def setup_method(self):
        self.registry = get_default_registry()
        self.qb = QueryBuilder(self.registry)

    def test_match_memory_unchanged(self):
        assert self.qb.match("memory") == "MATCH (m:Memory)"

    def test_match_transaction(self):
        assert self.qb.match("transaction") == "MATCH (m:Transaction)"

    def test_create_memory(self):
        assert "CREATE (m:Memory" in self.qb.create("memory")

    def test_merge_memory(self):
        assert self.qb.merge("memory") == "MERGE (m:Memory {id: $id})"

    def test_merge_transaction(self):
        assert self.qb.merge("transaction") == "MERGE (m:Transaction {id: $id})"

    def test_custom_alias(self):
        assert self.qb.match("transaction", alias="t") == "MATCH (t:Transaction)"

    def test_return_with_labels(self):
        assert self.qb.return_with_labels() == "RETURN m, labels(m) AS labels"
        assert self.qb.return_with_labels("t") == "RETURN t, labels(t) AS labels"

    def test_search_builds_parameterized_query(self):
        query, params = self.qb.search("transaction", {"category": "groceries"})
        assert "Transaction" in query
        assert "filter_category" in params
        assert params["filter_category"] == "groceries"

    def test_search_with_text_query(self):
        query, params = self.qb.search("memory", {"query": "redis timeout"})
        assert "CONTAINS" in query
        assert params["search_query"] == "redis timeout"

    def test_relationship_match(self):
        result = self.qb.relationship_match("memory", "memory", "RELATED_TO")
        assert "RELATED_TO" in result
        assert ":Memory" in result

    def test_invalid_label_rejected(self):
        with pytest.raises((ValueError, KeyError)):
            self.qb.match("Memory}) DETACH DELETE m //")

    def test_invalid_alias_rejected(self):
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.match("memory", alias="m; DROP")

    def test_invalid_key_field_rejected(self):
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.merge("memory", key_field="id} SET m.admin=true //")

    def test_invalid_relationship_type_rejected(self):
        with pytest.raises(ValueError, match="Invalid"):
            self.qb.relationship_match("memory", "memory", "RELATED_TO]->(x) DETACH DELETE x //")
```

**Gate:** All 14 tests pass + `uv run pytest tests/test_database.py -v` still passes.

### Phase 3 verification: `tests/test_transaction_model.py`

```python
from datetime import datetime, timezone
from pydantic import ValidationError
from memorygraph.models import Transaction

class TestTransactionModel:
    def test_valid_transaction(self):
        t = Transaction(amount=50.0, merchant="Pueblo", category="groceries", date=datetime.now(timezone.utc))
        assert t.amount == 50.0
        assert t.merchant == "Pueblo"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            Transaction(amount=50.0, category="groceries", date=datetime.now(timezone.utc))
            # Missing: merchant

    def test_default_values(self):
        t = Transaction(amount=10, merchant="Test", category="test", date=datetime.now(timezone.utc))
        assert t.currency == "USD"
        assert t.payment_method == "unknown"
        assert t.importance == 0.3

    def test_tags_lowercased(self):
        t = Transaction(amount=10, merchant="Test", category="test",
                        date=datetime.now(timezone.utc), tags=["FOOD", "Weekly"])
        assert t.tags == ["food", "weekly"]

    def test_to_storage_properties(self):
        t = Transaction(amount=42.50, merchant="Pueblo", category="groceries",
                        date=datetime(2026, 3, 3, tzinfo=timezone.utc))
        props = t.to_storage_properties()
        assert props["amount"] == 42.50
        assert props["merchant"] == "Pueblo"
        assert isinstance(props["date"], str)  # ISO string
```

**Gate:** All 5 tests pass.

### Phase 4 verification: `tests/test_node_factory.py`

Tests use plain dicts (matching what `.data()` actually returns):

```python
from memorygraph.node_factory import NodeFactory
from memorygraph.type_registry import get_default_registry
from memorygraph.models import Memory, Transaction

class TestNodeFactory:
    def setup_method(self):
        self.registry = get_default_registry()
        self.factory = NodeFactory(self.registry)

    def test_memory_from_record(self):
        """Plain dict with Memory label → Memory object."""
        props = {"id": "1", "type": "task", "title": "Test", "content": "..."}
        result = self.factory.from_record(props, "Memory")
        assert isinstance(result, Memory)

    def test_transaction_from_record(self):
        """Plain dict with Transaction label → Transaction object."""
        props = {"id": "2", "amount": 50.0, "merchant": "Pueblo",
                 "category": "groceries", "date": "2026-03-03T00:00:00+00:00"}
        result = self.factory.from_record(props, "Transaction")
        assert isinstance(result, Transaction)
        assert result.amount == 50.0

    def test_unknown_label_falls_back_to_memory(self):
        """Unknown label → Memory fallback."""
        props = {"id": "3", "type": "general", "title": "X", "content": "Y"}
        result = self.factory.from_record(props, "WeirdType")
        assert isinstance(result, Memory)

    def test_neo4j_record_with_labels(self):
        """Simulated .data() result with labels list."""
        record = {
            "m": {"id": "4", "amount": 25.0, "merchant": "Cafe",
                  "category": "dining", "date": "2026-03-03T12:00:00+00:00"},
            "labels": ["Transaction"]
        }
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)

    def test_multi_label_resolves_to_specific(self):
        """["Memory", "Transaction"] → Transaction (most specific wins)."""
        record = {
            "m": {"id": "5", "amount": 25.0, "merchant": "Cafe",
                  "category": "dining", "date": "2026-03-03T12:00:00+00:00"},
            "labels": ["Memory", "Transaction"]
        }
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)

    def test_context_prefix_normalization(self):
        """context_project, context_source → nested context dict."""
        props = {"id": "6", "type": "task", "title": "Test", "content": "...",
                 "context_project_path": "/my/project", "context_session_id": "abc"}
        result = self.factory.from_record(props, "Memory")
        assert isinstance(result, Memory)

    def test_iso_datetime_string_parsed(self):
        """ISO datetime strings in date fields are parsed."""
        props = {"id": "7", "amount": 10.0, "merchant": "Test", "category": "test",
                 "date": "2026-03-03T18:00:00+00:00"}
        result = self.factory.from_record(props, "Transaction")
        assert isinstance(result, Transaction)
        assert isinstance(result.date, datetime)
```

**Gate:** All 7 tests pass.

### Phase 5 verification: `tests/test_database_api.py`

```python
import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
from memorygraph.sqlite_database import SQLiteMemoryDatabase
from memorygraph.models import Memory, MemoryType, Transaction

class TestDatabaseAPI:
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
        """get_node returns Memory for memory nodes, Transaction for tx nodes."""
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
        """Storing an unregistered type raises KeyError."""
        from pydantic import BaseModel
        class Foo(BaseModel):
            id: str = None
        with pytest.raises(KeyError):
            await db.store_node("foo", Foo())
```

**Gate:** All 5 tests pass.

### Phase 6 verification: `tests/test_schema_manager.py`

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

### Phase 7 verification: `tests/test_multi_label_integration.py`

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
```

### Verification gate per phase

| Phase | Gate command | Must pass |
|-------|-------------|-----------|
| 1 | `uv run pytest tests/test_type_registry.py -v` | 6 tests |
| 2 | `uv run pytest tests/test_query_builder.py tests/test_database.py -v` | 14 QB tests + existing DB tests |
| 3 | `uv run pytest tests/test_transaction_model.py -v` | 5 tests |
| 4 | `uv run pytest tests/test_node_factory.py -v` | 7 tests |
| 5 | `uv run pytest tests/test_database_api.py -v` | 5 tests |
| 6 | `uv run pytest tests/test_schema_manager.py tests/test_backward_compatibility.py -v` | Schema + backward compat |
| 7 | `uv run pytest tests/test_multi_label_integration.py -v` | 4 integration tests |
| **ALL** | `uv run pytest tests/ -v` | **Every test in the repo passes** |

### Autonomous workflow rule

**Do NOT proceed to Phase N+1 until Phase N's gate passes.** If a test fails:
1. Read the error message
2. Fix the code
3. Re-run the gate command
4. Only move on when green

After ALL phases: run `uv run pytest tests/ -v` to verify zero regressions across the entire test suite.

---

## Files Changed Summary

| File | Action | Phase |
|------|--------|-------|
| `src/memorygraph/type_registry.py` | NEW | 1 |
| `src/memorygraph/query_builder.py` | NEW | 2 |
| `src/memorygraph/node_factory.py` | NEW | 4 |
| `src/memorygraph/models.py` | MODIFY (add Transaction) | 3 |
| `src/memorygraph/database.py` | MODIFY (QueryBuilder + store_node/get_node/search_nodes) | 2, 5 |
| `src/memorygraph/sqlite_database.py` | MODIFY (store_node/get_node/search_nodes) | 5 |
| `src/memorygraph/backends/neo4j_backend.py` | MODIFY (dynamic schema) | 6 |
| `src/memorygraph/backends/sqlite_fallback.py` | MODIFY (per-type indexes) | 6 |
| `src/memorygraph/tools/memory_tools.py` | MODIFY (node_type param) | 7 |
| `src/memorygraph/tools/search_tools.py` | MODIFY (node_type param) | 7 |
| `src/memorygraph/server.py` | MODIFY (tool schema additions) | 7 |
| `tests/test_type_registry.py` | NEW | 1 |
| `tests/test_query_builder.py` | NEW | 2 |
| `tests/test_transaction_model.py` | NEW | 3 |
| `tests/test_node_factory.py` | NEW | 4 |
| `tests/test_database_api.py` | NEW | 5 |
| `tests/test_schema_manager.py` | NEW | 6 |
| `tests/test_multi_label_integration.py` | NEW | 7 |
