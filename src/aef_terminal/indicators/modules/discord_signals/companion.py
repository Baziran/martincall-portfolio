from __future__ import annotations

import json
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aef_terminal.config import env_or_secret

from .contracts import require_discord_snowflake, resolve_discord_signal_channel_ids
from .rpc_bridge import (
    DISCORD_RPC_BRIDGE_CONTRACT,
    DISCORD_SIGNAL_JOURNAL_OUTCOMES,
)


_LOGGER = logging.getLogger("discord-signals-companion")
_RPC_VERSION = 1
_HANDSHAKE = 0
_FRAME = 1
_CLOSE = 2
_PING = 3
_PONG = 4
_MAX_RPC_FRAME_BYTES = 8 * 1024 * 1024
_TERMINAL_HEADER = "X-MartinCall-Discord-RPC"
_OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"


class DiscordRpcCompanionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DiscordRpcCompanionConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    channel_ids: tuple[str, ...]
    author_id: str
    bridge_secret: str
    terminal_base_url: str
    request_timeout_seconds: float
    history_limit: int
    snapshot_seconds: float
    gate_poll_seconds: float

    @classmethod
    def from_environment(cls) -> DiscordRpcCompanionConfig:
        client_id = require_discord_snowflake(
            env_or_secret("AEF_DISCORD_RPC_CLIENT_ID"),
            field="discord_rpc_client_id",
        )
        channel_ids = resolve_discord_signal_channel_ids(
            env_or_secret("AEF_DISCORD_SIGNALS_CHANNEL_IDS"),
            field="discord_channel_ids",
        )
        author_id = require_discord_snowflake(
            env_or_secret("AEF_DISCORD_SIGNALS_AUTHOR_ID"),
            field="discord_author_id",
        )
        client_secret = str(env_or_secret("AEF_DISCORD_RPC_CLIENT_SECRET") or "").strip()
        redirect_uri = str(env_or_secret("AEF_DISCORD_RPC_REDIRECT_URI") or "").strip()
        bridge_secret = str(env_or_secret("AEF_DISCORD_SIGNALS_BRIDGE_SECRET") or "").strip()
        terminal_base_url = str(
            env_or_secret(
                "AEF_DISCORD_SIGNALS_TERMINAL_URL",
                "http://127.0.0.1:8000",
            )
            or ""
        ).strip()
        if not client_secret:
            raise ValueError("AEF_DISCORD_RPC_CLIENT_SECRET is required")
        if not redirect_uri:
            raise ValueError("AEF_DISCORD_RPC_REDIRECT_URI is required")
        if len(bridge_secret) < 32:
            raise ValueError(
                "AEF_DISCORD_SIGNALS_BRIDGE_SECRET must contain at least 32 characters"
            )
        parsed_terminal = urlparse(terminal_base_url)
        if (
            parsed_terminal.scheme != "http"
            or parsed_terminal.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_terminal.username
            or parsed_terminal.password
            or parsed_terminal.query
            or parsed_terminal.fragment
        ):
            raise ValueError("AEF_DISCORD_SIGNALS_TERMINAL_URL must be a loopback HTTP URL")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            channel_ids=channel_ids,
            author_id=author_id,
            bridge_secret=bridge_secret,
            terminal_base_url=terminal_base_url.rstrip("/"),
            request_timeout_seconds=max(
                float(
                    env_or_secret(
                        "AEF_DISCORD_SIGNALS_REQUEST_TIMEOUT_SECONDS",
                        "8",
                    )
                    or "8"
                ),
                1.0,
            ),
            history_limit=min(
                max(
                    int(
                        env_or_secret(
                            "AEF_DISCORD_SIGNALS_HISTORY_LIMIT",
                            "500",
                        )
                        or "500"
                    ),
                    1,
                ),
                1_000,
            ),
            snapshot_seconds=min(
                max(
                    float(
                        env_or_secret(
                            "AEF_DISCORD_SIGNALS_SNAPSHOT_SECONDS",
                            "30",
                        )
                        or "30"
                    ),
                    5.0,
                ),
                3_600.0,
            ),
            gate_poll_seconds=min(
                max(
                    float(
                        env_or_secret(
                            "AEF_DISCORD_SIGNALS_GATE_POLL_SECONDS",
                            "2",
                        )
                        or "2"
                    ),
                    0.5,
                ),
                30.0,
            ),
        )


class DiscordIpcConnection:
    def __init__(self, transport: socket.socket) -> None:
        self._transport = transport

    @classmethod
    def connect(cls) -> DiscordIpcConnection:
        failures: list[str] = []
        for path in discord_ipc_paths():
            transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                transport.connect(str(path))
            except OSError as exc:
                transport.close()
                failures.append(f"{path}:{exc.errno}")
                continue
            _LOGGER.info("Connected to Discord Desktop IPC at %s", path)
            return cls(transport)
        detail = ", ".join(failures[-3:]) or "no discord-ipc socket found"
        raise DiscordRpcCompanionError(f"discord_desktop_ipc_unavailable: {detail}")

    def close(self) -> None:
        try:
            self._transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._transport.close()
        except OSError:
            pass

    def send_json(self, opcode: int, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(
            dict(payload),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self._send_frame(opcode, raw)

    def receive_json(self) -> dict[str, Any]:
        while True:
            opcode, raw = self._receive_frame()
            if opcode == _PING:
                self._send_frame(_PONG, raw)
                continue
            if opcode == _CLOSE:
                detail = raw.decode("utf-8", errors="replace")[:500]
                raise DiscordRpcCompanionError(f"discord_rpc_closed:{detail}")
            if opcode != _FRAME:
                continue
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DiscordRpcCompanionError("discord_rpc_invalid_json") from exc
            if not isinstance(payload, dict):
                raise DiscordRpcCompanionError("discord_rpc_invalid_payload")
            return payload

    def _send_frame(self, opcode: int, raw: bytes) -> None:
        if len(raw) > _MAX_RPC_FRAME_BYTES:
            raise DiscordRpcCompanionError("discord_rpc_frame_too_large")
        self._transport.sendall(struct.pack("<II", opcode, len(raw)) + raw)

    def _receive_frame(self) -> tuple[int, bytes]:
        header = self._receive_exact(8)
        opcode, length = struct.unpack("<II", header)
        if length > _MAX_RPC_FRAME_BYTES:
            raise DiscordRpcCompanionError("discord_rpc_frame_too_large")
        return opcode, self._receive_exact(length)

    def _receive_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._transport.recv(remaining)
            if not chunk:
                raise DiscordRpcCompanionError("discord_rpc_connection_closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class DiscordRpcClient:
    def __init__(
        self,
        connection: DiscordIpcConnection,
        *,
        event_handler: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self.connection = connection
        self._event_handler = event_handler

    def handshake(self, client_id: str) -> None:
        self.connection.send_json(
            _HANDSHAKE,
            {"v": _RPC_VERSION, "client_id": client_id},
        )
        ready = self.connection.receive_json()
        if ready.get("cmd") != "DISPATCH" or ready.get("evt") != "READY":
            raise DiscordRpcCompanionError("discord_rpc_ready_expected")

    def command(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        event: str | None = None,
    ) -> dict[str, Any]:
        nonce = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "cmd": name,
            "args": dict(args),
            "nonce": nonce,
        }
        if event is not None:
            payload["evt"] = event
        self.connection.send_json(_FRAME, payload)
        while True:
            response = self.connection.receive_json()
            if response.get("cmd") == "DISPATCH":
                self._event_handler(response)
                continue
            if response.get("nonce") != nonce:
                continue
            if response.get("evt") == "ERROR":
                data = response.get("data")
                detail = json.dumps(data, ensure_ascii=False)[:500]
                raise DiscordRpcCompanionError(f"discord_rpc_{name.lower()}_failed:{detail}")
            data = response.get("data")
            if not isinstance(data, dict):
                raise DiscordRpcCompanionError(f"discord_rpc_{name.lower()}_invalid_response")
            return data

    def receive_forever(self) -> None:
        while True:
            payload = self.connection.receive_json()
            if payload.get("cmd") == "DISPATCH":
                self._event_handler(payload)


@dataclass(frozen=True, slots=True)
class DiscordOAuthToken:
    access_token: str
    expires_at: datetime

    def is_valid(self) -> bool:
        return datetime.now(UTC) + timedelta(seconds=60) < self.expires_at


class TerminalBridgePublisher:
    def __init__(
        self,
        config: DiscordRpcCompanionConfig,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.config = config
        self._opener = opener
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1_000)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="discord-signals-terminal-publisher",
            daemon=True,
        )
        self._thread.start()

    def publish(self, payload: Mapping[str, Any]) -> None:
        try:
            self._queue.put_nowait(dict(payload))
        except queue.Full as exc:
            raise DiscordRpcCompanionError("discord_terminal_bridge_queue_full") from exc

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None

    def _run(self) -> None:
        pending: dict[str, Any] | None = None
        retry_delay = 1.0
        while True:
            if pending is None:
                try:
                    pending = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if self._stopping.is_set():
                        return
                    continue
                if pending is None:
                    return
            try:
                self._post(pending)
            except Exception as exc:
                _LOGGER.warning("Terminal bridge publish failed: %s", str(exc)[:300])
                if self._stopping.wait(retry_delay):
                    return
                retry_delay = min(retry_delay * 2.0, 10.0)
                continue
            pending = None
            retry_delay = 1.0

    def _post(self, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(
            dict(payload),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            (f"{self.config.terminal_base_url}/api/indicators/discord-signals/rpc-ingest"),
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                _TERMINAL_HEADER: self.config.bridge_secret,
                "User-Agent": "MartinCall-DiscordRpcCompanion/0.1",
            },
            method="POST",
        )
        with self._opener(
            request,
            timeout=self.config.request_timeout_seconds,
        ) as response:
            response_body = response.read()
        try:
            acknowledgement = json.loads(response_body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiscordRpcCompanionError("discord_terminal_bridge_ack_invalid") from exc
        if not isinstance(acknowledgement, Mapping) or acknowledgement.get("ok") is not True:
            raise DiscordRpcCompanionError("discord_terminal_bridge_ack_invalid")
        if payload.get("event") == "status":
            return
        journal_outcomes = acknowledgement.get("journal_outcomes")
        if not isinstance(journal_outcomes, Mapping) or any(
            not isinstance(message_id, str)
            or not isinstance(outcome, str)
            or outcome not in DISCORD_SIGNAL_JOURNAL_OUTCOMES
            for message_id, outcome in journal_outcomes.items()
        ):
            raise DiscordRpcCompanionError("discord_terminal_bridge_ack_invalid")
        expected_message_id: object | None = None
        if payload.get("event") == "message_delete":
            expected_message_id = payload.get("message_id")
        elif payload.get("event") in {"message_create", "message_update"}:
            message = payload.get("message")
            if isinstance(message, Mapping):
                expected_message_id = message.get("id")
        if (
            isinstance(expected_message_id, str)
            and journal_outcomes.get(expected_message_id) not in DISCORD_SIGNAL_JOURNAL_OUTCOMES
        ):
            raise DiscordRpcCompanionError("discord_terminal_bridge_ack_invalid")
        if expected_message_id is not None and not isinstance(
            expected_message_id,
            str,
        ):
            raise DiscordRpcCompanionError("discord_terminal_bridge_ack_invalid")


class TerminalGateClient:
    def __init__(
        self,
        config: DiscordRpcCompanionConfig,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.config = config
        self._opener = opener

    def active(self) -> bool:
        request = urllib.request.Request(
            (f"{self.config.terminal_base_url}/api/indicators/discord-signals/companion-gate"),
            headers={
                "Accept": "application/json",
                _TERMINAL_HEADER: self.config.bridge_secret,
                "User-Agent": "MartinCall-DiscordRpcCompanion/0.2",
            },
            method="GET",
        )
        try:
            with self._opener(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
        ) as exc:
            raise DiscordRpcCompanionError(
                f"discord_terminal_gate_unavailable:{str(exc)[:300]}"
            ) from exc
        gate = payload.get("gate") if isinstance(payload, Mapping) else None
        active = gate.get("active") if isinstance(gate, Mapping) else None
        if not isinstance(active, bool):
            raise DiscordRpcCompanionError("discord_terminal_gate_response_invalid")
        return active


class DiscordRpcCompanion:
    def __init__(
        self,
        config: DiscordRpcCompanionConfig,
        publisher: TerminalBridgePublisher,
        gate_client: TerminalGateClient | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self.gate_client = gate_client or TerminalGateClient(config)
        self.instance_id = str(uuid.uuid4())
        self._oauth_token: DiscordOAuthToken | None = None
        self._rpc: DiscordRpcClient | None = None
        self._message_lock = threading.RLock()
        self._messages: dict[str, dict[str, Any]] = {}
        self._reconciling_channel_ids: set[str] = set()
        self._deferred_channel_events: dict[
            str,
            list[dict[str, Any]],
        ] = {}

    def run_forever(self) -> None:
        self.publisher.start()
        try:
            while True:
                self._wait_for_active_gate()
                self._publish_status("starting")
                try:
                    self._run_session()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    if not self._gate_active_fail_closed():
                        continue
                    detail = str(exc)[:500]
                    _LOGGER.warning("Discord RPC session failed: %s", detail)
                    self._publish_status("error", detail=detail)
                    self._publish_status("reconnecting")
                    time.sleep(3.0)
        except KeyboardInterrupt:
            _LOGGER.info("Stopping Discord Signals companion")
        finally:
            self._publish_status("stopped")
            self.publisher.stop()

    def _run_session(self) -> None:
        connection = DiscordIpcConnection.connect()
        monitor_stop = threading.Event()
        gate_closed = threading.Event()
        monitor_thread = threading.Thread(
            target=self._monitor_gate,
            args=(connection, monitor_stop, gate_closed),
            name="discord-signals-lease-gate",
            daemon=True,
        )
        monitor_thread.start()
        try:
            rpc = DiscordRpcClient(connection, event_handler=self._handle_event)
            self._rpc = rpc
            rpc.handshake(self.config.client_id)
            self._authenticate(rpc)
            initial_message_count = 0
            for configured_channel_id in self.config.channel_ids:
                snapshot_observed_at = datetime.now(UTC)
                with self._message_lock:
                    self._reconciling_channel_ids.add(configured_channel_id)
                    self._deferred_channel_events[configured_channel_id] = []
                for event in (
                    "MESSAGE_CREATE",
                    "MESSAGE_UPDATE",
                    "MESSAGE_DELETE",
                ):
                    rpc.command(
                        "SUBSCRIBE",
                        {"channel_id": configured_channel_id},
                        event=event,
                    )
                channel = rpc.command(
                    "GET_CHANNEL",
                    {"channel_id": configured_channel_id},
                )
                channel_id = require_discord_snowflake(
                    channel.get("id"),
                    field="channel_id",
                )
                if channel_id != configured_channel_id:
                    raise DiscordRpcCompanionError("discord_rpc_channel_mismatch")
                raw_history = channel.get("messages")
                if not isinstance(raw_history, list):
                    raise DiscordRpcCompanionError("discord_rpc_channel_history_invalid")
                if any(not isinstance(message, Mapping) for message in raw_history):
                    raise DiscordRpcCompanionError("discord_rpc_channel_history_invalid")
                bounded_history = [
                    dict(message) for message in raw_history[-self.config.history_limit :]
                ]
                history_ids = tuple(
                    require_discord_snowflake(
                        message.get("id"),
                        field="history_message_id",
                    )
                    for message in bounded_history
                )
                if len(set(history_ids)) != len(history_ids):
                    raise DiscordRpcCompanionError("discord_rpc_channel_history_duplicate")
                history = [
                    message for message in bounded_history if self._is_target_author(message)
                ]
                initial_message_count += len(history)
                self._remember_messages(
                    history,
                    channel_id=channel_id,
                    replace_channel=True,
                )
                self.publisher.publish(
                    self._envelope(
                        "snapshot",
                        channel_id=channel_id,
                        messages=history,
                        observed_at=snapshot_observed_at,
                    )
                )
                with self._message_lock:
                    deferred_events = tuple(self._deferred_channel_events.pop(channel_id, ()))
                    self._reconciling_channel_ids.discard(channel_id)
                for deferred_event in deferred_events:
                    self._handle_event(deferred_event)
            self._publish_status("live")
            _LOGGER.info(
                "Discord Signals live for %d channels; initial messages: %d",
                len(self.config.channel_ids),
                initial_message_count,
            )
            snapshot_stop = threading.Event()
            snapshot_thread = threading.Thread(
                target=self._publish_snapshot_loop,
                args=(snapshot_stop,),
                name="discord-signals-snapshot-replay",
                daemon=True,
            )
            snapshot_thread.start()
            try:
                rpc.receive_forever()
            finally:
                snapshot_stop.set()
                snapshot_thread.join()
        except Exception:
            if not gate_closed.is_set():
                raise
        finally:
            monitor_stop.set()
            self._rpc = None
            with self._message_lock:
                self._reconciling_channel_ids.clear()
                self._deferred_channel_events.clear()
            connection.close()
            monitor_thread.join()

    def _wait_for_active_gate(self) -> None:
        waiting_logged = False
        while True:
            if self._gate_active_fail_closed():
                if waiting_logged:
                    _LOGGER.info("Discord Signals lease gate opened")
                return
            if not waiting_logged:
                _LOGGER.info("Discord Signals dormant; waiting for an active indicator tab")
                waiting_logged = True
            time.sleep(self.config.gate_poll_seconds)

    def _gate_active_fail_closed(self) -> bool:
        try:
            return self.gate_client.active()
        except DiscordRpcCompanionError as exc:
            _LOGGER.debug("Discord Signals gate closed: %s", str(exc)[:300])
            return False

    def _monitor_gate(
        self,
        connection: DiscordIpcConnection,
        stop: threading.Event,
        gate_closed: threading.Event,
    ) -> None:
        while not stop.wait(self.config.gate_poll_seconds):
            if self._gate_active_fail_closed():
                continue
            gate_closed.set()
            connection.close()
            return

    def _authenticate(self, rpc: DiscordRpcClient) -> None:
        token = self._oauth_token
        if token is not None and token.is_valid():
            try:
                rpc.command("AUTHENTICATE", {"access_token": token.access_token})
                return
            except DiscordRpcCompanionError:
                self._oauth_token = None
        self._publish_status("authorizing")
        authorization = rpc.command(
            "AUTHORIZE",
            {
                "client_id": self.config.client_id,
                "scopes": ["rpc", "identify", "messages.read"],
            },
        )
        code = str(authorization.get("code") or "").strip()
        if not code:
            raise DiscordRpcCompanionError("discord_rpc_authorization_code_missing")
        token = exchange_discord_oauth_code(self.config, code)
        rpc.command("AUTHENTICATE", {"access_token": token.access_token})
        self._oauth_token = token

    def _handle_event(self, payload: Mapping[str, Any]) -> None:
        event = str(payload.get("evt") or "")
        if event not in {
            "MESSAGE_CREATE",
            "MESSAGE_UPDATE",
            "MESSAGE_DELETE",
        }:
            return
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise DiscordRpcCompanionError("discord_rpc_message_data_invalid")
        channel_id = require_discord_snowflake(
            data.get("channel_id"),
            field="channel_id",
        )
        if channel_id not in self.config.channel_ids:
            raise DiscordRpcCompanionError("discord_rpc_event_channel_mismatch")
        with self._message_lock:
            if channel_id in self._reconciling_channel_ids:
                self._deferred_channel_events[channel_id].append(dict(payload))
                return
        message = data.get("message")
        if not isinstance(message, Mapping):
            raise DiscordRpcCompanionError("discord_rpc_message_invalid")
        if event == "MESSAGE_DELETE":
            message_id = require_discord_snowflake(
                message.get("id"),
                field="message_id",
            )
            with self._message_lock:
                self._messages.pop(message_id, None)
            self.publisher.publish(
                self._envelope(
                    "message_delete",
                    channel_id=channel_id,
                    message_id=message_id,
                )
            )
            return
        if not self._is_target_author(message):
            return
        self._remember_messages((message,), channel_id=channel_id)
        self.publisher.publish(
            self._envelope(
                event.lower(),
                channel_id=channel_id,
                message=dict(message),
            )
        )

    def _is_target_author(self, message: Mapping[str, Any]) -> bool:
        author = message.get("author")
        if not isinstance(author, Mapping):
            return False
        try:
            author_id = require_discord_snowflake(
                author.get("id"),
                field="author_id",
            )
        except TypeError, ValueError:
            return False
        return author_id == self.config.author_id

    def _publish_status(self, state: str, *, detail: str = "") -> None:
        for channel_id in self.config.channel_ids:
            self.publisher.publish(
                self._envelope(
                    "status",
                    channel_id=channel_id,
                    state=state,
                    detail=detail,
                )
            )

    def _remember_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        channel_id: str,
        replace_channel: bool = False,
    ) -> None:
        with self._message_lock:
            if replace_channel:
                self._messages = {
                    message_id: message
                    for message_id, message in self._messages.items()
                    if message.get("channel_id") != channel_id
                }
            for message in messages:
                message_id = require_discord_snowflake(
                    message.get("id"),
                    field="message_id",
                )
                self._messages[message_id] = {
                    **dict(message),
                    "channel_id": channel_id,
                }
            channel_message_ids = sorted(
                (
                    message_id
                    for message_id, message in self._messages.items()
                    if message.get("channel_id") == channel_id
                ),
                key=int,
            )
            for message_id in channel_message_ids[: -self.config.history_limit]:
                self._messages.pop(message_id, None)

    def _message_snapshot(self, *, channel_id: str) -> list[dict[str, Any]]:
        with self._message_lock:
            return [
                dict(self._messages[message_id])
                for message_id in sorted(self._messages, key=int)
                if self._messages[message_id].get("channel_id") == channel_id
            ]

    def _publish_snapshot_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.config.snapshot_seconds):
            for channel_id in self.config.channel_ids:
                self.publisher.publish(
                    self._envelope(
                        "snapshot",
                        channel_id=channel_id,
                        messages=self._message_snapshot(channel_id=channel_id),
                    )
                )
            self._publish_status("live")

    def _envelope(
        self,
        event: str,
        *,
        channel_id: str,
        observed_at: datetime | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        exact_observed_at = observed_at or datetime.now(UTC)
        if exact_observed_at.tzinfo is None or exact_observed_at.utcoffset() is None:
            raise ValueError("Discord envelope observed_at must be timezone-aware")
        return {
            "contract": DISCORD_RPC_BRIDGE_CONTRACT,
            "event": event,
            "channel_id": channel_id,
            "instance_id": self.instance_id,
            "sent_at": exact_observed_at.astimezone(UTC).isoformat(),
            "heartbeat_seconds": self.config.snapshot_seconds,
            **payload,
        }


def discord_ipc_paths() -> tuple[Path, ...]:
    prefixes: list[Path] = []
    for name in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            path = Path(value)
            if path not in prefixes:
                prefixes.append(path)
    fallback = Path("/tmp")
    if fallback not in prefixes:
        prefixes.append(fallback)
    return tuple(prefix / f"discord-ipc-{index}" for prefix in prefixes for index in range(10))


def exchange_discord_oauth_code(
    config: DiscordRpcCompanionConfig,
    code: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> DiscordOAuthToken:
    body = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "MartinCall-DiscordRpcCompanion/0.1",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=config.request_timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise DiscordRpcCompanionError(f"discord_oauth_http_{exc.code}:{detail}") from exc
    except (OSError, TimeoutError) as exc:
        raise DiscordRpcCompanionError(f"discord_oauth_request_failed:{str(exc)[:300]}") from exc
    try:
        payload = json.loads(raw)
        access_token = str(payload.get("access_token") or "").strip()
        expires_in = max(int(payload.get("expires_in") or 0), 1)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DiscordRpcCompanionError("discord_oauth_response_invalid") from exc
    if not access_token:
        raise DiscordRpcCompanionError("discord_oauth_access_token_missing")
    return DiscordOAuthToken(
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = DiscordRpcCompanionConfig.from_environment()
        companion = DiscordRpcCompanion(
            config,
            TerminalBridgePublisher(config),
        )
        companion.run_forever()
    except (DiscordRpcCompanionError, TypeError, ValueError) as exc:
        _LOGGER.error("Cannot start Discord Signals companion: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
