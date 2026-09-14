from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from importlib import import_module
from typing import Any


@lru_cache(maxsize=None)
def resolve_ref(ref: str) -> Callable[..., Any]:
    """Resolve one manifest-owned function reference without loading the registry."""

    module_name, function_name = ref.split(":", 1)
    return getattr(import_module(module_name), function_name)
