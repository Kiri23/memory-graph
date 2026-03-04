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

    def test_all_types_default_registry(self):
        """get_default_registry() only has Memory (no circular dependency)."""
        registry = get_default_registry()
        assert len(registry.all_types()) == 1  # memory only
        assert registry.all_types()[0].name == "memory"

    def test_all_types_with_custom_types(self):
        """register_custom_types() adds Transaction to registry."""
        from memorygraph.type_registry import register_custom_types
        registry = get_default_registry()
        register_custom_types(registry)
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

## Acceptance Criteria

- [ ] Registry can register and retrieve type configs
- [ ] Default "memory" type is pre-registered via `get_default_registry()`
- [ ] `register_custom_types()` adds Transaction (and future custom types)
- [ ] Unknown type raises `KeyError` with helpful message listing registered types
- [ ] Duplicate registration raises `ValueError`
- [ ] `all_types()` returns all registered configs
- [ ] All 7 tests pass
