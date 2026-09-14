from pathlib import Path

from aef_terminal.ui.asset_services import (
    CSS_ASSET_FILES,
    HTML_TEMPLATE_FILE,
    JS_ASSET_FILES,
    QUOTE_STREAM_WORKER_FILE,
)


ROOT = Path(__file__).resolve().parents[1]
ASSET_MAP = ROOT / "src/aef_terminal/ui/assets/README.md"


def test_browser_asset_map_mentions_every_core_asset() -> None:
    documented = ASSET_MAP.read_text(encoding="utf-8")
    required = (
        *JS_ASSET_FILES,
        *CSS_ASSET_FILES,
        HTML_TEMPLATE_FILE,
        QUOTE_STREAM_WORKER_FILE,
    )

    missing = sorted(path.name for path in required if f"`{path.name}`" not in documented)

    assert missing == []
