from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """AC9: any outbound connection in these tests is a bug."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted in a unit test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield
