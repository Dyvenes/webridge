"""Запуск fake_site через uvicorn в фоновом потоке."""
from __future__ import annotations

import socket
import threading
import time
import urllib.request

import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeSite:
    def __init__(self) -> None:
        from tests.fake_site.app import app

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeSite":
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url, timeout=0.5)
                break
            except Exception:
                time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)