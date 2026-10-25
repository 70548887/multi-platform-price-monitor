"""Source adapters used by the price collection workers."""

from .base import AuthExpired, CollectorError, IncompleteCollection, SourceProduct
from .xiaohong66 import DEFAULT_RULE, Xiaohong66Collector

__all__ = [
    "AuthExpired",
    "CollectorError",
    "IncompleteCollection",
    "SourceProduct",
    "Xiaohong66Collector",
    "DEFAULT_RULE",
]
