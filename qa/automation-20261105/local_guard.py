"""Explicit pytest plugin: deny external TCP and existing local DB services."""

import socket

import pytest


@pytest.fixture(autouse=True)
def qa_network_boundary(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple):
            if address[0] not in ("127.0.0.1", "::1"):
                raise RuntimeError("QA only allows loopback network")
            if address[1] in (3306, 6379):
                raise RuntimeError("QA must not use existing MySQL/Redis services")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
