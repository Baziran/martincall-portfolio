#!/usr/bin/env python
from __future__ import annotations

import argparse
from pathlib import Path

from aef_terminal.indicators.scaffold import indicator_module_template, validate_indicator_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an AEF indicator module scaffold.")
    parser.add_argument("indicator_id", help="lower_snake_case indicator id")
    parser.add_argument("--label", default="", help="Display label; defaults to title-cased id")
    parser.add_argument("--order", type=int, default=500, help="Pipeline/manager/runtime order")
    parser.add_argument(
        "--output-dir",
        default="src/aef_terminal/indicators/modules",
        help="Indicator package directory",
    )
    parser.add_argument(
        "--package-name", default="aef_terminal.indicators.modules", help="Import package for refs"
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing package __init__.py"
    )
    args = parser.parse_args(argv)

    indicator_id = validate_indicator_id(args.indicator_id)
    output_dir = Path(args.output_dir)
    package_dir = output_dir / indicator_id
    path = package_dir / "__init__.py"
    if path.exists() and not args.force:
        raise SystemExit(f"{path} already exists; use --force to overwrite")
    package_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        indicator_module_template(
            indicator_id,
            label=args.label,
            package_name=args.package_name,
            pipeline_order=args.order,
        ),
        encoding="utf-8",
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
