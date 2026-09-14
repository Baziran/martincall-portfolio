from __future__ import annotations

import importlib
from pathlib import Path


def test_ui_service_modules_import_cleanly() -> None:
    services_dir = Path("src/aef_terminal/ui/services")
    module_names = sorted(
        path.stem for path in services_dir.glob("*.py") if path.name != "__init__.py"
    )
    assert module_names, "expected ui.services modules"
    for module_name in module_names:
        importlib.import_module(f"aef_terminal.ui.services.{module_name}")
