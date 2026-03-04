"""Protocol definitions for backend type safety."""
from typing import Any, List, Optional, Protocol, Tuple

from pydantic import BaseModel
from .models import Memory, Relationship, SearchQuery


class MemoryOperations(Protocol):
    """
    Operations all backends support - the true common interface.

    This protocol defines the minimal set of operations that all memory backends
    must support, whether they use Cypher (graph databases) or REST (cloud API).

    Use this protocol for type hints when you need to work with any backend type.
    """

    async def store_memory(self, memory: Memory) -> str:
        """Store a memory and return its ID."""
        ...

    async def get_memory(
        self, memory_id: str, include_relationships: bool = True
    ) -> Optional[Memory]:
        """Retrieve a memory by ID."""
        ...

    async def update_memory(self, memory_id: str, **updates: Any) -> Memory:
        """Update an existing memory."""
        ...

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory."""
        ...

    async def search_memories(self, query: SearchQuery) -> List[Memory]:
        """Search for memories based on query parameters."""
        ...

    async def create_relationship(
        self, from_id: str, to_id: str, rel_type: str, **props: Any
    ) -> str:
        """Create a relationship between two memories."""
        ...

    async def get_related_memories(
        self, memory_id: str, **filters: Any
    ) -> List[Tuple[Memory, Relationship]]:
        """Get memories related to a specific memory."""
        ...

    async def store_node(self, type_name: str, node: BaseModel) -> str:
        """Store any registered node type and return its ID."""
        ...

    async def get_node(self, node_id: str) -> Optional[BaseModel]:
        """Retrieve any node by ID, returning the correct Pydantic model."""
        ...

    async def search_nodes(
        self, type_name: str, filters: dict
    ) -> List[BaseModel]:
        """Search nodes of a specific type with property filters."""
        ...
