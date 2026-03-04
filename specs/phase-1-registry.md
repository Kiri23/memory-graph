# Phase 1: NodeTypeRegistry

**Status:** [ ] Not started
**File:** `src/memorygraph/type_registry.py` (NEW)
**Gate:** `uv run pytest tests/test_type_registry.py -v` — 7 tests

## What this does

A central registry mapping type names to labels, Pydantic models, and schema metadata. Named `type_registry.py` (not `registry.py`) to avoid confusion with `tools/registry.py`.

## Dependencies

None — this is the foundation.

## Code

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
    """Create registry with default Memory type pre-registered.

    NOTE: Only registers 'memory' here. Custom types (e.g. Transaction)
    are added in their own phases via register_custom_types().
    """
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


def register_custom_types(registry: NodeTypeRegistry) -> None:
    """Register custom node types. Called after Phase 3 models exist.

    Separate from get_default_registry() to avoid a circular dependency:
    Phase 1 defines the registry, Phase 3 defines Transaction, then
    this function wires them together.
    """
    from .models import Transaction
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
```

## Tests: `tests/test_type_registry.py`

> **IMPORTANT — DUMMY MODEL:** These tests use a lightweight `DummyCustomModel`
> instead of the real `Transaction` model (which doesn't exist until Phase 3).
> This keeps Phase 1 truly independent. After Phase 3 is complete, the
> `test_all_types_with_custom_types` test should be **duplicated or extended**
> in a post-Phase-3 integration test that calls `register_custom_types()` with
> the real Transaction model. The dummy tests here remain valid — they prove the
> registry mechanics work regardless of model type.

```python
import pytest
from typing import Optional
from pydantic import BaseModel
from memorygraph.type_registry import (
    NodeTypeRegistry, NodeTypeConfig, get_default_registry
)
from memorygraph.models import Memory


# --- Dummy model for Phase 1 tests (Transaction doesn't exist yet) ---
# DUMMY DATA: This model stands in for Transaction until Phase 3 creates it.
# The registry doesn't care about model internals — it only stores the class
# reference. Any BaseModel subclass proves the registry works correctly.
class DummyCustomModel(BaseModel):
    """Lightweight stand-in for custom node types. Replace with real
    Transaction import after Phase 3 is implemented."""
    id: Optional[str] = None
    name: str = "test"


class TestNodeTypeRegistry:
    def test_register_and_retrieve(self):
        """Register a type, retrieve it by name."""
        registry = NodeTypeRegistry()
        # DUMMY DATA: Uses DummyCustomModel instead of Transaction (Phase 3)
        config = NodeTypeConfig(name="custom", label="Custom", model=DummyCustomModel, indexes=["id"])
        registry.register(config)
        assert registry.get_label("custom") == "Custom"
        assert registry.get_model("custom") == DummyCustomModel

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
        # DUMMY DATA: Uses DummyCustomModel instead of Transaction (Phase 3)
        config = NodeTypeConfig(name="tx", label="Tx", model=DummyCustomModel, indexes=[])
        registry.register(config)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(config)

    def test_all_types_default_registry(self):
        """get_default_registry() only has Memory (no circular dependency)."""
        registry = get_default_registry()
        assert len(registry.all_types()) == 1  # memory only
        assert registry.all_types()[0].name == "memory"

    def test_register_multiple_custom_types(self):
        """Registry supports multiple custom types alongside Memory.

        NOTE — DUMMY DATA: This test uses DummyCustomModel to prove the
        registry can hold multiple types. After Phase 3, add a separate
        integration test that calls register_custom_types() with the real
        Transaction model to verify the full wiring.
        """
        registry = get_default_registry()
        registry.register(NodeTypeConfig(
            name="custom", label="Custom", model=DummyCustomModel, indexes=["id"]
        ))
        assert len(registry.all_types()) == 2  # memory + custom
        names = [t.name for t in registry.all_types()]
        assert "memory" in names
        assert "custom" in names

    def test_has_type(self):
        """has_type() returns True/False."""
        registry = get_default_registry()
        assert registry.has_type("memory") is True
        assert registry.has_type("nonexistent") is False
```

## Acceptance Criteria

- [ ] Registry can register and retrieve type configs
- [ ] Default "memory" type is pre-registered via `get_default_registry()`
- [ ] `register_custom_types()` adds Transaction (and future custom types)
- [ ] Unknown type raises `KeyError` with helpful message listing registered types
- [ ] Duplicate registration raises `ValueError`
- [ ] `all_types()` returns all registered configs
- [ ] All 7 tests pass
