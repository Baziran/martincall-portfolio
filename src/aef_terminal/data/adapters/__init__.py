from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from inspect import getmembers, isclass
from pkgutil import iter_modules

from aef_terminal.data.adapters.base import ProviderAdapterBase


@lru_cache(maxsize=1)
def discover_provider_adapters(
    package_name: str = "aef_terminal.data.adapters",
) -> tuple[ProviderAdapterBase, ...]:
    package = import_module(package_name)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return ()
    adapters: list[ProviderAdapterBase] = []
    seen: set[str] = set()
    for item in iter_modules(package_paths):
        if item.ispkg or item.name.startswith("_") or item.name in {"base"}:
            continue
        module = import_module(f"{package_name}.{item.name}")
        for _name, candidate in getmembers(module, isclass):
            if candidate.__module__ != module.__name__ or not issubclass(
                candidate, ProviderAdapterBase
            ):
                continue
            adapter = candidate()
            key = str(adapter.manifest.key or "").strip().lower()
            if not key:
                raise ValueError(f"Provider adapter {candidate.__name__} has no manifest key")
            if key in seen:
                raise ValueError(f"Duplicate provider adapter key: {key}")
            seen.add(key)
            adapters.append(adapter)
    return tuple(sorted(adapters, key=lambda adapter: adapter.manifest.key))


__all__ = ["ProviderAdapterBase", "discover_provider_adapters"]
