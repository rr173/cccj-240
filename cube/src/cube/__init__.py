"""Offline sparse cube: build handover material into a queryable cube."""

from .engine import (
    ALL_NOT_APPLICABLE,
    ALL_UNKNOWN,
    FULLY_EXACT,
    PARTIAL,
    Cube,
    build_cube,
    query_cube,
)
from .loader import load_handover
from .model import ModelError

__all__ = [
    "ALL_NOT_APPLICABLE",
    "ALL_UNKNOWN",
    "FULLY_EXACT",
    "PARTIAL",
    "Cube",
    "ModelError",
    "build_cube",
    "load_handover",
    "query_cube",
]
