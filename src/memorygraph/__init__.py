"""
Claude Code Memory Server

A graph-based MCP server that provides intelligent memory capabilities for Claude Code,
enabling persistent knowledge tracking, relationship mapping, and contextual development assistance.

Supports multiple backends: SQLite (default), Neo4j, and Memgraph.
"""

__version__ = "0.12.4"
__author__ = "Gregory Dickson"
__email__ = "gregory.d.dickson@gmail.com"

from .server import ClaudeMemoryServer
from .models import (
    Memory,
    MemoryType,
    Transaction,
    Relationship,
    RelationshipType,
    MemoryNode,
    MemoryContext,
    MemoryError,
    MemoryNotFoundError,
    RelationshipError,
    ValidationError,
    DatabaseConnectionError,
    SchemaError,
    NotFoundError,
    BackendError,
    ConfigurationError,
)
from .type_registry import NodeTypeRegistry, NodeTypeConfig, get_default_registry, register_custom_types
from .query_builder import QueryBuilder
from .node_factory import NodeFactory

__all__ = [
    "ClaudeMemoryServer",
    "Memory",
    "MemoryType",
    "Transaction",
    "NodeTypeRegistry",
    "NodeTypeConfig",
    "get_default_registry",
    "register_custom_types",
    "QueryBuilder",
    "NodeFactory",
    "Relationship",
    "RelationshipType",
    "MemoryNode",
    "MemoryContext",
    "MemoryError",
    "MemoryNotFoundError",
    "RelationshipError",
    "ValidationError",
    "DatabaseConnectionError",
    "SchemaError",
    "NotFoundError",
    "BackendError",
    "ConfigurationError",
]