# Phase 3: Transaction Model

**Status:** [ ] Not started
**File:** `src/memorygraph/models.py` (MODIFY)
**Gate:** `uv run pytest tests/test_transaction_model.py -v` — 6 tests

## What this does

Adds a `Transaction` Pydantic model to `models.py` alongside Memory. First concrete custom node type — proves the multi-label pattern works.

## Dependencies

- Phase 1 (NodeTypeRegistry — `register_custom_types()` references Transaction)

## Context from Phase 1

Database initialization wires the model to the registry:
```python
# In SQLiteMemoryDatabase.__init__ / MemoryDatabase.__init__:
from .type_registry import get_default_registry, register_custom_types
registry = get_default_registry()   # Phase 1: Memory only
register_custom_types(registry)     # Adds Transaction
```

## Code (added to models.py)

```python
class Transaction(BaseModel):
    """Domain-specific model for financial transactions."""
    id: Optional[str] = None
    amount: float
    merchant: str
    category: str  # groceries, dining, transport, etc.
    currency: str = "USD"
    payment_method: str = "unknown"  # google_pay, cash, credit, ath_movil
    date: datetime
    tags: List[str] = Field(default_factory=list)
    importance: float = Field(default=0.3, ge=0.0, le=1.0)
    note: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)  # Same as Memory — enables cross-type relationships with context
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator('date')
    @classmethod
    def ensure_timezone(cls, v: datetime) -> datetime:
        """Ensure date is timezone-aware. Defaults to UTC if naive.

        Prevents comparison issues with created_at/updated_at which are UTC.
        Naive datetimes from user input (e.g., "2026-03-03T00:00:00") get UTC.
        """
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, v: List[str]) -> List[str]:
        """Normalize tags to lowercase (same behavior as Memory)."""
        return [tag.lower().strip() for tag in v if tag.strip()]

    def to_storage_properties(self) -> Dict[str, Any]:
        """Convert to flat dict for storage (both Neo4j and SQLite).

        Context dict is flattened to context_* keys (same convention as Memory)
        so NodeFactory._normalize_properties() can regroup them on read.
        """
        props = {
            'id': self.id,
            'amount': self.amount,
            'merchant': self.merchant,
            'category': self.category,
            'currency': self.currency,
            'payment_method': self.payment_method,
            'date': self.date.isoformat(),
            'tags': self.tags,
            'importance': self.importance,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }
        if self.note:
            props['note'] = self.note
        # Flatten context dict to context_* keys (same convention as Memory)
        for ctx_key, ctx_value in self.context.items():
            props[f'context_{ctx_key}'] = ctx_value
        return props
```

## Tests: `tests/test_transaction_model.py`

```python
import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from memorygraph.models import Transaction

class TestTransactionModel:
    def test_valid_transaction(self):
        t = Transaction(amount=50.0, merchant="Pueblo", category="groceries", date=datetime.now(timezone.utc))
        assert t.amount == 50.0
        assert t.merchant == "Pueblo"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            Transaction(amount=50.0, category="groceries", date=datetime.now(timezone.utc))
            # Missing: merchant

    def test_default_values(self):
        t = Transaction(amount=10, merchant="Test", category="test", date=datetime.now(timezone.utc))
        assert t.currency == "USD"
        assert t.payment_method == "unknown"
        assert t.importance == 0.3

    def test_tags_lowercased(self):
        t = Transaction(amount=10, merchant="Test", category="test",
                        date=datetime.now(timezone.utc), tags=["FOOD", "Weekly"])
        assert t.tags == ["food", "weekly"]

    def test_to_storage_properties(self):
        t = Transaction(amount=42.50, merchant="Pueblo", category="groceries",
                        date=datetime(2026, 3, 3, tzinfo=timezone.utc))
        props = t.to_storage_properties()
        assert props["amount"] == 42.50
        assert props["merchant"] == "Pueblo"
        assert isinstance(props["date"], str)  # ISO string

    def test_naive_date_gets_utc(self):
        """Naive datetime (no timezone) is auto-converted to UTC."""
        naive_dt = datetime(2026, 3, 3, 12, 0, 0)  # no tzinfo
        t = Transaction(amount=10, merchant="Test", category="test", date=naive_dt)
        assert t.date.tzinfo is not None
        assert t.date.tzinfo == timezone.utc
```

## Acceptance Criteria

- [ ] Transaction model validates correctly (required fields, defaults)
- [ ] Missing required fields raise `ValidationError`
- [ ] Tags are auto-lowercased via `@field_validator`
- [ ] `to_storage_properties()` returns flat dict suitable for both backends
- [ ] `context` dict flattens to `context_*` keys in storage properties
- [ ] Naive datetimes (no timezone) are auto-converted to UTC
- [ ] All 6 tests pass
