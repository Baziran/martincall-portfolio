from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence, Set
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from math import isfinite
from typing import Any


def _canonical_hash_value(value: Any, active_ids: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError("non-finite floats are not stable-hash values")
        return value
    if isinstance(value, Enum):
        return _canonical_hash_value(value.value, active_ids)
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        item = value.item()
        if item is value:
            raise TypeError("numpy scalar item() returned itself")
        return _canonical_hash_value(item, active_ids)

    identity = id(value)
    if identity in active_ids:
        raise TypeError(
            f"cyclic stable-hash value: {type(value).__module__}.{type(value).__qualname__}"
        )
    active_ids.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            type_name = f"{type(value).__module__}.{type(value).__qualname__}"
            return {
                "__type__": type_name,
                "__fields__": {
                    field.name: _canonical_hash_value(
                        getattr(value, field.name),
                        active_ids,
                    )
                    for field in fields(value)
                },
            }
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("stable-hash mapping keys must be strings")
                out[key] = _canonical_hash_value(item, active_ids)
            return out
        if isinstance(value, Set):
            items = [_canonical_hash_value(item, active_ids) for item in value]
            return {
                "__set__": sorted(
                    items,
                    key=lambda item: json.dumps(
                        item,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                )
            }
        if isinstance(value, Sequence):
            return [_canonical_hash_value(item, active_ids) for item in value]
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            type_name = f"{type(value).__module__}.{type(value).__qualname__}"
            return {
                "__type__": type_name,
                "__fields__": _canonical_hash_value(attributes, active_ids),
            }
        raise TypeError(
            f"{type(value).__module__}.{type(value).__qualname__} is not a stable-hash value"
        )
    finally:
        active_ids.remove(identity)


def canonical_hash_value(value: Any) -> Any:
    """Project supported runtime state to a deterministic JSON-native value."""
    return _canonical_hash_value(value, set())


def stable_hash(value: Any, *, length: int = 12) -> str:
    canonical = canonical_hash_value(value)
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    size = max(8, min(int(length), len(digest)))
    return digest[:size]
