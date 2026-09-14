from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.config import AppConfig

_REQUEST_LINE = b"START_LOGIN\n"
_ACCEPTED_LINE = "ACCEPTED"


@dataclass(frozen=True)
class IbkrGatewayLoginControlError(RuntimeError):
    code: str
    message: str
    retryable: bool
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        return self.message


class IbkrGatewayLoginControl:
    """Request one explicit cold Gateway login through the private control lane."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        cooldown_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        create_connection: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self._host = str(host or "").strip()
        self._port = int(port)
        self._timeout_seconds = max(0.2, min(float(timeout_seconds), 8.0))
        self._cooldown_seconds = max(5.0, min(float(cooldown_seconds), 300.0))
        self._monotonic = monotonic
        self._create_connection = create_connection
        self._request_lock = threading.Lock()
        self._last_accepted_at: float | None = None

    @classmethod
    def from_config(cls, config: AppConfig) -> IbkrGatewayLoginControl:
        return cls(
            host=config.ibkr_gateway_login_control_host,
            port=config.ibkr_gateway_login_control_port,
            timeout_seconds=config.ibkr_gateway_login_control_timeout_seconds,
            cooldown_seconds=config.ibkr_gateway_login_control_cooldown_seconds,
        )

    @property
    def configured(self) -> bool:
        return bool(self._host and 1 <= self._port <= 65535)

    def status(self) -> dict[str, Any]:
        last_accepted_at = self._last_accepted_at
        remaining = 0.0
        if last_accepted_at is not None:
            remaining = max(
                0.0,
                self._cooldown_seconds - (self._monotonic() - last_accepted_at),
            )
        return {
            "configured": self.configured,
            "cooldown_seconds": self._cooldown_seconds,
            "cooldown_remaining_seconds": remaining,
        }

    def request_login(self) -> dict[str, Any]:
        if not self.configured:
            raise IbkrGatewayLoginControlError(
                code="IBKR_GATEWAY_LOGIN_CONTROL_NOT_CONFIGURED",
                message="IB Gateway login control is not configured for this runtime.",
                retryable=False,
            )
        if not self._request_lock.acquire(blocking=False):
            raise IbkrGatewayLoginControlError(
                code="IBKR_GATEWAY_LOGIN_REQUEST_IN_PROGRESS",
                message="An IB Gateway login request is already in progress.",
                retryable=True,
                retry_after_seconds=1.0,
            )
        try:
            now = self._monotonic()
            if self._last_accepted_at is not None:
                remaining = self._cooldown_seconds - (now - self._last_accepted_at)
                if remaining > 0:
                    raise IbkrGatewayLoginControlError(
                        code="IBKR_GATEWAY_LOGIN_REQUEST_COOLDOWN",
                        message="A recent IB Gateway login request is still settling.",
                        retryable=True,
                        retry_after_seconds=remaining,
                    )
            try:
                with self._create_connection(
                    (self._host, self._port),
                    timeout=self._timeout_seconds,
                ) as control_socket:
                    control_socket.settimeout(self._timeout_seconds)
                    control_socket.sendall(_REQUEST_LINE)
                    response = control_socket.recv(128)
            except OSError as exc:
                raise IbkrGatewayLoginControlError(
                    code="IBKR_GATEWAY_LOGIN_CONTROL_UNAVAILABLE",
                    message="IB Gateway login control is unavailable.",
                    retryable=True,
                ) from exc
            response_line = response.decode("ascii", errors="replace").strip()
            if response_line != _ACCEPTED_LINE:
                raise IbkrGatewayLoginControlError(
                    code="IBKR_GATEWAY_LOGIN_CONTROL_REJECTED",
                    message="IB Gateway login control rejected the request.",
                    retryable=True,
                )
            self._last_accepted_at = self._monotonic()
            return {
                "ok": True,
                "accepted": True,
                "message": "IB Gateway is starting one new login attempt.",
                "control": self.status(),
            }
        finally:
            self._request_lock.release()


IBKR_GATEWAY_LOGIN_CONTROL = IbkrGatewayLoginControl.from_config(AppConfig())


def ibkr_gateway_login_control_status() -> dict[str, Any]:
    return IBKR_GATEWAY_LOGIN_CONTROL.status()


def request_ibkr_gateway_login() -> dict[str, Any]:
    return IBKR_GATEWAY_LOGIN_CONTROL.request_login()
