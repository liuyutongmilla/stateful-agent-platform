"""The WebSocket gateway: auth, multiplexing, ordering, replay after disconnects."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from app.gateway.hub import Channel, Connection, Hub


def expected(n: int) -> str:
    return " ".join(f"tok{i}" for i in range(n))


def seqs(client, sid) -> list[int]:
    return [e["seq"] for e in client.events[sid]]


# ---------------------------------------------------------------- hub unit


def test_replay_returns_exactly_the_missed_frames():
    ch = Channel("s", buffer=5)
    for i in range(3):
        ch.publish("t", {"i": i})
    assert [json.loads(f)["seq"] for f in ch.replay_after(1)] == [2, 3]
    assert ch.replay_after(3) == []
    assert ch.replay_after(0) is not None and len(ch.replay_after(0)) == 3
    assert ch.replay_after(99) is None  # client claims a future seq: do not trust it


def test_replay_gives_up_when_the_buffer_no_longer_reaches_back():
    ch = Channel("s", buffer=5)
    for i in range(20):
        ch.publish("t", {"i": i})
    assert ch.replay_after(10) is None
    assert [json.loads(f)["seq"] for f in ch.replay_after(15)] == [16, 17, 18, 19, 20]


async def test_slow_consumer_is_flagged_instead_of_buffering_forever():
    hub, conn = Hub(buffer=100), Connection(max_queue=10)
    hub.subscribe(conn, "s")
    for i in range(50):
        hub.publish("s", "t", {"i": i})
    assert conn.overflowed.is_set() and conn.queue.qsize() == 10
    hub.drop(conn)
    assert not hub.channel("s").subscribers


# -------------------------------------------------------------------- auth


async def connect_raw(platform):
    return await websockets.connect(platform.url.replace("http", "ws", 1) + "/ws")


@pytest.mark.parametrize("first_frame", ['{"op":"subscribe","ch":"x"}', '{"op":"auth","token":"forged"}', "not json"])
async def test_socket_is_closed_unless_the_first_frame_authenticates(platform, first_frame):
    ws = await connect_raw(platform)
    await ws.send(first_frame)
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await ws.recv()
    assert closed.value.rcvd.code == 1008


async def test_sessions_are_private_to_their_owner(platform):
    alice, sid = await platform.session("alice")
    bob = await platform.client("bob")
    for op in ("subscribe", "send", "resume", "cancel", "approve"):
        await bob._op(op, ch=sid, text="list users", approved=True)
        err = await bob.wait_for(lambda: bob.errors.pop() if bob.errors else None)
        assert err["code"] == "not_found", op
    await asyncio.sleep(0.1)
    assert not alice.events[sid] and not bob.events[sid]


async def test_malformed_frames_get_an_error_and_the_connection_survives(platform):
    c, sid = await platform.session("alice")
    await c.ws.send("[1,2,3]")
    await c.ws.send('{"op":"teleport","ch":"%s"}' % sid)
    await c.ws.send('{"op":"send","ch":"%s","text":""}' % sid)
    await c.wait_for(lambda: len(c.errors) == 3)
    assert {e["code"] for e in c.errors} == {"bad_request"}
    await c.send(sid, "stream 5")
    await c.wait_status(sid, "idle")
    assert c.text(sid) == expected(5)


# ------------------------------------------------------------ multiplexing


async def test_one_socket_carries_many_sessions_concurrently(make_platform):
    p = await make_platform(mock_token_delay_ms=2)
    c = await p.client("alice")
    sids = [await c.create_session() for _ in range(5)]
    for sid in sids:
        await c.subscribe(sid)
    for sid in sids:
        await c.send(sid, "stream 80")
    await asyncio.gather(*(c.wait_status(sid, "idle") for sid in sids))

    for sid in sids:
        assert c.text(sid) == expected(80)
        assert seqs(c, sid) == list(range(1, len(c.events[sid]) + 1))  # per-channel order, no gaps
    assert c.gaps == 0 and c.duplicates == 0
    # frames from different sessions really were interleaved on the wire
    switches = sum(1 for a, b in zip(c.arrivals, c.arrivals[1:]) if a != b)
    assert switches > 100


async def test_two_connections_can_watch_the_same_session(platform):
    a, sid = await platform.session("alice")
    b = await platform.client("alice")
    await b.subscribe(sid)
    await a.send(sid, "stream 20")
    await asyncio.gather(a.wait_status(sid, "idle"), b.wait_status(sid, "idle"))
    assert a.text(sid) == b.text(sid) == expected(20)


async def test_unsubscribe_stops_delivery(make_platform):
    p = await make_platform(mock_token_delay_ms=5)
    c, sid = await p.session("alice")
    await c.send(sid, "stream 200")
    await c.wait_for(lambda: len(c.of_type(sid, "token")) > 5)
    await c._op("unsubscribe", ch=sid)
    await asyncio.sleep(0.1)
    count = len(c.events[sid])
    await asyncio.sleep(0.2)
    assert len(c.events[sid]) == count


# --------------------------------------------------------------- recovery


async def test_reconnect_mid_stream_replays_missed_tokens_exactly_once(make_platform):
    p = await make_platform(mock_token_delay_ms=3)
    c, sid = await p.session("alice")
    await c.send(sid, "stream 400")
    for threshold in (40, 150, 260):  # three separate connection failures during one answer
        await c.wait_for(lambda t=threshold: len(c.of_type(sid, "token")) >= t)
        await c.drop()
        await asyncio.sleep(0.15)  # tokens keep being produced while we are away
        await c.reconnect()
    await c.wait_status(sid, "idle")

    assert c.modes[sid] == ["snapshot", "replay", "replay", "replay"]
    assert c.text(sid) == expected(400)
    assert c.gaps == 0
    assert seqs(c, sid) == list(range(1, len(c.events[sid]) + 1))


async def test_run_finishes_while_the_client_is_gone(make_platform):
    p = await make_platform(mock_token_delay_ms=1)
    c, sid = await p.session("alice")
    await c.send(sid, "run a health check")
    await c.wait_for(lambda: c.of_type(sid, "stage"))
    await c.drop()
    await asyncio.sleep(1.0)
    assert (await p.app.state.sessions.get(sid))["status"] == "idle"  # completed with nobody connected
    await c.reconnect()
    await c.wait_status(sid, "idle")
    assert "search-api is degraded" in c.of_type(sid, "final")[0]["data"]["text"]
    assert c.gaps == 0


async def test_client_too_far_behind_gets_a_snapshot_instead(make_platform):
    p = await make_platform(mock_token_delay_ms=1, replay_buffer=50)
    c, sid = await p.session("alice")
    await c.send(sid, "stream 300")
    await c.wait_for(lambda: len(c.of_type(sid, "token")) >= 10)
    await c.drop()
    await asyncio.sleep(1.0)
    await c.reconnect()

    assert c.modes[sid][-1] == "snapshot"
    snap = c.snapshots[sid]
    assert snap["status"] == "idle"
    assert snap["messages"][-1]["content"] == expected(300)
    # and the channel is usable again from the snapshot's sequence number
    await c.send(sid, "stream 5")
    await c.wait_for(lambda: len(c.of_type(sid, "final")) >= 1)
    assert c.gaps == 0


async def test_commands_are_applied_once_even_when_resent(platform):
    """A client that never saw its ack resends the same command id after reconnecting."""
    c, sid = await platform.session("alice")
    frame = json.dumps({"op": "send", "ch": sid, "id": "op-1", "text": "stream 5"})
    await c.ws.send(frame)
    await c.wait_status(sid, "idle")
    await c.ws.send(frame)  # the retry
    await c.ws.send(json.dumps({"op": "send", "ch": sid, "id": "op-2", "text": "stream 3"}))
    await c.wait_for(lambda: len(c.of_type(sid, "final")) == 2)
    assert [e["data"]["text"] for e in c.of_type(sid, "user_message")] == ["stream 5", "stream 3"]
    assert not c.errors


async def test_command_sent_into_a_dead_socket_is_delivered_after_reconnect(make_platform):
    p = await make_platform(mock_token_delay_ms=1)
    c, sid = await p.session("alice")
    await c.drop()
    await c.send(sid, "stream 10")  # nowhere to go yet
    await asyncio.sleep(0.1)
    assert not c.events[sid]
    await c.reconnect()
    await c.wait_status(sid, "idle")
    assert c.text(sid) == expected(10) and len(c.of_type(sid, "user_message")) == 1
