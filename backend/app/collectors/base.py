"""Stable, source-independent collector input and output types."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class CollectorError(Exception):
    """The source could not be read or its response was invalid."""


class AuthExpired(CollectorError):
    """The source requires the operator to reconnect the account."""


class IncompleteCollection(CollectorError):
    """A batch cannot be asserted to contain the complete requested catalogue."""


@dataclass(slots=True)
class SourceProduct:
    source_id: str
    name: str
    category_id: str
    category_name: str
    price: Decimal
    unit: str
    display_quantity: Decimal
    status: str
    stock: Decimal | None
    stock_unlimited: bool
    raw: dict[str, Any]
    quality: list[str] = field(default_factory=list)
