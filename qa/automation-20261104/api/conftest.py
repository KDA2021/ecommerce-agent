"""Real HTTP to project routes; in-memory state, no production container build."""

import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import uvicorn

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("Automation suite only permits loopback TCP")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def api_factory(monkeypatch):
    from app.application.order_confirmation import OrderConfirmationManager
    from app.presentation import server as project_server

    async def forbidden_build(*args, **kwargs):
        raise AssertionError("Production container must never be built in this suite")

    monkeypatch.setattr(project_server, "build_container", forbidden_build)
    resources = []

    def start(*, missing_manager=False, authenticated=False):
        manager = None if missing_manager else OrderConfirmationManager()

        class Container:
            settings = SimpleNamespace(
                api_key="",
                buyer_api_keys_json=(
                    '{"alice":"qa-alice-key-000001","bob":"qa-bob-key-0000001"}'
                    if authenticated
                    else ""
                ),
            )
            confirmation_manager = manager

            async def close(self):
                pass

        app = project_server.create_app(container=Container(), build_on_startup=False)
        lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def seeded_lifespan(instance):
            async with lifespan(instance):
                if manager:
                    await manager.create_preview(
                        {"buyer_id": "alice", "items": []}, token="qa-token"
                    )
                yield

        app.router.lifespan_context = seeded_lifespan
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        session = requests.Session()
        session.trust_env = False
        resources.append((server, thread, listener, session))
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Isolated API failed to start")
            time.sleep(0.02)
        return SimpleNamespace(base=f"http://127.0.0.1:{port}", http=session)

    yield start
    for server, thread, listener, session in reversed(resources):
        session.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Isolated API did not shut down")
