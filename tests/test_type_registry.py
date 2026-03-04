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
        """Registry supports multiple custom types alongside Memory."""
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
