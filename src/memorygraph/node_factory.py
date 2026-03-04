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
          Neo4j:  factory.from_neo4j_record(record)  # preferred
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
