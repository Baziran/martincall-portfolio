from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from aef_terminal import config as app_config
from scripts import codex_advisor_bridge as bridge


def _runner() -> bridge.CodexAdvisorRunner:
    runner = object.__new__(bridge.CodexAdvisorRunner)
    runner.model = "gpt-5.6-terra"
    runner.reasoning_effort = "high"
    runner.timeout_seconds = 38.0
    runner.codex_path = "/usr/local/bin/codex"
    runner._admission = threading.BoundedSemaphore(value=1)
    return runner


def test_codex_bridge_command_is_ephemeral_read_only_and_ignores_user_config(tmp_path) -> None:
    runner = _runner()
    output_path = tmp_path / "output.txt"
    schema_path = tmp_path / "schema.json"

    command = runner._command(model="gpt-5.6-sol", output_path=output_path, schema_path=schema_path)

    assert command[0:2] == ["/usr/local/bin/codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert command[command.index("--output-schema") + 1] == str(schema_path)
    assert command[-1] == "-"


def test_codex_bridge_child_environment_cannot_select_api_billing(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-pass")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")

    env = bridge.CodexAdvisorRunner._chatgpt_environment()

    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert env["CODEX_HOME"] == "/tmp/codex-home"


def test_codex_bridge_finds_cli_bundled_with_chatgpt_app(monkeypatch) -> None:
    monkeypatch.setattr(bridge.shutil, "which", lambda _: None)
    monkeypatch.setattr(bridge.Path, "is_file", lambda path: str(path).endswith("/codex"))
    monkeypatch.setattr(bridge.CodexAdvisorRunner, "_require_chatgpt_login", lambda self: None)

    runner = bridge.CodexAdvisorRunner(
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timeout_seconds=38.0,
    )

    assert runner.codex_path == "/Applications/ChatGPT.app/Contents/Resources/codex"


def test_codex_bridge_verdict_uses_canonical_schema_and_typed_response(monkeypatch) -> None:
    runner = _runner()
    captured = {}
    verdict = {
        "phase": "range",
        "bias": "neutral",
        "market_view": "wait",
        "headline": "Wait for confirmed acceptance",
        "pattern": "range acceptance",
        "confidence": 0.6,
        "timeframe": "5m",
        "evidence": ["confirmed bars remain in range"],
        "contradictions": [],
        "invalidation": ["confirmed close outside range"],
        "data_quality": "confirmed",
    }

    def fake_execute(prompt, *, model, schema=None):
        captured.update({"prompt": prompt, "model": model, "schema": schema})
        return json.dumps(verdict), "gpt-5.6-terra", 123.4

    monkeypatch.setattr(runner, "_execute", fake_execute)

    payload = runner.verdict({"model": "gpt-5.6-terra", "advisor_input": {"bars": []}})

    assert payload["ok"] is True
    assert payload["provider"] == "codex"
    assert payload["endpoint"] == "codex-exec"
    assert payload["verdict"] == verdict
    assert captured["schema"] is bridge.AI_THIRD_OPINION_SCHEMA
    assert "Do not inspect files" in captured["prompt"]


def test_codex_bridge_discussion_uses_typed_answer_and_object_schema(monkeypatch) -> None:
    runner = _runner()
    captured = {}
    discussion = {
        "answer": "Provisional price is testing the confirmed range high.",
        "object_suggestions": [
            {
                "kind": "price_line",
                "label": "Confirmed range high",
                "rationale": "Level is supported by confirmed bars.",
                "direction": "neutral",
                "top": None,
                "bottom": None,
                "price": 102.0,
                "extend_bars": 24,
                "confidence": 0.7,
            }
        ],
    }

    def fake_execute(prompt, *, model, schema=None):
        captured.update({"prompt": prompt, "model": model, "schema": schema})
        return json.dumps(discussion), "gpt-5.6-terra", 42.0

    monkeypatch.setattr(runner, "_execute", fake_execute)

    payload = runner.discussion(
        {
            "model": "gpt-5.6-terra",
            "advisor_input": {"bars": [], "live_bar": None},
            "question": "Draw the confirmed range high.",
            "history": [],
        }
    )

    assert payload["answer"] == discussion["answer"]
    assert payload["object_suggestions"] == discussion["object_suggestions"]
    assert captured["schema"] is bridge.AI_THIRD_OPINION_DISCUSSION_SCHEMA
    assert "Do not inspect files" in captured["prompt"]


def test_codex_bridge_rejects_unknown_model() -> None:
    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.CodexAdvisorRunner._model("gpt-5.6-luna")

    assert exc_info.value.status == "model_missing"


def test_env_or_secret_reads_docker_secret_file(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "bridge.token"
    token_file.write_text("token-from-file\n", encoding="utf-8")
    monkeypatch.delenv("CODEX_ADVISOR_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_ADVISOR_BRIDGE_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(app_config, "_SECRET_FILE_CACHE", None)

    assert app_config.env_or_secret("CODEX_ADVISOR_BRIDGE_TOKEN") == "token-from-file"


def test_codex_bridge_gate_defaults_closed_and_controls_request_admission() -> None:
    token = "a" * 32
    server = bridge.CodexAdvisorServer(
        ("127.0.0.1", 0),
        bridge.CodexAdvisorHandler,
        runner=_runner(),
        bridge_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    def request(path: str, payload: dict[str, object]) -> dict[str, object]:
        response = urlopen(
            Request(
                f"{base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            ),
            timeout=2.0,
        )
        return json.loads(response.read().decode("utf-8"))

    try:
        assert server.gate.is_set() is False
        with pytest.raises(HTTPError) as exc_info:
            request("/discussion", {"question": "blocked"})
        assert exc_info.value.code == 503
        assert json.loads(exc_info.value.read().decode("utf-8"))["status"] == "gate_closed"
        assert request("/gate/open", {"enabled": True})["gate"] == "open"
        assert server.gate.is_set() is True
        assert request("/gate/close", {"enabled": False})["gate"] == "closed"
        assert server.gate.is_set() is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
