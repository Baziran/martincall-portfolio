"""Startup discovery, catalog diagnostics, and package-owned asset resolution.

Indicator contract and graph validation live in ``module_validation`` so discovery-only tasks do
not need to load the full validation implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules
from types import ModuleType
from typing import Any

from aef_terminal.indicators.module_contract import IndicatorModule
from aef_terminal.indicators.module_validation import _validate_discovered_modules


_DISCOVERY_SKIP = frozenset(
    {
        "__init__",
        "contracts",
        "control_specs",
        "defaults",
        "module_contract",
        "module_discovery",
        "module_validation",
        "registry",
        "scaffold",
        "scoring",
        "settings_schema",
    }
)
_MISSING_EXPORT = object()


@dataclass(frozen=True)
class IndicatorModuleCatalogEntry:
    module: str
    status: str
    indicator_ids: tuple[str, ...] = ()
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class _IndicatorDiscoveryScan:
    package: str
    modules: tuple[IndicatorModule, ...]
    installed: tuple[IndicatorModuleCatalogEntry, ...] = ()
    available: tuple[IndicatorModuleCatalogEntry, ...] = ()
    invalid: tuple[IndicatorModuleCatalogEntry, ...] = ()
    skipped: tuple[IndicatorModuleCatalogEntry, ...] = ()


def _modules_from_imported(module: ModuleType) -> tuple[IndicatorModule, ...]:
    single = getattr(module, "INDICATOR_MODULE", _MISSING_EXPORT)
    many = getattr(module, "INDICATOR_MODULES", _MISSING_EXPORT)
    out: list[IndicatorModule] = []
    if single is not _MISSING_EXPORT:
        if not isinstance(single, IndicatorModule):
            raise TypeError("INDICATOR_MODULE must be an IndicatorModule")
        out.append(single)
    if many is not _MISSING_EXPORT:
        if not isinstance(many, tuple):
            raise TypeError("INDICATOR_MODULES must be a tuple of IndicatorModule values")
        invalid_types = sorted(
            {
                f"{type(item).__module__}.{type(item).__qualname__}"
                for item in many
                if not isinstance(item, IndicatorModule)
            }
        )
        if invalid_types:
            raise TypeError(
                f"INDICATOR_MODULES contains non-IndicatorModule values: {', '.join(invalid_types)}"
            )
        out.extend(many)
    return tuple(out)


@lru_cache(maxsize=None)
def _scan_indicator_modules(
    package_name: str = "aef_terminal.indicators.modules",
) -> _IndicatorDiscoveryScan:
    invalid: list[IndicatorModuleCatalogEntry] = []
    available: list[IndicatorModuleCatalogEntry] = []
    skipped: list[IndicatorModuleCatalogEntry] = []
    try:
        package = import_module(package_name)
    except Exception as exc:  # pragma: no cover - exact exception belongs to the package.
        invalid.append(
            IndicatorModuleCatalogEntry(
                module=package_name,
                status="invalid",
                reason="import_error",
                detail=str(exc),
            )
        )
        return _IndicatorDiscoveryScan(
            package=package_name,
            modules=(),
            invalid=tuple(invalid),
        )

    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        invalid.append(
            IndicatorModuleCatalogEntry(
                module=package_name,
                status="invalid",
                reason="not_package",
            )
        )
        return _IndicatorDiscoveryScan(
            package=package_name,
            modules=(),
            invalid=tuple(invalid),
        )

    modules_by_package: dict[str, tuple[IndicatorModule, ...]] = {}
    for item in sorted(iter_modules(package_paths), key=lambda candidate: candidate.name):
        module_name = f"{package_name}.{item.name}"
        if item.name.startswith("_") or item.name in _DISCOVERY_SKIP:
            skipped.append(
                IndicatorModuleCatalogEntry(
                    module=module_name,
                    status="skipped",
                    reason="internal",
                )
            )
            continue
        try:
            imported = import_module(module_name)
        except Exception as exc:  # pragma: no cover - exact exception belongs to the package.
            invalid.append(
                IndicatorModuleCatalogEntry(
                    module=module_name,
                    status="invalid",
                    reason="import_error",
                    detail=str(exc),
                )
            )
            continue
        try:
            modules = _modules_from_imported(imported)
        except (TypeError, ValueError) as exc:
            invalid.append(
                IndicatorModuleCatalogEntry(
                    module=module_name,
                    status="invalid",
                    reason="invalid_contract",
                    detail=f"malformed_export:{type(exc).__name__}:{exc}",
                )
            )
            continue
        if not modules:
            available.append(
                IndicatorModuleCatalogEntry(
                    module=module_name,
                    status="available",
                    reason="no_indicator_module",
                )
            )
            continue
        modules_by_package[module_name] = modules

    quarantined = _validate_discovered_modules(modules_by_package)
    installed: list[IndicatorModuleCatalogEntry] = []
    discovered: list[IndicatorModule] = []
    for module_name, modules in modules_by_package.items():
        indicator_ids = tuple(str(module.id) for module in modules)
        quarantine = quarantined.get(module_name)
        if quarantine is not None:
            invalid.append(
                IndicatorModuleCatalogEntry(
                    module=module_name,
                    status="invalid",
                    indicator_ids=indicator_ids,
                    reason=quarantine.reason,
                    detail="; ".join(sorted(quarantine.details)),
                )
            )
            continue
        installed.append(
            IndicatorModuleCatalogEntry(
                module=module_name,
                status="installed",
                indicator_ids=indicator_ids,
            )
        )
        discovered.extend(modules)

    return _IndicatorDiscoveryScan(
        package=package_name,
        modules=tuple(
            sorted(
                discovered,
                key=lambda module: (module.spec.pipeline_order, module.id),
            )
        ),
        installed=tuple(
            sorted(
                installed,
                key=lambda entry: (entry.indicator_ids, entry.module),
            )
        ),
        available=tuple(sorted(available, key=lambda entry: entry.module)),
        invalid=tuple(sorted(invalid, key=lambda entry: entry.module)),
        skipped=tuple(sorted(skipped, key=lambda entry: entry.module)),
    )


def discover_indicator_modules(
    package_name: str = "aef_terminal.indicators.modules",
) -> tuple[IndicatorModule, ...]:
    return _scan_indicator_modules(package_name).modules


discover_indicator_modules.cache_clear = _scan_indicator_modules.cache_clear  # type: ignore[attr-defined]


def indicator_module_asset_paths(
    asset_type: str,
    package_name: str = "aef_terminal.indicators.modules",
) -> tuple[Path, ...]:
    field_name = {
        "js": "ui_js_assets",
        "css": "ui_css_assets",
    }.get(str(asset_type))
    if field_name is None:
        raise ValueError(f"Unknown indicator module asset type: {asset_type}")
    paths: list[Path] = []
    for indicator_module in discover_indicator_modules(package_name):
        assets = getattr(indicator_module, field_name)
        if not assets:
            continue
        module_name = indicator_module.spec.calculate_ref.split(":", 1)[0]
        imported = import_module(module_name)
        module_file = Path(str(getattr(imported, "__file__", ""))).resolve()
        if module_file.name != "__init__.py":
            raise ValueError(
                f"Indicator module {indicator_module.id} with UI assets must be a package"
            )
        root = module_file.parent
        for asset in assets:
            path = (root / asset).resolve()
            if root not in path.parents or not path.is_file():
                raise ValueError(
                    f"Indicator module asset is unavailable: {indicator_module.id}:{asset}"
                )
            paths.append(path)
    return tuple(paths)


def indicator_module_catalog(
    package_name: str = "aef_terminal.indicators.modules",
) -> dict[str, Any]:
    """Return diagnostics from the same startup scan used by the runtime registry."""
    scan = _scan_indicator_modules(package_name)
    return {
        "package": scan.package,
        "installed": [asdict(entry) for entry in scan.installed],
        "available": [asdict(entry) for entry in scan.available],
        "invalid": [asdict(entry) for entry in scan.invalid],
        "skipped": [asdict(entry) for entry in scan.skipped],
    }
