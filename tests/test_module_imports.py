from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "aef_terminal"


def _production_module_names() -> tuple[str, ...]:
    names = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT / "src").with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        names.append(".".join(parts))
    return tuple(sorted(set(names)))


def _import_in_fresh_process(module_name: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib,sys; importlib.import_module(sys.argv[1])",
                module_name,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return module_name, "import timed out after 15 seconds"
    if result.returncode == 0:
        return module_name, ""
    detail = (result.stderr or result.stdout).strip()
    return module_name, detail or f"exit status {result.returncode}"


def test_every_production_module_imports_in_a_fresh_process() -> None:
    modules = _production_module_names()
    with ThreadPoolExecutor(max_workers=8) as executor:
        failures = dict(
            (module_name, detail)
            for module_name, detail in executor.map(_import_in_fresh_process, modules)
            if detail
        )

    assert failures == {}
