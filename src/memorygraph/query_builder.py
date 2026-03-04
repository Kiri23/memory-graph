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

    def merge(self, type_name: str, alias: str = "m") -> str:
        """MERGE = create-or-update (upsert). Primary method for store operations.

        Always merges on `id` — consistent with match()/create()/merge() signatures.
        The MERGE key is hardcoded to `id` because all node types use UUID ids
        as their unique identifier.
        """
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        alias = _validate_identifier(alias, "alias")
        return f"MERGE ({alias}:{label} {{id: $id}})"

    def return_with_labels(self, alias: str = "m") -> str:
        """Generate RETURN clause that includes explicit labels.

        IMPORTANT: Both database.py and neo4j_backend.py call result.data()
        which converts Neo4j records to plain dicts. So record["labels"]
        will be a plain list[str], NOT a frozenset.
        """
        alias = _validate_identifier(alias, "alias")
        return f"RETURN {alias}, labels({alias}) AS labels"

    def relationship_match(
        self, from_type: str, to_type: str, rel_type: str,
        from_alias: str = "from", to_alias: str = "to", rel_alias: str = "r"
    ) -> str:
        """Generate MATCH for relationship queries with validated identifiers."""
        from_label = _validate_identifier(self.registry.get_label(from_type), "label")
        to_label = _validate_identifier(self.registry.get_label(to_type), "label")
        rel_type = _validate_identifier(rel_type, "relationship_type")
        from_alias = _validate_identifier(from_alias, "alias")
        to_alias = _validate_identifier(to_alias, "alias")
        rel_alias = _validate_identifier(rel_alias, "alias")
        return f"MATCH ({from_alias}:{from_label})-[{rel_alias}:{rel_type}]->({to_alias}:{to_label})"

    def search(self, type_name: str, filters: dict) -> tuple[str, dict]:
        """Build a parameterized search query from filters.

        Special keys: "query" (text search), "tags" (ANY match), "min_importance" (threshold).
        All other keys: exact property match.
        Returns: (cypher_query, parameters) tuple
        """
        label = _validate_identifier(self.registry.get_label(type_name), "label")
        conditions = []
        params = {}

        _SPECIAL_KEYS = {"query", "tags", "min_importance"}

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
            elif key in _SPECIAL_KEYS:
                continue
            else:
                safe_key = _validate_identifier(key, "filter_field")
                param_name = f"filter_{safe_key}"
                conditions.append(f"m.{safe_key} = ${param_name}")
                params[param_name] = value

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"MATCH (m:{label}){where} RETURN m, labels(m) AS labels"
        return query, params
