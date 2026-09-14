from __future__ import annotations

import json
import subprocess
from pathlib import Path


ASSETS = Path("src/aef_terminal/ui/assets")


def _asset(path: str) -> str:
    return (ASSETS / path).read_text(encoding="utf-8")


def test_open_settings_never_promotes_system_health_to_database_diagnostics(
    tmp_path: Path,
) -> None:
    reference_data = _asset("js/19-reference-data.js")
    fetch_source = (
        "const SYSTEM_HEALTH_MEMORY_TTL_MS"
        + reference_data.split("const SYSTEM_HEALTH_MEMORY_TTL_MS", 1)[1].split(
            "async function loadSystemHealth", 1
        )[0]
    )
    script = tmp_path / "system-health-request-contract.js"
    script.write_text(
        "\n".join(
            (
                "const urls = [];",
                "const state = { settings: { open: true }, systemHealth: null };",
                "function fetchJson(url) { urls.push(url); return Promise.resolve({ status: 'ok' }); }",
                fetch_source,
                "(async () => {",
                "  await fetchSystemHealth({ force: true });",
                "  await fetchSystemHealth({ force: true, details: true });",
                "  process.stdout.write(JSON.stringify(urls));",
                "})().catch(error => { console.error(error); process.exit(2); });",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "/api/system?details=false",
        "/api/system?details=true",
    ]
    assert "state.settings?.open" not in fetch_source
    assert "const details = options.details === true;" in fetch_source


def test_database_diagnostics_have_one_explicit_settings_action() -> None:
    bindings = _asset("js/70-settings-bindings.js")
    panel = _asset("js/30-panels-system-health.js")
    template = _asset("templates/index.html")

    settings_open_body = bindings.split('settingsToggle.addEventListener("click", () => {', 1)[
        1
    ].split('document.getElementById("system-diagnostics-request")', 1)[0]
    request_body = bindings.split('document.getElementById("system-diagnostics-request")', 1)[
        1
    ].split('document.getElementById("settings-close")', 1)[0]

    assert "loadSystemHealth(" not in settings_open_body
    assert "details: true" not in settings_open_body
    assert "await loadSystemHealth({ force: true, details: true });" in request_body
    assert bindings.count("details: true") == 1
    assert 'id="system-diagnostics-request"' in template
    assert 'id="system-diagnostics-status"' in template
    assert "const storageDiagnosticsCards = hasStorageDiagnostics" in panel
    assert "Detailed PostgreSQL statistics load only on request." in panel
