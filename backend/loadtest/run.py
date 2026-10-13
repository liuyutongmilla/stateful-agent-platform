"""Load and recovery test for the streaming gateway.

    python loadtest/run.py                       # boots its own server, 50 sessions
    python loadtest/run.py --url http://host:8000 --sessions 50 --connections 10

Each session runs one tool-using request (exercising MCP, authorization and
checkpoints) and then one long streamed answer. Sessions are multiplexed over
a smaller number of WebSocket connections.

Two phases:
  steady   no failures; measures token delivery latency
  chaos    every connection is killed mid-stream and reconnects; verifies that
           each session still receives every token exactly once, in order

Latency is `client receive time - server token creation time`, so it is only
meaningful when client and server share a clock (same host). It measures the
platform's delivery path (graph stream -> hub -> socket -> client), NOT model
latency: the server uses the mock model, which emits a token every
MOCK_TOKEN_DELAY_MS.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.testing import PlatformClient  # noqa: E402

USERS = ["alice", "bob", "carol"]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


async def run_session(c: PlatformClient, sid: str, tokens: int) -> None:
    await c.send(sid, "run a health check" if c.user != "carol" else "show the audit log")
    first = await c.wait_status(sid, "idle", timeout=120)
    await c.send(sid, f"stream {tokens}")
    await c.wait_status(sid, "idle", timeout=300, after=first["seq"])


async def chaos_monkey(c: PlatformClient, rng: random.Random, kills: int, window: float) -> None:
    for _ in range(kills):
        await asyncio.sleep(rng.uniform(0.2, window))
        await c.drop()
        await asyncio.sleep(rng.uniform(0.05, 0.4))
        await c.reconnect()


async def phase(name: str, url: str, sessions: int, connections: int, tokens: int, kills: int, seed: int) -> dict:
    rng = random.Random(seed)
    clients = [PlatformClient(url, USERS[i % len(USERS)]) for i in range(connections)]
    await asyncio.gather(*(c.connect() for c in clients))
    owners: list[tuple[PlatformClient, str]] = []
    for i in range(sessions):
        c = clients[i % connections]
        sid = await c.create_session()
        await c.subscribe(sid)
        owners.append((c, sid))

    started = time.perf_counter()
    token_delay = float(os.environ.get("MOCK_TOKEN_DELAY_MS", "15")) / 1000
    monkeys = [asyncio.create_task(chaos_monkey(c, rng, kills, tokens * token_delay / (kills + 1))) for c in clients] if kills else []
    await asyncio.gather(*(run_session(c, sid, tokens) for c, sid in owners))
    for m in monkeys:
        m.cancel()
    await asyncio.gather(*monkeys, return_exceptions=True)
    elapsed = time.perf_counter() - started

    expected = " ".join(f"tok{i}" for i in range(tokens))
    intact = 0
    for c, sid in owners:
        seqs = [e["seq"] for e in c.events[sid]]
        streamed = "".join(e["data"]["text"] for e in c.of_type(sid, "token") if ":final" not in e["data"]["id"])
        if seqs == list(range(1, len(seqs) + 1)) and streamed.endswith(expected) and len(c.of_type(sid, "final")) == 2:
            intact += 1
    latency = [x for c in clients for x in c.token_latency_ms]
    result = {
        "phase": name,
        "sessions": sessions,
        "connections": connections,
        "completed": intact,
        "elapsed_s": round(elapsed, 2),
        "live_tokens": len(latency),
        "replayed_tokens": sum(c.replayed_tokens for c in clients),
        "tokens_per_s": round(len(latency) / elapsed),
        "p50_ms": round(percentile(latency, 50), 2),
        "p95_ms": round(percentile(latency, 95), 2),
        "p99_ms": round(percentile(latency, 99), 2),
        "max_ms": round(max(latency), 2),
        "mean_ms": round(statistics.fmean(latency), 2),
        "reconnects": sum(c.reconnects for c in clients),
        "gaps": sum(c.gaps for c in clients),
        "duplicates_dropped": sum(c.duplicates for c in clients),
        "errors": sum(len(c.errors) for c in clients),
    }
    await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
    return result


def report(r: dict) -> None:
    print(f"\n== {r['phase']} ==")
    print(f"  sessions completed intact : {r['completed']}/{r['sessions']} over {r['connections']} connections in {r['elapsed_s']}s")
    print(f"  tokens delivered live     : {r['live_tokens']} ({r['tokens_per_s']}/s)   replayed after reconnect: {r['replayed_tokens']}")
    print(f"  delivery latency (ms)     : p50 {r['p50_ms']}  p95 {r['p95_ms']}  p99 {r['p99_ms']}  max {r['max_ms']}  mean {r['mean_ms']}")
    print(f"  reconnects {r['reconnects']}   sequence gaps {r['gaps']}   duplicates dropped {r['duplicates_dropped']}   protocol errors {r['errors']}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="existing server; omit to start one for the test")
    ap.add_argument("--sessions", type=int, default=50)
    ap.add_argument("--connections", type=int, default=10)
    ap.add_argument("--tokens", type=int, default=400, help="tokens in each session's streamed answer")
    ap.add_argument("--kills", type=int, default=2, help="chaos phase: times each connection is killed")
    ap.add_argument("--p95-budget-ms", type=float, default=200)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    proc = None
    url = args.url
    if not url:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        db = os.path.join(tempfile.mkdtemp(prefix="loadtest-"), "state.sqlite")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "CHECKPOINT_DB": db, "AGENT_REQUIRE_APPROVAL": "0"},
        )
        url = f"http://127.0.0.1:{port}"
        for _ in range(200):
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            await asyncio.sleep(0.05)
    try:
        steady = await phase("steady", url, args.sessions, args.connections, args.tokens, 0, args.seed)
        report(steady)
        chaos = await phase("chaos", url, args.sessions, args.connections, args.tokens, args.kills, args.seed)
        report(chaos)
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)

    ok = (
        steady["completed"] == args.sessions
        and chaos["completed"] == args.sessions
        and steady["gaps"] == chaos["gaps"] == 0
        and steady["p95_ms"] < args.p95_budget_ms
    )
    print(f"\n{'PASS' if ok else 'FAIL'}: {args.sessions} concurrent sessions, steady p95 {steady['p95_ms']} ms (budget {args.p95_budget_ms} ms), "
          f"chaos recovered {chaos['completed']}/{args.sessions} sessions across {chaos['reconnects']} reconnects")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
