"""The agent workflow as a LangGraph state machine.

    intake -> plan -> [ reason <-> act ] -> review -> finalize
                       '--- execute ---'

Five stages; `execute` is a bounded ReAct loop. LangGraph writes a checkpoint
after every node, so an interrupted workflow resumes at the node that had not
finished, with all earlier work (plan, tool results, iteration count) intact.

The loop bound is enforced in graph state, not by a framework recursion
limit: `reason` refuses to invoke the model once `iterations` reaches the
cap, and the run still goes through review/finalize so the user gets a
coherent account of what was and wasn't done.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..auth import CallerContext
from .llm import LLM, LLMResult
from .state import AgentState, current_run_messages
from .tools import ToolGateway, ToolOutcome, ToolSpec

PLAN_SYSTEM = (
    "You are the planning step of an administrative agent. Given the conversation and the tools "
    "available to this user, reply with ONLY a JSON array of 1-5 short imperative steps. "
    "If no tool is needed, reply [\"Answer directly\"]."
)

ACT_SYSTEM = """You are an administrative agent for the organization "{org}", acting on behalf of user "{user}".

Rules:
- Use the provided tools to act; never invent data a tool could return.
- Every tool call is authorized as {user}. If a tool returns permission_denied, do not retry it
  or look for a workaround: tell the user what was denied and stop pursuing that step.
- Destructive tools require the user's approval, which the platform requests for you.
- You have at most {remaining} more tool-using turns in this run. Be economical.
- When the task is done, reply with a concise summary and no tool calls.

Plan for this request:
{plan}"""


def _emit(type_: str, **data: Any) -> None:
    """Send a UI event out through LangGraph's custom stream, stamped at creation time."""
    get_stream_writer()({"type": type_, "data": data, "ts": time.time() * 1000})


def _parse_plan(text: str) -> list[str]:
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            steps = json.loads(text[start : end + 1])
            if isinstance(steps, list):
                return [str(s) for s in steps][:5]
        except json.JSONDecodeError:
            pass
    return [line.strip(" -*0123456789.") for line in text.splitlines() if line.strip()][:5] or ["Answer directly"]


def build_graph(
    llm: LLM,
    tools: ToolGateway,
    checkpointer: BaseCheckpointSaver | None,
    *,
    max_iterations: int = 10,
    prefilter_tools: bool = True,
    require_approval: bool = True,
):
    def caller(state: AgentState, tool: str, call_id: str) -> CallerContext:
        return CallerContext(state["user_id"], state["org_id"], state["session_id"], tool, call_id)

    async def bound_specs(state: AgentState) -> list[ToolSpec]:
        allowed = set(state.get("allowed_tools", []))
        return [s for s in await tools.specs() if s.name in allowed]

    # ------------------------------------------------------------------ 1. intake
    async def intake(state: AgentState) -> dict[str, Any]:
        """Reset per-run state and resolve the caller's tool surface via SpiceDB."""
        _emit("stage", stage="intake")
        who = await tools.call("whoami", {}, caller(state, "whoami", f"{state['run_id']}:whoami"))
        permitted = set(who.data["allowed_tools"]) if who.status == "ok" else set()
        names = [s.name for s in await tools.specs() if s.name in permitted or (not prefilter_tools and who.status == "ok")]
        _emit("tools", allowed=names, roles=who.data["roles"] if who.status == "ok" else [])
        return {
            "stage": "intake",
            "allowed_tools": names,
            "plan": [],
            "iterations": 0,
            "limit_reached": False,
            "review": {},
            "final": "",
        }

    # -------------------------------------------------------------------- 2. plan
    async def plan(state: AgentState) -> dict[str, Any]:
        _emit("stage", stage="plan")
        result = await llm.complete("plan", PLAN_SYSTEM, state["messages"], await bound_specs(state), lambda _t: None)
        steps = _parse_plan(result.text)
        _emit("plan", steps=steps)
        return {"stage": "plan", "plan": steps}

    # ------------------------------------------------------- 3. execute (ReAct)
    async def reason(state: AgentState) -> dict[str, Any]:
        done = state.get("iterations", 0)
        if done >= max_iterations:
            _emit("limit", iterations=done, max_iterations=max_iterations)
            return {"stage": "execute", "limit_reached": True}
        n = done + 1
        msg_id = f"{state['run_id']}:{n}"
        _emit("stage", stage="execute", iteration=n, max_iterations=max_iterations)
        _emit("message_start", id=msg_id)
        system = ACT_SYSTEM.format(
            org=state["org_id"],
            user=state["user_id"],
            remaining=max_iterations - done,
            plan="\n".join(f"{i}. {s}" for i, s in enumerate(state.get("plan", []), 1)) or "(none)",
        )
        result: LLMResult = await llm.complete(
            "act", system, state["messages"], await bound_specs(state), lambda t: _emit("token", id=msg_id, text=t)
        )
        _emit("message_end", id=msg_id, text=result.text, tool_calls=len(result.tool_calls))
        message = {
            "role": "assistant",
            "id": msg_id,
            "content": result.text,
            "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in result.tool_calls],
        }
        return {"stage": "execute", "messages": [message], "iterations": n}

    def after_reason(state: AgentState) -> str:
        if state.get("limit_reached"):
            return "review"
        last = state["messages"][-1]
        return "act" if last["role"] == "assistant" and last.get("tool_calls") else "review"

    async def act(state: AgentState) -> dict[str, Any]:
        calls = state["messages"][-1]["tool_calls"]
        allowed = set(state.get("allowed_tools", []))
        specs = {s.name: s for s in await tools.specs()}

        # Ask for approval BEFORE running anything: a node re-executes from the
        # top when resumed, so no side effect may precede the interrupt.
        risky = [c for c in calls if c["name"] in allowed and specs[c["name"]].destructive]
        approved = True
        if risky and require_approval:
            answer = interrupt({"kind": "approval", "calls": risky})
            approved = bool(isinstance(answer, dict) and answer.get("approved"))

        async def run_one(call: dict[str, Any]) -> dict[str, Any]:
            name = call["name"]
            _emit("tool_call", id=call["id"], name=name, args=call["args"], destructive=name in specs and specs[name].destructive)
            if name not in allowed:
                outcome = ToolOutcome("denied", json.dumps({"error": "permission_denied", "detail": f"{name} is not available to this user"}))
            elif call in risky and not approved:
                outcome = ToolOutcome("rejected", json.dumps({"error": "rejected_by_user", "detail": "the user declined this action"}))
            else:
                # The tool-call id doubles as the idempotency key on the server.
                outcome = await tools.call(name, call["args"], caller(state, name, call["id"]))
            _emit("tool_result", id=call["id"], name=name, status=outcome.status, content=outcome.content)
            return {"role": "tool", "tool_call_id": call["id"], "name": name, "content": outcome.content, "status": outcome.status}

        results = await asyncio.gather(*(run_one(c) for c in calls))
        return {"stage": "execute", "messages": list(results)}

    # ------------------------------------------------------------------ 4. review
    async def review(state: AgentState) -> dict[str, Any]:
        """Deterministic audit of the run: what was attempted, what succeeded, what was blocked."""
        _emit("stage", stage="review")
        outcomes = [m for m in current_run_messages(state["messages"]) if m["role"] == "tool"]
        count = lambda status: sum(1 for m in outcomes if m["status"] == status)  # noqa: E731
        summary = {
            "tool_calls": len(outcomes),
            "ok": count("ok"),
            "denied": count("denied"),
            "rejected": count("rejected"),
            "errors": count("error"),
            "iterations": state.get("iterations", 0),
            "max_iterations": max_iterations,
            "limit_reached": bool(state.get("limit_reached")),
        }
        _emit("review", **summary)
        return {"stage": "review", "review": summary}

    # ---------------------------------------------------------------- 5. finalize
    async def finalize(state: AgentState) -> dict[str, Any]:
        _emit("stage", stage="finalize")
        review_ = state.get("review", {})
        update: dict[str, Any] = {"stage": "done"}
        if state.get("limit_reached"):
            # No extra model call here: the bound on model invocations is strict.
            text = (
                f"I stopped after reaching the limit of {max_iterations} reasoning iterations without finishing. "
                f"In this run I made {review_.get('tool_calls', 0)} tool calls "
                f"({review_.get('ok', 0)} succeeded, {review_.get('denied', 0)} denied, {review_.get('errors', 0)} failed). "
                "Nothing further was attempted. You can narrow the request and try again."
            )
            msg_id = f"{state['run_id']}:final"
            _emit("message_start", id=msg_id)
            _emit("token", id=msg_id, text=text)
            _emit("message_end", id=msg_id, text=text, tool_calls=0)
            update["messages"] = [{"role": "assistant", "id": msg_id, "content": text, "tool_calls": []}]
        else:
            text = state["messages"][-1].get("content") or "(the model returned an empty response)"
        _emit("final", text=text, review=review_)
        update["final"] = text
        return update

    graph = StateGraph(AgentState)
    graph.add_node("intake", intake)
    graph.add_node("plan", plan)
    graph.add_node("reason", reason)
    graph.add_node("act", act)
    graph.add_node("review", review)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "plan")
    graph.add_edge("plan", "reason")
    graph.add_conditional_edges("reason", after_reason, {"act": "act", "review": "review"})
    graph.add_edge("act", "reason")
    graph.add_edge("review", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


# Worst case: intake, plan, max*(reason, act), one refused reason, review, finalize.
def recursion_limit(max_iterations: int) -> int:
    return 2 * max_iterations + 10
