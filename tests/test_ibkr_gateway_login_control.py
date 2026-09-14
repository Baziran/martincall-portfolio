from __future__ import annotations

from typing import Any

import pytest

from aef_terminal.ui.ibkr_gateway_login_control import (
    IbkrGatewayLoginControl,
    IbkrGatewayLoginControlError,
)


class _FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""
        self.timeout = 0.0

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, _size: int) -> bytes:
        return self.response


def test_gateway_login_control_sends_exact_request_and_accepts_exact_response() -> None:
    connection = _FakeSocket(b"ACCEPTED\n")
    targets: list[tuple[tuple[str, int], float]] = []

    def connect(target: tuple[str, int], *, timeout: float) -> _FakeSocket:
        targets.append((target, timeout))
        return connection

    control = IbkrGatewayLoginControl(
        host="ib-gateway",
        port=7463,
        timeout_seconds=2.0,
        cooldown_seconds=30.0,
        monotonic=lambda: 100.0,
        create_connection=connect,
    )

    result = control.request_login()

    assert result["ok"] is True
    assert result["accepted"] is True
    assert connection.sent == b"START_LOGIN\n"
    assert targets == [(("ib-gateway", 7463), 2.0)]


def test_gateway_login_control_rejects_repeat_during_cooldown() -> None:
    now = [100.0]
    connection = _FakeSocket(b"ACCEPTED\n")
    control = IbkrGatewayLoginControl(
        host="ib-gateway",
        port=7463,
        timeout_seconds=2.0,
        cooldown_seconds=30.0,
        monotonic=lambda: now[0],
        create_connection=lambda *_args, **_kwargs: connection,
    )
    control.request_login()
    now[0] = 105.0

    with pytest.raises(IbkrGatewayLoginControlError) as raised:
        control.request_login()

    assert raised.value.code == "IBKR_GATEWAY_LOGIN_REQUEST_COOLDOWN"
    assert raised.value.retry_after_seconds == 25.0


def test_gateway_login_control_requires_configuration_and_exact_acknowledgement() -> None:
    unconfigured = IbkrGatewayLoginControl(
        host="",
        port=0,
        timeout_seconds=2.0,
        cooldown_seconds=30.0,
    )
    with pytest.raises(IbkrGatewayLoginControlError) as unconfigured_error:
        unconfigured.request_login()
    assert unconfigured_error.value.code == "IBKR_GATEWAY_LOGIN_CONTROL_NOT_CONFIGURED"

    rejected = IbkrGatewayLoginControl(
        host="ib-gateway",
        port=7463,
        timeout_seconds=2.0,
        cooldown_seconds=30.0,
        create_connection=lambda *_args, **_kwargs: _FakeSocket(b"REJECTED\n"),
    )
    with pytest.raises(IbkrGatewayLoginControlError) as rejected_error:
        rejected.request_login()
    assert rejected_error.value.code == "IBKR_GATEWAY_LOGIN_CONTROL_REJECTED"
