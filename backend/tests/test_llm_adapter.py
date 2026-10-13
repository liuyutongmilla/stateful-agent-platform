"""The LangChain adapter, exercised against a scripted chat model (no network)."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from app.agent.llm import LangChainLLM, to_langchain_messages
from app.agent.tools import ToolSpec

SPEC = ToolSpec("get_user", "Get a user", {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]})


class Scripted(BaseChatModel):
    chunks: list[Any]
    seen: dict[str, Any] = {}

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.seen["tools"] = tools
        return self

    def _generate(self, *a, **k) -> ChatResult:  # pragma: no cover - streaming only
        raise NotImplementedError

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen["messages"] = messages
        for chunk in self.chunks:
            yield ChatGenerationChunk(message=chunk)


async def test_streams_text_and_assembles_tool_calls_from_chunks():
    model = Scripted(
        chunks=[
            AIMessageChunk(content="Looking "),
            AIMessageChunk(content=[{"type": "text", "text": "that up."}]),
            AIMessageChunk(content="", tool_call_chunks=[{"name": "get_user", "args": '{"user_', "id": "tc_1", "index": 0}]),
            AIMessageChunk(content="", tool_call_chunks=[{"name": None, "args": 'id": "bob"}', "id": None, "index": 0}]),
        ]
    )
    tokens: list[str] = []
    result = await LangChainLLM(model).complete("act", "sys", [{"role": "user", "content": "who is bob"}], [SPEC], tokens.append)

    assert tokens == ["Looking ", "that up."]
    assert result.text == "Looking that up."
    assert [(c.id, c.name, c.args) for c in result.tool_calls] == [("tc_1", "get_user", {"user_id": "bob"})]
    assert model.seen["tools"][0]["function"]["name"] == "get_user"
    assert model.seen["tools"][0]["function"]["parameters"] == SPEC.input_schema


async def test_planning_calls_bind_no_tools_and_stream_nothing():
    model = Scripted(chunks=[AIMessageChunk(content='["List users"]')])
    tokens: list[str] = []
    result = await LangChainLLM(model).complete("plan", "sys", [{"role": "user", "content": "hi"}], [SPEC], tokens.append)
    assert result.text == '["List users"]' and tokens == [] and "tools" not in model.seen


def test_history_converts_to_langchain_messages():
    out = to_langchain_messages(
        "sys",
        [
            {"role": "user", "content": "restart it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "restart_service", "args": {"service": "x"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "restart_service", "content": '{"error":"permission_denied"}', "status": "denied"},
        ],
    )
    assert [type(m) for m in out] == [SystemMessage, HumanMessage, AIMessage, ToolMessage]
    assert out[2].tool_calls[0]["id"] == "c1" and out[3].tool_call_id == "c1" and out[3].status == "error"
