from datetime import datetime
from memorygraph.node_factory import NodeFactory
from memorygraph.type_registry import get_default_registry, register_custom_types
from memorygraph.models import Memory, Transaction


class TestNodeFactory:
    def setup_method(self):
        self.registry = get_default_registry()
        register_custom_types(self.registry)
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
