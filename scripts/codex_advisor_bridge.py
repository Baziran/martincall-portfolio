#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from aef_terminal.config import env_or_secret
from aef_terminal.indicators.modules.ai_third_opinion.providers import (
    AI_THIRD_OPINION_DISCUSSION_SCHEMA,
    AI_THIRD_OPINION_SCHEMA,
    DEFAULT_CODEX_MODEL,
    _discussion_system_prompt,
    _verdict_system_prompt,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = PROJECT_ROOT / "secrets" / "codex_advisor_bridge.token"
TOKEN_ENV = "CODEX_ADVISOR_BRIDGE_TOKEN"
TOKEN_FILE_ENV = f"{TOKEN_ENV}_FILE"
MODEL_ENV = "CODEX_ADVISOR_MODEL"
REASONING_ENV = "CODEX_ADVISOR_REASONING_EFFORT"
TIMEOUT_ENV = "CODEX_ADVISOR_EXEC_TIMEOUT_SECONDS"
BIND_ENV = "CODEX_ADVISOR_BIND"
PORT_ENV = "CODEX_ADVISOR_PORT"
MAX_BODY_BYTES = 1_048_576
ALLOWED_MODELS = frozenset({"gpt-5.6-terra", "gpt-5.6-sol"})
ALLOWED_REASONING = frozenset({"low", "medium", "high", "xhigh"})


class BridgeError(RuntimeError):
    def __init__(
        self, status: str, message: str, *, http_status: HTTPStatus = HTTPStatus.BAD_GATEWAY
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class CodexAdvisorRunner:
    def __init__(self, *, model: str, reasoning_effort: str, timeout_seconds: float) -> None:
        self.model = self._model(model)
        self.reasoning_effort = self._reasoning(reasoning_effort)
        self.timeout_seconds = max(5.0, min(float(timeout_seconds), 40.0))
        bundled_codex = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        self.codex_path = shutil.which("codex") or (
            str(bundled_codex) if bundled_codex.is_file() else None
        )
        self._admission = threading.BoundedSemaphore(value=1)
        if not self.codex_path:
            raise BridgeError(
                "cli_missing",
                "Codex CLI is neither on PATH nor bundled with ChatGPT.app.",
            )
        self._require_chatgpt_login()

    @staticmethod
    def _model(value: Any) -> str:
        model = str(value or DEFAULT_CODEX_MODEL).strip()
        if model not in ALLOWED_MODELS:
            raise BridgeError(
                "model_missing",
                f"Unsupported Codex model: {model}",
                http_status=HTTPStatus.BAD_REQUEST,
            )
        return model

    @staticmethod
    def _reasoning(value: Any) -> str:
        reasoning = str(value or "high").strip().lower()
        if reasoning not in ALLOWED_REASONING:
            raise BridgeError(
                "invalid_request",
                f"Unsupported reasoning effort: {reasoning}",
                http_status=HTTPStatus.BAD_REQUEST,
            )
        return reasoning

    def _require_chatgpt_login(self) -> None:
        env = self._chatgpt_environment()
        result = subprocess.run(
            [self.codex_path, "login", "status"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0 or "Logged in using ChatGPT" not in output:
            raise BridgeError(
                "auth_error",
                "Codex CLI must be logged in with ChatGPT on this host.",
                http_status=HTTPStatus.UNAUTHORIZED,
            )

    @staticmethod
    def _chatgpt_environment() -> dict[str, str]:
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        return env

    def _command(
        self, *, model: str, output_path: Path, schema_path: Path | None = None
    ) -> list[str]:
        command = [
            self.codex_path,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--config",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--output-last-message",
            str(output_path),
            "--color",
            "never",
        ]
        if schema_path is not None:
            command.extend(("--output-schema", str(schema_path)))
        command.append("-")
        return command

    @staticmethod
    def _failure_status(stderr: str) -> str:
        text = stderr.lower()
        if "usage limit" in text or "rate limit" in text or "too many requests" in text:
            return "rate_limited"
        if "not logged in" in text or "unauthorized" in text or "authentication" in text:
            return "auth_error"
        if "model" in text and (
            "not found" in text or "not available" in text or "unsupported" in text
        ):
            return "model_missing"
        if "network" in text or "connection" in text or "dns" in text:
            return "network_error"
        return "execution_error"

    def _execute(
        self, prompt: str, *, model: Any, schema: dict[str, Any] | None = None
    ) -> tuple[str, str, float]:
        selected_model = self._model(model or self.model)
        if not self._admission.acquire(blocking=False):
            raise BridgeError(
                "busy",
                "Codex advisor is already processing a request.",
                http_status=HTTPStatus.TOO_MANY_REQUESTS,
            )
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="martincall-codex-advisor-") as temp_dir:
                temp_root = Path(temp_dir)
                output_path = temp_root / "last-message.txt"
                schema_path: Path | None = None
                if schema is not None:
                    schema_path = temp_root / "output-schema.json"
                    schema_path.write_text(
                        json.dumps(schema, separators=(",", ":")), encoding="utf-8"
                    )
                process = subprocess.Popen(
                    self._command(
                        model=selected_model, output_path=output_path, schema_path=schema_path
                    ),
                    cwd=temp_root,
                    env=self._chatgpt_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    _, stderr = process.communicate(prompt, timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise BridgeError(
                        "timeout", "Codex advisor exceeded its bounded execution timeout."
                    ) from exc
                if process.returncode != 0:
                    detail = str(stderr or "").strip()[-1000:]
                    raise BridgeError(
                        self._failure_status(detail), detail or "Codex CLI request failed."
                    )
                try:
                    output = output_path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise BridgeError(
                        "invalid_output", "Codex CLI did not produce a final response."
                    ) from exc
                if not output:
                    raise BridgeError(
                        "invalid_output", "Codex CLI returned an empty final response."
                    )
        finally:
            self._admission.release()
        return output, selected_model, round((time.monotonic() - started) * 1000.0, 1)

    def verdict(self, payload: dict[str, Any]) -> dict[str, Any]:
        advisor_input = payload.get("advisor_input")
        if not isinstance(advisor_input, dict):
            raise BridgeError(
                "invalid_request",
                "advisor_input must be an object.",
                http_status=HTTPStatus.BAD_REQUEST,
            )
        prompt = (
            "Do not inspect files, run shell commands, browse, or call tools. "
            "Use only the supplied market-context JSON. Your final response must match the output schema.\n\n"
            f"{_verdict_system_prompt()}\n\n"
            f"Market context JSON:\n{json.dumps(advisor_input, separators=(',', ':'), default=str)}"
        )
        output, model, latency_ms = self._execute(
            prompt, model=payload.get("model"), schema=AI_THIRD_OPINION_SCHEMA
        )
        try:
            verdict = json.loads(output)
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid_output", "Codex verdict was not valid JSON.") from exc
        if not isinstance(verdict, dict):
            raise BridgeError("invalid_output", "Codex verdict must be a JSON object.")
        return {
            "ok": True,
            "status": "ready",
            "provider": "codex",
            "model": model,
            "endpoint": "codex-exec",
            "response_id": f"codex_{uuid.uuid4().hex}",
            "latency_ms": latency_ms,
            "usage": {},
            "verdict": verdict,
        }

    def discussion(self, payload: dict[str, Any]) -> dict[str, Any]:
        advisor_input = payload.get("advisor_input")
        question = str(payload.get("question") or "").strip()
        history = payload.get("history")
        if not isinstance(advisor_input, dict) or not question:
            raise BridgeError(
                "invalid_request",
                "advisor_input must be an object and question must be non-empty.",
                http_status=HTTPStatus.BAD_REQUEST,
            )
        if not isinstance(history, list):
            history = []
        prompt_payload = {
            "question": question[:2000],
            "conversation_history": history[-8:],
            "market_context": advisor_input,
        }
        prompt = (
            "Do not inspect files, run shell commands, browse, or call tools. "
            "Use only the supplied JSON. Your final response must match the output schema.\n\n"
            f"{_discussion_system_prompt()}\n\n"
            f"Discussion JSON:\n{json.dumps(prompt_payload, separators=(',', ':'), default=str)}"
        )
        output, model, latency_ms = self._execute(
            prompt,
            model=payload.get("model"),
            schema=AI_THIRD_OPINION_DISCUSSION_SCHEMA,
        )
        try:
            discussion = json.loads(output)
        except json.JSONDecodeError as exc:
            raise BridgeError(
                "invalid_output",
                "Codex discussion was not valid JSON.",
            ) from exc
        if (
            not isinstance(discussion, dict)
            or not str(discussion.get("answer") or "").strip()
            or not isinstance(discussion.get("object_suggestions"), list)
        ):
            raise BridgeError(
                "invalid_output",
                "Codex discussion did not match the typed response contract.",
            )
        return {
            "ok": True,
            "status": "ready",
            "provider": "codex",
            "model": model,
            "endpoint": "codex-exec",
            "response_id": f"codex_{uuid.uuid4().hex}",
            "latency_ms": latency_ms,
            "usage": {},
            "answer": str(discussion["answer"]).strip(),
            "object_suggestions": discussion["object_suggestions"][:3],
        }


class CodexAdvisorHandler(BaseHTTPRequestHandler):
    server: CodexAdvisorServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[codex-advisor] {self.address_string()} {format % args}", flush=True)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.bridge_token}"
        supplied = self.headers.get("Authorization") or ""
        return hmac.compare_digest(supplied, expected)

    def _payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise BridgeError(
                "invalid_request", "Invalid Content-Length.", http_status=HTTPStatus.BAD_REQUEST
            ) from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise BridgeError(
                "invalid_request",
                "Request body size is invalid.",
                http_status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(
                "invalid_request",
                "Request body must be valid JSON.",
                http_status=HTTPStatus.BAD_REQUEST,
            ) from exc
        if not isinstance(payload, dict):
            raise BridgeError(
                "invalid_request",
                "Request body must be a JSON object.",
                http_status=HTTPStatus.BAD_REQUEST,
            )
        return payload

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "status": "auth_error", "message": "Invalid bridge token."},
            )
            return
        if self.path != "/health":
            self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "status": "not_found", "message": "Unknown endpoint."},
            )
            return
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "status": "ready",
                "provider": "codex",
                "model": self.server.runner.model,
                "reasoning_effort": self.server.runner.reasoning_effort,
                "auth": "chatgpt",
                "gate": "open" if self.server.gate.is_set() else "closed",
            },
        )

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "status": "auth_error", "message": "Invalid bridge token."},
            )
            return
        try:
            payload = self._payload()
            if self.path == "/gate/open":
                self.server.gate.set()
                response = {
                    "ok": True,
                    "status": "ready",
                    "provider": "codex",
                    "gate": "open",
                }
            elif self.path == "/gate/close":
                self.server.gate.clear()
                response = {
                    "ok": True,
                    "status": "ready",
                    "provider": "codex",
                    "gate": "closed",
                }
            elif self.path in {"/verdict", "/discussion"}:
                if not self.server.gate.is_set():
                    raise BridgeError(
                        "gate_closed",
                        "Codex advisor gate is closed because AI Third Opinion calculation is off.",
                        http_status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                response = (
                    self.server.runner.verdict(payload)
                    if self.path == "/verdict"
                    else self.server.runner.discussion(payload)
                )
            else:
                raise BridgeError(
                    "not_found", "Unknown endpoint.", http_status=HTTPStatus.NOT_FOUND
                )
        except BridgeError as exc:
            self._json(exc.http_status, {"ok": False, "status": exc.status, "message": str(exc)})
            return
        except Exception as exc:
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "status": "bridge_error",
                    "message": f"{exc.__class__.__name__}: {exc}",
                },
            )
            return
        self._json(HTTPStatus.OK, response)


class CodexAdvisorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        runner: CodexAdvisorRunner,
        bridge_token: str,
    ) -> None:
        super().__init__(server_address, handler)
        self.runner = runner
        self.bridge_token = bridge_token
        self.gate = threading.Event()


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return default
    return parsed if parsed > 0 else default


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded host bridge from MartinCall to a ChatGPT-authenticated Codex CLI."
    )
    parser.add_argument("--bind", default=os.getenv(BIND_ENV, "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv(PORT_ENV, "8765")))
    parser.add_argument("--model", default=env_or_secret(MODEL_ENV, DEFAULT_CODEX_MODEL))
    parser.add_argument("--reasoning-effort", default=env_or_secret(REASONING_ENV, "high"))
    parser.add_argument(
        "--timeout", type=float, default=_positive_float(env_or_secret(TIMEOUT_ENV), 38.0)
    )
    args = parser.parse_args()

    os.environ.setdefault(TOKEN_FILE_ENV, str(DEFAULT_TOKEN_FILE))
    bridge_token = str(env_or_secret(TOKEN_ENV) or "").strip()
    if len(bridge_token) < 32:
        parser.error(f"{TOKEN_ENV} must contain at least 32 characters.")
    runner = CodexAdvisorRunner(
        model=str(args.model),
        reasoning_effort=str(args.reasoning_effort),
        timeout_seconds=float(args.timeout),
    )
    server = CodexAdvisorServer(
        (str(args.bind), int(args.port)),
        CodexAdvisorHandler,
        runner=runner,
        bridge_token=bridge_token,
    )
    print(
        f"Codex advisor bridge ready on http://{args.bind}:{args.port} "
        f"(model={runner.model}, reasoning={runner.reasoning_effort}, auth=ChatGPT, "
        "concurrency=1, gate=closed)",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
