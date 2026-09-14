from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from aef_terminal import __version__
from aef_terminal import build_info as build_info_module
from aef_terminal.ui import app as ui_app
from aef_terminal.ui import app_runtime_wiring
from aef_terminal.ui import system_status_runtime
from scripts import write_build_info


def test_private_release_version_sources_are_consistent(tmp_path, monkeypatch) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    readme = Path("README.md").read_text(encoding="utf-8")
    server_env = Path("deploy/hetzner/server.env.example").read_text(encoding="utf-8")
    output_path = tmp_path / "_build_info.json"
    monkeypatch.setattr(write_build_info, "OUT", output_path)
    monkeypatch.setenv("BUILD_GIT_SHA", "abc1234")
    monkeypatch.delenv("BUILD_VERSION", raising=False)

    assert project["project"]["version"] == __version__ == "1.5.0"
    assert f"Current release baseline: **MartinCall {__version__} private**" in readme
    assert f"BUILD_VERSION={__version__}" in server_env
    assert write_build_info.main() == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["version"] == __version__


def test_build_info_reads_embedded_file(tmp_path, monkeypatch) -> None:
    info_path = tmp_path / "_build_info.json"
    info_path.write_text(
        json.dumps(
            {
                "version": "2026.06.17-abc1234",
                "git_sha": "abc1234",
                "built_at": "2026-06-17T08:30:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_info_module, "_BUILD_INFO_PATH", info_path)
    build_info_module.build_info.cache_clear()
    assert build_info_module.build_info() == {
        "version": "2026.06.17-abc1234",
        "git_sha": "abc1234",
        "built_at": "2026-06-17T08:30:00+00:00",
    }


def test_app_runtime_status_includes_build_info(monkeypatch) -> None:
    original_service = system_status_runtime._SYSTEM_STATUS_SERVICE
    monkeypatch.setattr(
        system_status_runtime,
        "_SYSTEM_STATUS_SERVICE",
        original_service,
    )
    app_runtime_wiring.configure_system_status(
        started_at=ui_app.STARTED_AT,
        build={
            "version": "test-build",
            "git_sha": "deadbeef",
            "built_at": "2026-06-17T00:00:00+00:00",
        },
        store_factory=lambda: None,
    )
    status = ui_app.app_runtime_status()
    assert status["version"] == "test-build"
    assert status["git_sha"] == "deadbeef"
    assert status["built_at"] == "2026-06-17T00:00:00+00:00"
    assert "build_info()" not in Path("src/aef_terminal/ui/system_status.py").read_text(
        encoding="utf-8"
    )
    assert "build_info()" not in Path("src/aef_terminal/ui/app.py").read_text(encoding="utf-8")


def test_container_build_requires_source_revision() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    launcher = Path("martincall-server.sh").read_text(encoding="utf-8")

    assert "ARG BUILD_GIT_SHA=unknown" not in dockerfile
    assert 'test "${BUILD_GIT_SHA}" != "unknown"' in dockerfile
    assert "BUILD_GIT_SHA:-unknown" not in compose
    assert "BUILD_GIT_SHA: ${BUILD_GIT_SHA:-}" in compose
    assert 'export BUILD_GIT_SHA="$(git rev-parse --short HEAD)"' in launcher
    assert 'export BUILD_GIT_SHA="${BUILD_GIT_SHA}-dirty"' in launcher
    assert '"${COMPOSE[@]}" config >/dev/null' in launcher
    assert 'docker exec "$db_container" printenv POSTGRES_PASSWORD' not in launcher
    assert "DEBUG_POSTGRES_PASSWORD" not in launcher
    assert "temporary local-debug PostgreSQL password" not in launcher


def test_discord_companion_waits_for_operational_readiness() -> None:
    launcher = Path("martincall-server.sh").read_text(encoding="utf-8")
    discord_launcher = launcher.split("start_discord_companion()", 1)[1].split(
        "stop_discord_companion()",
        1,
    )[0]

    assert "for attempt in {1..80}" in discord_launcher
    assert 'grep -E "Discord Signals (live|dormant)"' in discord_launcher
    assert "Connected to Discord Desktop IPC" not in discord_launcher


def test_remote_launcher_owns_tunnel_and_discord_companion_lifecycle() -> None:
    launcher = Path("martincall-server.sh").read_text(encoding="utf-8")
    remote_launcher = launcher.split("start_remote_tunnel()", 1)[1].split(
        'case "${1:-}" in',
        1,
    )[0]
    remote_start = remote_launcher.split("start_remote_access()", 1)[1].split(
        "stop_remote_access()",
        1,
    )[0]

    assert 'REMOTE_SSH_HOST="${MARTINCALL_REMOTE_SSH_HOST:-bazserv-remote}"' in launcher
    assert 'REMOTE_LOCAL_PORT="${MARTINCALL_REMOTE_LOCAL_PORT:-18000}"' in launcher
    assert "-o ExitOnForwardFailure=yes" in remote_launcher
    assert "-o ServerAliveInterval=30" in remote_launcher
    assert '-L "$REMOTE_FORWARD_SPEC"' in remote_launcher
    assert 'curl -fsS --max-time 2 "$REMOTE_APP_URL/api/ready"' in remote_launcher
    assert remote_start.index("start_remote_tunnel") < remote_start.index("start_discord_companion")
    assert "Keep this terminal open" in remote_start
    assert "while remote_tunnel_pid" in remote_start
    assert "discord_companion_pid" in remote_start
    assert "remote-start)" in launcher
    assert "remote-stop)" in launcher
    assert "remote-status)" in launcher


def test_research_capture_deployment_uses_stable_data_mount_and_operator_commands() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    launcher = Path("martincall-server.sh").read_text(encoding="utf-8")
    server_env = Path("deploy/hetzner/server.env.example").read_text(encoding="utf-8")

    assert "${MARTINCALL_DATA_ROOT:-../data}:/data" in compose
    assert "AEF_RESEARCH_CAPTURE_ENABLED" in compose
    assert "research-status)" in launcher
    assert "research-export)" in launcher
    assert "python scripts/export_research_capture.py" in launcher
    assert "MARTINCALL_DATA_ROOT=/opt/martincall/shared/data" in server_env
    assert 'AEF_RESEARCH_CAPTURE_INSTRUMENT_IDS=["ibkr|future_root|ES|CME|USD|ES"]' in server_env


def test_remote_gateway_login_control_is_private_and_single_attempt() -> None:
    compose = Path("deploy/hetzner/docker-compose.yml").read_text(encoding="utf-8")
    supervisor = Path("deploy/hetzner/ib-gateway-control/supervisor.sh").read_text(encoding="utf-8")

    assert "AEF_IBKR_GATEWAY_LOGIN_CONTROL_HOST: ib-gateway" in compose
    assert 'AEF_IBKR_GATEWAY_LOGIN_CONTROL_PORT: "7463"' in compose
    assert "./deploy/hetzner/ib-gateway-control:" in compose
    assert "TWOFA_TIMEOUT_ACTION: exit" in compose
    assert 'RELOGIN_AFTER_TWOFA_TIMEOUT: "no"' in compose
    assert "7463:7463" not in compose
    assert "waiting for an explicit login request" in supervisor
    assert "-name autorestart -print" in supervisor


def test_ui_gate_references_only_existing_test_files() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    declared = {
        token.rstrip("\\")
        for token in makefile.split()
        if token.startswith("tests/") and token.rstrip("\\").endswith(".py")
    }

    assert "tests/test_history_repair.py" in declared
    assert declared
    assert not [path for path in sorted(declared) if not Path(path).is_file()]


def test_smoke_api_http_checks_have_bounded_deadlines() -> None:
    smoke = Path("scripts/smoke_api.sh").read_text(encoding="utf-8")

    assert "curl_bounded()" in smoke
    assert "--connect-timeout 3" in smoke
    assert '--max-time "$max_time"' in smoke
    assert 'curl_bounded 5 "${BASE_URL}/api/health"' in smoke
    assert 'curl_bounded 5 "${BASE_URL}/api/ready"' in smoke
    assert 'curl_bounded 10 "${BASE_URL}/api/instruments"' in smoke
    assert 'curl_bounded 30 "${BASE_URL}/api/market?${MARKET_QUERY}"' in smoke
    assert "curl -fsS" not in smoke


def test_container_runtime_installs_timezone_database_for_ibkr_timestamps() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert '"tzdata>=' in pyproject


def test_container_runtime_uses_version_locked_production_dependencies() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    workflow_sources = [
        path.read_text(encoding="utf-8")
        for path in (
            Path(".github/workflows/test.yml"),
            Path(".github/workflows/ui-gates.yml"),
            Path(".github/workflows/smoke-api.yml"),
        )
    ]
    lock_lines = {
        line
        for line in Path("requirements.lock").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    tinvest_sdk = (
        "t-tech-investments @ "
        "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/files/"
        "018cd34007d9308d755ca583e502224a8009211d52f903bcbfa48853d47fa9de/"
        "t_tech_investments-1.49.3-py3-none-any.whl"
        "#sha256=018cd34007d9308d755ca583e502224a8009211d52f903bcbfa48853d47fa9de"
    )

    assert dockerfile.startswith("FROM python:3.14.6-slim-bookworm")
    assert "COPY pyproject.toml requirements.lock ./" in dockerfile
    assert "pip install --requirement requirements.lock" in dockerfile
    assert "pip install --upgrade pip" not in dockerfile
    assert "pip install -e . --no-deps --no-build-isolation" in dockerfile
    assert all(
        "pip install --requirement requirements.lock" in source for source in workflow_sources
    )
    assert all("==" in line or line == tinvest_sdk for line in lock_lines)
    assert tinvest_sdk in project["project"]["dependencies"]
    assert tinvest_sdk in lock_lines
    locked_names = {
        re.split(r"\s*(?:==|@)", line, maxsplit=1)[0].lower().replace("_", "-")
        for line in lock_lines
    }
    required_names = {
        re.split(r"[\[<>=!~;@]", value, maxsplit=1)[0].strip().lower().replace("_", "-")
        for value in project["project"]["dependencies"]
    }
    assert required_names <= locked_names
    assert {
        "fastapi==0.140.0",
        "ib_async==2.1.0",
        "psycopg==3.3.4",
        "tzdata==2025.3",
        "uvicorn==0.51.0",
    } <= lock_lines
    assert {
        "grpcio==1.83.0",
        "protobuf==6.33.6",
        "t-tech-investments",
    } <= (lock_lines | locked_names)


def test_production_compose_fails_closed_and_uses_current_tick_contract() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    makefile = Path("Makefile").read_text(encoding="utf-8")
    tick_launcher = Path("scripts/ibkr_tick_feed.py").read_text(encoding="utf-8")

    assert "timescale/timescaledb:latest-pg16@sha256:" in compose
    assert "shared_preload_libraries=timescaledb,pg_stat_statements" in compose
    assert "track_io_timing=on" in compose
    assert "pg_stat_statements.track=all" in compose
    assert "pg_stat_statements.max=10000" in compose
    assert "MARTINCALL_POSTGRES_PASSWORD:?" in compose
    password_references = re.findall(r"\$\{MARTINCALL_POSTGRES_PASSWORD([^}]*)\}", compose)
    assert password_references
    assert all(reference.startswith(":?") for reference in password_references)
    assert '"${MARTINCALL_BIND_ADDRESS:-127.0.0.1}:8000:8000"' in compose
    assert "http://127.0.0.1:8000/ready" in compose
    assert '"curl"' in compose
    assert '"python",\n          "-c"' not in compose
    assert "interval: 60s" in compose
    assert "retries: 3" in compose
    assert "apt-get install --yes --no-install-recommends curl" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "MARTINCALL_TICK_INSTRUMENT_ID" in compose
    assert 'martincall-app:\n    profiles: ["runtime"]' in compose
    assert 'profiles: ["ticks"]' in compose
    assert 'restart: "no"' in compose.split("martincall-tick-feed:", 1)[1]
    assert "MARTINCALL_TICK_SYMBOLS" not in compose
    assert "--symbols" not in compose
    assert "--mode" not in compose
    assert "AEF_ALPACA" not in compose
    assert "AEF_GEX_SAVE_CSV" not in compose
    assert "AEF_IBKR_REPAIR_CLIENT_ID" not in compose
    assert "AEF_IBKR_ALERT_CLIENT_ID" not in compose
    assert "exact provider-qualified --instrument-id is required" in tick_launcher
    assert "MARTINCALL_BIND_ADDRESS ?= 127.0.0.1" in makefile
    assert "./martincall-server.sh start" in makefile
    assert "./martincall-server.sh tick-feed" in makefile
    server_script = Path("martincall-server.sh").read_text(encoding="utf-8")
    assert 'ps --status running --services "$APP_SERVICE"' in server_script
    assert 'ps --status running --services "$TICK_SERVICE"' in server_script
    assert "--host $(MARTINCALL_BIND_ADDRESS)" in makefile


def test_hetzner_pilot_owns_fresh_storage_and_only_gateway_secrets() -> None:
    compose = Path("deploy/hetzner/docker-compose.yml").read_text(encoding="utf-8")
    env_example = Path("deploy/hetzner/server.env.example").read_text(encoding="utf-8")
    runbook = Path("deploy/hetzner/README.md").read_text(encoding="utf-8")

    assert "martincall-timescale-data:\n    external: false" in compose
    assert "AEF_SECRET_FILE: !reset null" in compose
    assert "CODEX_ADVISOR_BRIDGE_TOKEN_FILE: !reset null" in compose
    assert "secrets: !reset []" in compose
    assert "martincall_runtime_secrets: !reset null" in compose
    assert "codex_advisor_bridge_token: !reset null" in compose
    assert "ghcr.io/gnzsnz/ib-gateway:10.45.1j@sha256:" in compose
    assert "TRADING_MODE: live" in compose
    assert 'READ_ONLY_API: "yes"' in compose
    assert "AEF_IBKR_PORT: ${AEF_IBKR_PORT:-4003}" in compose
    assert "AEF_IBKR_GEX_PORT: ${AEF_IBKR_GEX_PORT:-4003}" in compose
    assert "AEF_IBKR_PORT=4003" in env_example
    assert "AEF_IBKR_GEX_PORT=4003" in env_example
    assert f"BUILD_VERSION={__version__}" in env_example
    assert "Compose creates the named `martincall-timescale-data` volume" in runbook
    assert "required pilot secrets" in runbook
