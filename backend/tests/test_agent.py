"""The agent runtime end to end: stages, the loop bound, checkpoints, approvals, resume."""

from __future__ import annotations

import json

from app.agent.llm import MockLLM
from app.agent.state import STAGES


class CountingLLM(MockLLM):
    def __init__(self) -> None:
        super().__init__(token_delay_ms=0)
        self.calls = {"plan": 0, "act": 0}

    async def complete(self, purpose, system, messages, tools, on_token):
        self.calls[purpose] += 1
        return await super().complete(purpose, system, messages, tools, on_token)


class FailsOnce(CountingLLM):
    """Raises on the Nth reasoning call the first time it gets there, like a provider outage."""

    def __init__(self, fail_on_act_call: int) -> None:
        super().__init__()
        self._fail_on, self._failed = fail_on_act_call, False

    async def complete(self, purpose, system, messages, tools, on_token):
        if purpose == "act" and self.calls["act"] + 1 == self._fail_on and not self._failed:
            self._failed = True
            raise ConnectionError("model provider unavailable")
        return await super().complete(purpose, system, messages, tools, on_token)


def stages_seen(client, sid) -> list[str]:
    out: list[str] = []
    for e in client.of_type(sid, "stage"):
        if not out or out[-1] != e["data"]["stage"]:
            out.append(e["data"]["stage"])
    return out


def audit(platform, tool: str, org: str = "acme") -> list[dict]:
    return [e for e in platform.app.state.admin_server.store.audit[org] if e["tool"] == tool]


def config(sid: str) -> dict:
    return {"configurable": {"thread_id": sid}}


# ------------------------------------------------------------------ stages


async def test_run_goes_through_the_five_stages_in_order(platform):
    c, sid = await platform.session("alice")
    await c.send(sid, "run a health check")
    await c.wait_status(sid, "idle")

    assert stages_seen(c, sid) == list(STAGES)
    assert c.of_type(sid, "plan")[0]["data"]["steps"]
    review = c.of_type(sid, "review")[0]["data"]
    assert review == {
        "tool_calls": 3, "ok": 3, "denied": 0, "rejected": 0, "errors": 0,
        "iterations": 3, "max_iterations": 10, "limit_reached": False,
    }  # fmt: skip
    final = c.of_type(sid, "final")[0]["data"]["text"]
    assert "search-api is degraded" in final and final == c.text(sid)
    # the second reasoning turn issued two tool calls at once
    assert [e["data"]["name"] for e in c.of_type(sid, "tool_call")] == ["list_services", "get_service_status", "list_incidents"]


async def test_a_checkpoint_is_persisted_at_every_stage(platform):
    c, sid = await platform.session("alice")
    await c.send(sid, "run a health check")
    await c.wait_status(sid, "idle")

    graph = platform.app.state.runtime.graph
    history = [s async for s in graph.aget_state_history(config(sid))]
    # every node was, at some point, the durable "next thing to do"
    pending_nodes = {n for s in history for n in s.next}
    assert pending_nodes >= {"intake", "plan", "reason", "act", "review", "finalize"}
    # and the stage recorded in checkpoints advances through all five
    recorded = {s.values.get("stage") for s in history}
    assert recorded >= {"intake", "plan", "execute", "review", "done"}
    # intermediate context is in the checkpoint, not just the final answer
    mid = next(s for s in history if s.next == ("review",))
    assert mid.values["iterations"] == 3 and mid.values["plan"]
    assert sum(1 for m in mid.values["messages"] if m["role"] == "tool") == 3


# -------------------------------------------------------------- loop bound


async def test_react_loop_stops_after_ten_model_invocations(make_platform):
    llm = CountingLLM()
    p = await make_platform(llm)
    c, sid = await p.session("alice")
    await c.send(sid, "loop forever")
    await c.wait_status(sid, "idle")

    assert llm.calls == {"plan": 1, "act": 10}
    assert len(c.of_type(sid, "tool_call")) == 10
    assert c.of_type(sid, "limit")[0]["data"] == {"iterations": 10, "max_iterations": 10}
    review = c.of_type(sid, "review")[0]["data"]
    assert review["limit_reached"] and review["iterations"] == 10
    # the run still finishes cleanly through review and finalize
    assert stages_seen(c, sid) == list(STAGES)
    assert "limit of 10" in c.of_type(sid, "final")[0]["data"]["text"]
    snapshot = await p.app.state.runtime.snapshot(await p.app.state.sessions.get(sid))
    assert snapshot["status"] == "idle" and not snapshot["resumable"]


async def test_the_limit_is_configurable_and_resets_per_run(make_platform):
    llm = CountingLLM()
    p = await make_platform(llm, max_iterations=3)
    c, sid = await p.session("alice")
    await c.send(sid, "loop forever")
    done = await c.wait_status(sid, "idle")
    assert llm.calls["act"] == 3

    await c.send(sid, "list flags")  # same thread: history kept, counters reset
    await c.wait_status(sid, "idle", after=done["seq"])
    assert llm.calls["act"] == 3 + 2
    assert c.of_type(sid, "review")[-1]["data"]["limit_reached"] is False
    assert "new-checkout" in c.of_type(sid, "final")[-1]["data"]["text"]


# ---------------------------------------------------------------- approval


async def test_destructive_tool_waits_for_approval(platform):
    store = platform.app.state.admin_server.store
    before = store.services["acme"]["search-api"]["last_restart"]
    c, sid = await platform.session("alice")
    await c.send(sid, "restart search-api")
    await c.wait_status(sid, "awaiting_approval")

    pending = c.of_type(sid, "approval_required")[0]["data"]
    assert [x["name"] for x in pending["calls"]] == ["restart_service"]
    assert store.services["acme"]["search-api"]["last_restart"] == before  # nothing happened yet
    assert not c.of_type(sid, "tool_call")

    await c.send(sid, "list users")  # cannot start another run over a pending decision
    assert (await c.wait_error())["code"] == "conflict"

    await c.approve(sid, True)
    await c.wait_status(sid, "idle")
    assert c.of_type(sid, "tool_result")[0]["data"]["status"] == "ok"
    assert store.services["acme"]["search-api"]["last_restart"] != before
    assert len(audit(platform, "restart_service")) == 1


async def test_declined_approval_never_reaches_the_tool_server(platform):
    c, sid = await platform.session("alice")
    await c.send(sid, "suspend bob")
    await c.wait_status(sid, "awaiting_approval")
    await c.approve(sid, False)
    await c.wait_status(sid, "idle")

    assert c.of_type(sid, "tool_result")[0]["data"]["status"] == "rejected"
    assert audit(platform, "suspend_user") == []
    assert platform.app.state.admin_server.store.users["acme"]["bob"]["status"] == "active"
    assert "declined" in c.of_type(sid, "final")[0]["data"]["text"]


async def test_pending_approval_survives_a_restart(make_platform):
    a = await make_platform()
    c, sid = await a.session("alice")
    await c.send(sid, "rotate key_ci")
    await c.wait_status(sid, "awaiting_approval")
    await a.stop()

    b = await make_platform()  # same database file, fresh process state
    c2 = await b.client("alice")
    await c2.subscribe(sid)
    assert c2.snapshots[sid]["status"] == "awaiting_approval"
    assert c2.snapshots[sid]["pending_approval"]["calls"][0]["name"] == "rotate_api_key"
    await c2.approve(sid, True)
    await c2.wait_status(sid, "idle")
    assert c2.of_type(sid, "tool_result")[0]["data"]["status"] == "ok"


# --------------------------------------------------------- least privilege


async def test_caller_identity_reaches_the_authorization_check(platform):
    """dave may restart the one service he maintains and nothing else."""
    c, sid = await platform.session("dave")
    await c.send(sid, "restart api-gateway")
    await c.wait_status(sid, "awaiting_approval")
    await c.approve(sid, True)
    await c.wait_status(sid, "idle")

    result = c.of_type(sid, "tool_result")[0]["data"]
    assert result["status"] == "denied"
    assert "user:dave lacks service#operate on service:api-gateway" in json.loads(result["content"])["detail"]
    entry = audit(platform, "restart_service")[0]
    assert (entry["actor"], entry["decision"], entry["session"]) == ("dave", "denied", sid)
    assert "permission denied" in c.of_type(sid, "final")[0]["data"]["text"]
    assert c.of_type(sid, "review")[0]["data"]["iterations"] == 2  # reported, not retried

    c2, sid2 = await platform.session("dave")
    await c2.send(sid2, "restart search-api")
    await c2.wait_status(sid2, "awaiting_approval")
    await c2.approve(sid2, True)
    await c2.wait_status(sid2, "idle")
    assert c2.of_type(sid2, "tool_result")[0]["data"]["status"] == "ok"


async def test_agent_is_only_offered_tools_the_caller_may_use(platform):
    c, sid = await platform.session("carol")  # auditor: read-only
    await c.send(sid, "restart search-api")
    await c.wait_status(sid, "idle")

    offered = c.of_type(sid, "tools")[0]["data"]
    assert offered["roles"] == ["auditor"]
    assert "query_audit_log" in offered["allowed"] and "restart_service" not in offered["allowed"]
    assert not c.of_type(sid, "tool_call")
    assert "not among the tools your role permits" in c.text(sid)


async def test_server_side_check_holds_even_if_the_agent_is_offered_everything(make_platform, monkeypatch):
    monkeypatch.setenv("AGENT_PREFILTER_TOOLS", "0")
    p = await make_platform()
    c, sid = await p.session("carol")
    await c.send(sid, "enable new-checkout")
    await c.wait_status(sid, "idle")

    assert c.of_type(sid, "tool_result")[0]["data"]["status"] == "denied"
    assert p.app.state.admin_server.store.flags["acme"]["new-checkout"]["enabled"] is False
    assert audit(p, "set_feature_flag")[0]["decision"] == "denied"


# ------------------------------------------------------------------ resume


async def test_failed_run_resumes_from_its_checkpoint_without_redoing_work(make_platform):
    llm = FailsOnce(fail_on_act_call=3)
    p = await make_platform(llm)
    c, sid = await p.session("alice")
    await c.send(sid, "run a health check")
    failed = await c.wait_status(sid, "error")
    assert "model provider unavailable" in c.of_type(sid, "error")[0]["data"]["message"]

    runtime, sessions = p.app.state.runtime, p.app.state.sessions
    snap = await runtime.snapshot(await sessions.get(sid))
    assert snap["resumable"] and snap["stage"] == "execute" and snap["iterations"] == 2
    assert sum(1 for m in snap["messages"] if m["role"] == "tool") == 3
    assert llm.calls == {"plan": 1, "act": 2}

    await c.resume(sid)
    await c.wait_status(sid, "idle", after=failed["seq"])

    assert c.of_type(sid, "resumed")[0]["data"] == {"from_stage": "execute", "next": ["reason"]}
    assert llm.calls == {"plan": 1, "act": 3}  # only the failed step ran again
    assert len(audit(p, "whoami")) == 1  # intake was not repeated
    assert len(audit(p, "list_services")) == 1  # earlier tool calls were not repeated
    assert "search-api is degraded" in c.of_type(sid, "final")[0]["data"]["text"]
    assert stages_seen(c, sid) == ["intake", "plan", "execute", "review", "finalize"]


async def test_cancelled_run_can_be_resumed(make_platform):
    p = await make_platform(mock_token_delay_ms=2)
    c, sid = await p.session("alice")
    await c.send(sid, "stream 1500")
    await c.wait_for(lambda: len(c.of_type(sid, "token")) > 30)
    await c.cancel(sid)
    stopped = await c.wait_status(sid, "interrupted")
    assert stopped["data"]["resumable"]
    partial = len(c.of_type(sid, "token"))
    assert partial < 1500

    await c.resume(sid)
    await c.wait_status(sid, "idle", after=stopped["seq"])
    assert c.of_type(sid, "final")[0]["data"]["text"].split() == [f"tok{i}" for i in range(1500)]

    await c.resume(sid)  # nothing left to do
    assert (await c.wait_error())["code"] == "conflict"


async def test_interrupted_run_resumes_after_restart_on_the_same_database(make_platform):
    a = await make_platform(mock_token_delay_ms=20)
    c, sid = await a.session("alice")
    await c.send(sid, "run a health check")
    # wait until the third reasoning turn is streaming, i.e. two turns and three tool calls are checkpointed
    await c.wait_for(lambda: len(c.of_type(sid, "tool_result")) == 3 and len(c.of_type(sid, "token")) > 3)
    await a.stop()
    # A graceful stop marks the run interrupted; put back what a hard kill would leave behind.
    import aiosqlite

    async with aiosqlite.connect(a.settings.checkpoint_db) as db:
        await db.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))
        await db.commit()

    llm = CountingLLM()
    b = await make_platform(llm)
    c2 = await b.client("alice")
    await c2.subscribe(sid)
    snap = c2.snapshots[sid]
    assert c2.modes[sid] == ["snapshot"]
    assert (snap["status"], snap["stage"], snap["iterations"], snap["resumable"]) == ("interrupted", "execute", 2, True)
    assert sum(1 for m in snap["messages"] if m["role"] == "tool") == 3 and snap["plan"]

    await c2.resume(sid)
    await c2.wait_status(sid, "idle")
    assert llm.calls == {"plan": 0, "act": 1}  # the new process only finished what was left
    assert not c2.of_type(sid, "tool_call")
    assert "search-api is degraded" in c2.of_type(sid, "final")[0]["data"]["text"]
