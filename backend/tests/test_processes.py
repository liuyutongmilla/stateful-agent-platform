"""Tests that need real OS processes: a hard kill, and the split MCP deployment."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys

import httpx

from app.config import get_settings
from app.testing import PlatformClient
from tests.conftest import free_port


async def wait_port(port: int, proc: subprocess.Popen) -> None:
    for _ in range(300):
        if proc.poll() is not None:
            raise RuntimeError(f"process exited early with code {proc.returncode}")
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("server did not start")


def spawn(module_args: list[str], **env: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", *module_args],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def test_run_survives_sigkill_and_resumes_from_the_last_checkpoint(tmp_path):
    port, db = free_port(), str(tmp_path / "state.sqlite")
    args = ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    env = {"CHECKPOINT_DB": db, "MOCK_TOKEN_DELAY_MS": "40"}
    proc = spawn(args, **env)
    try:
        await wait_port(port, proc)
        c = PlatformClient(f"http://127.0.0.1:{port}", "alice")
        await c.connect()
        sid = await c.create_session()
        await c.subscribe(sid)
        await c.send(sid, "run a health check")
        # two reasoning turns and three tool calls are done; the third turn is mid-stream
        await c.wait_for(lambda: len(c.of_type(sid, "tool_result")) == 3 and len(c.of_type(sid, "token")) >= 3)
        tokens_before_kill = len(c.of_type(sid, "token"))

        proc.send_signal(signal.SIGKILL)  # no shutdown hooks, no status update, nothing flushed
        proc.wait()
        await c.drop()

        proc = spawn(args, **env)
        await wait_port(port, proc)
        await c.reconnect()

        snap = c.snapshots[sid]
        assert c.modes[sid] == ["snapshot", "snapshot"]  # new process, new epoch: full state, not replay
        assert (snap["status"], snap["stage"], snap["iterations"], snap["resumable"]) == ("interrupted", "execute", 2, True)
        assert sum(1 for m in snap["messages"] if m["role"] == "tool") == 3
        assert snap["plan"] and snap["allowed_tools"]

        await c.resume(sid)
        await c.wait_status(sid, "idle")
        assert c.of_type(sid, "resumed")[0]["data"]["next"] == ["reason"]
        final = c.of_type(sid, "final")[0]["data"]["text"]
        assert "search-api is degraded" in final and "Recommendation" in final
        assert len(c.of_type(sid, "tool_call")) == 3  # all from before the kill; none re-executed
        assert tokens_before_kill < len(final.split())
        await c.close()
    finally:
        proc.kill()
        proc.wait()


async def test_tools_work_over_streamable_http_to_a_separate_mcp_process(make_platform):
    """Caller context must survive a real network hop, not just an in-process call."""
    mcp_port = free_port()
    secret = get_settings().secret
    proc = spawn(["app.mcp_server"], MCP_PORT=str(mcp_port), MCP_HOST="127.0.0.1", PLATFORM_SECRET=secret)
    try:
        await wait_port(mcp_port, proc)
        p = await make_platform(mcp_url=f"http://127.0.0.1:{mcp_port}/mcp")
        assert not hasattr(p.app.state, "admin_server")  # nothing in-process

        async with httpx.AsyncClient() as http:
            token = (await http.post(f"{p.url}/api/login", json={"user": "alice"})).json()["token"]
            catalog = (await http.get(f"{p.url}/api/tools", headers={"Authorization": f"Bearer {token}"})).json()
        assert len(catalog) >= 15

        alice, sid = await p.session("alice")
        await alice.send(sid, "run a health check")
        await alice.wait_status(sid, "idle")
        assert alice.of_type(sid, "review")[0]["data"]["ok"] == 3

        dave, d_sid = await p.session("dave")
        await dave.send(d_sid, "restart api-gateway")
        await dave.wait_status(d_sid, "awaiting_approval")
        await dave.approve(d_sid, True)
        await dave.wait_status(d_sid, "idle")
        assert dave.of_type(d_sid, "tool_result")[0]["data"]["status"] == "denied"

        # 20 sessions sharing the one MCP connection concurrently
        clients = [await p.session("bob") for _ in range(20)]
        for c, s in clients:
            await c.send(s, "list flags")
        await asyncio.gather(*(c.wait_status(s, "idle") for c, s in clients))
        assert all(c.of_type(s, "tool_result")[0]["data"]["status"] == "ok" for c, s in clients)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
