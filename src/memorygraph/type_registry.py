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
