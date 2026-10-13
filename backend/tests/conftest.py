from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import replace
from typing import Any

import pytest
import uvicorn

from app.config import get_settings
from app.main import create_app
from app.testing import PlatformClient


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Platform:
    """A real uvicorn server on a loopback port, running in the test's event loop."""

    def __init__(self, db: str, **overrides: Any) -> None:
        self.settings = replace(get_settings(), **{"checkpoint_db": db, "mock_token_delay_ms": 1, **overrides})
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.app = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._clients: list[PlatformClient] = []

    async def start(self, llm: Any = None) -> "Platform":
        self.app = create_app(self.settings, llm=llm)
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.01)
        return self

    async def stop(self) -> None:
        for c in self._clients:
            with contextlib.suppress(Exception):
                await c.close()
        self._clients.clear()
        if self._server:
            self._server.should_exit = True
            await self._task

    async def client(self, user: str = "alice") -> PlatformClient:
        c = PlatformClient(self.url, user)
        await c.connect()
        self._clients.append(c)
        return c

    async def session(self, user: str = "alice") -> tuple[PlatformClient, str]:
        c = await self.client(user)
        sid = await c.create_session()
        await c.subscribe(sid)
        return c, sid


@pytest.fixture
async def platform(tmp_path):
    p = await Platform(str(tmp_path / "state.sqlite")).start()
    yield p
    await p.stop()


@pytest.fixture
async def make_platform(tmp_path):
    """Factory for tests that need custom settings, a custom LLM, or a restart on the same database."""
    started: list[Platform] = []

    async def make(llm: Any = None, db: str = "state.sqlite", **overrides: Any) -> Platform:
        p = await Platform(str(tmp_path / db), **overrides).start(llm)
        started.append(p)
        return p

    yield make
    for p in started:
        with contextlib.suppress(Exception):
            await p.stop()


# --------------------------------------------------------------------------- SpiceDB
#
# Tests marked as needing SpiceDB run against a real server when one is
# available and are skipped otherwise:
#   SPICEDB_BIN=/path/to/spicedb   start a throwaway in-memory instance per test
#   (or `spicedb` on PATH)

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

SPICEDB_KEY = "test-spicedb-key"


def _spicedb_binary() -> str | None:
    return os.environ.get("SPICEDB_BIN") or shutil.which("spicedb")


@pytest.fixture
def spicedb_endpoint():
    binary = _spicedb_binary()
    if not binary:
        pytest.skip("SpiceDB binary not available (set SPICEDB_BIN)")
    port = free_port()
    proc = subprocess.Popen(
        [
            binary, "serve", "--grpc-preshared-key", SPICEDB_KEY, "--datastore-engine", "memory",
            "--grpc-addr", f"127.0.0.1:{port}", "--metrics-enabled=false", "--telemetry-endpoint=", "--log-level", "error",
        ],  # fmt: skip
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            time.sleep(0.05)
        else:
            pytest.fail("SpiceDB did not start")
        yield f"127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
