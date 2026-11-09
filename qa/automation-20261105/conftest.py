import socket
import threading
import time
from queue import Queue
from types import SimpleNamespace

import pytest
import requests
import uvicorn
from qa_harness import ALICE_KEY, BOB_KEY, CONTROL, create_qa_app


def session(key=ALICE_KEY):
    client = requests.Session()
    client.trust_env = False
    client.headers["X-API-Key"] = key
    return client


@pytest.fixture
def commerce(tmp_path):
    app = create_qa_app(tmp_path, timeout=1)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=2)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    alice, bob = session(), session(BOB_KEY)
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "QA startup failed"
            time.sleep(0.01)
        yield SimpleNamespace(
            base=base,
            alice=alice,
            bob=bob,
            app=app,
            state=lambda: alice.get(
                base + "/__qa/state", headers={"X-QA-Control": CONTROL}, timeout=3
            ).json(),
        )
    finally:
        alice.close()
        bob.close()
        server.should_exit = True
        thread.join(8)
        listener.close()
        assert not thread.is_alive(), "QA server failed to exit"


class EventStream:
    """Real SSE over HTTP; bounded wait and transport errors exposed to tests."""

    def __init__(self, commerce, sid):
        self.client = session()
        self.response = self.client.get(
            commerce.base + "/commerce/events",
            params={"shopping_session_id": sid},
            stream=True,
            timeout=(2, 4),
        )
        assert self.response.status_code == 200, self.response.text
        self.events = Queue()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        import json

        name = ""
        try:
            for line in self.response.iter_lines(chunk_size=1, decode_unicode=True):
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    self.events.put((name, json.loads(line[6:])))
        except Exception as exc:
            self.events.put(("transport-error", str(exc)))

    def get(self, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event, payload = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event == name:
                return payload
            if event == "transport-error":
                raise AssertionError(payload)
        raise AssertionError(f"SSE event missing: {name}")

    def close(self):
        self.response.close()
        self.client.close()
        self.thread.join(5)


@pytest.fixture
def stream_factory(commerce):
    streams = []

    def start(sid):
        s = EventStream(commerce, sid)
        streams.append(s)
        deadline = time.monotonic() + 3
        while commerce.state()["subscribers"] < len(streams):
            assert time.monotonic() < deadline, "SSE subscriber not ready"
            time.sleep(0.01)
        return s

    yield start
    for s in streams:
        s.close()
