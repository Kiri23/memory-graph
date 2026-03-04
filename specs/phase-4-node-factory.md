# Phase 4: NodeFactory (Deserialization)

**Status:** [ ] Not started
**File:** `src/memorygraph/node_factory.py` (NEW)
**Gate:** `uv run pytest tests/test_node_factory.py -v` — 7 tests

## What this does

Unified deserializer that returns the correct Pydantic model based on label. Used by both Neo4j and SQLite code paths.

## Dependencies

- Phase 1 (NodeTypeRegistry)
- Phase 3 (Transaction model)

## Critical: How records arrive from backends

Both `database.py:210` and `neo4j_backend.py:116` call `result.data()` which returns **plain Python dicts**, not `neo4j.graph.Node` objects:

- `record["m"]` → `dict` (properties), not a Node with `.labels`
- `record["labels"]` → `list[str]` (from `labels(m) AS labels`)

For SQLite: properties come from `json.loads()`, label from the `label` column.

Existing `_neo4j_to_memory()` delegates to `utils/memory_parser.py:parse_memory_from_properties()`. NodeFactory wraps this: Memory nodes delegate to `parse_memory_from_properties`, custom types use `_normalize_properties` + Pydantic instantiation.

## Code

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

        Unified entry point for BOTH backends:
          Neo4j:  factory.from_record(record["m"], record["labels"][0])
          SQLite: factory.from_record(json.loads(row["properties"]), row["label"])
        """
        config = self._resolve_type(label)

        if config and config.name == "memory":
            return parse_memory_from_properties(properties, source="NodeFactory")

        if config:
            normalized = self._normalize_properties(properties, config.model)
            return config.model(**normalized)

        return parse_memory_from_properties(properties, source="NodeFactory-fallback")

    def from_neo4j_record(self, record: dict[str, Any]) -> BaseModel:
        """Convenience for Neo4j .data() results.

        Expects {"m": {props}, "labels": [str]} from RETURN m, labels(m) AS labels.
        IMPORTANT: record["m"] is a plain dict from .data(), NOT neo4j.graph.Node.
        """
        properties = record["m"]
        labels = record.get("labels", [])
        label = self._pick_specific_label(labels)
        return self.from_record(properties, label)

    def _resolve_type(self, label: str) -> Optional[NodeTypeConfig]:
        for config in self.registry.all_types():
            if config.label == label:
                return config
        return None

    def _pick_specific_label(self, labels: list[str]) -> str:
        """Priority: non-Memory registered label > Memory > first > "Memory"."""
        memory_label = None
        for label in labels:
            config = self._resolve_type(label)
            if config:
                if config.name == "memory":
                    memory_label = label
                    continue
                return label
        return memory_label or (labels[0] if labels else "Memory")

    def _normalize_properties(
        self, raw_props: dict[str, Any], model: type[BaseModel]
    ) -> dict[str, Any]:
        """Normalize storage properties before Pydantic instantiation.

        Handles: ISO strings -> datetime, Neo4j temporal types, context_* regrouping.
        """
        props = {}
        context = {}
        model_fields = model.model_fields

        for key, value in raw_props.items():
            if key.startswith("context_"):
                context[key[len("context_"):]] = value
                continue

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

## Tests: `tests/test_node_factory.py`

```python
from memorygraph.node_factory import NodeFactory
from memorygraph.type_registry import get_default_registry
from memorygraph.models import Memory, Transaction

class TestNodeFactory:
    def setup_method(self):
        self.registry = get_default_registry()
        self.factory = NodeFactory(self.registry)

    def test_memory_from_record(self):
        props = {"id": "1", "type": "task", "title": "Test", "content": "..."}
        result = self.factory.from_record(props, "Memory")
        assert isinstance(result, Memory)

    def test_transaction_from_record(self):
        props = {"id": "2", "amount": 50.0, "merchant": "Pueblo",
                 "category": "groceries", "date": "2026-03-03T00:00:00+00:00"}
        result = self.factory.from_record(props, "Transaction")
        assert isinstance(result, Transaction)
        assert result.amount == 50.0

    def test_unknown_label_falls_back_to_memory(self):
        props = {"id": "3", "type": "general", "title": "X", "content": "Y"}
        result = self.factory.from_record(props, "WeirdType")
        assert isinstance(result, Memory)

    def test_neo4j_record_with_labels(self):
        record = {
            "m": {"id": "4", "amount": 25.0, "merchant": "Cafe",
                  "category": "dining", "date": "2026-03-03T12:00:00+00:00"},
            "labels": ["Transaction"]
        }
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)

    def test_multi_label_resolves_to_specific(self):
        record = {
            "m": {"id": "5", "amount": 25.0, "merchant": "Cafe",
                  "category": "dining", "date": "2026-03-03T12:00:00+00:00"},
            "labels": ["Memory", "Transaction"]
        }
        result = self.factory.from_neo4j_record(record)
        assert isinstance(result, Transaction)

    def test_context_prefix_normalization(self):
        props = {"id": "6", "type": "task", "title": "Test", "content": "...",
                 "context_project_path": "/my/project", "context_session_id": "abc"}
        result = self.factory.from_record(props, "Memory")
        assert isinstance(result, Memory)

    def test_iso_datetime_string_parsed(self):
        props = {"id": "7", "amount": 10.0, "merchant": "Test", "category": "test",
                 "date": "2026-03-03T18:00:00+00:00"}
        result = self.factory.from_record(props, "Transaction")
        assert isinstance(result, Transaction)
        assert isinstance(result.date, datetime)
```

## Acceptance Criteria

- [ ] `from_record(props, "Transaction")` -> Transaction object
- [ ] `from_record(props, "Memory")` -> Memory (delegates to existing parser)
- [ ] `from_record(props, "UnknownType")` -> Memory fallback
- [ ] `from_neo4j_record({"m": {...}, "labels": [...]})` -> correct model
- [ ] Multi-label `["Memory", "Transaction"]` -> most specific type
- [ ] ISO datetime strings parsed, context_* regrouped
- [ ] Works with plain dicts (no neo4j.graph.Node dependency)
- [ ] All 7 tests pass
