"""Adapter 测试：厂商差异归一化 + 错误码判定。

全部离线——通过 `chunk_source` 注入口喂伪造的 SSE 文本，
所以不需要真 API Key、不联网，也不会因为厂商抖动导致测试不稳定。
"""

import json

import httpx
import pytest

from envy_agent_cli.llm.adapter import (
    ChatModel,
    OpenAICompatAdapter,
    StreamOpenError,
    map_finish_reason,
)
from envy_agent_cli.llm.events import (
    Error,
    LLMErrorCode,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from envy_agent_cli.llm.params import ChatParams


def sse(*chunks: dict) -> str:
    """把若干 chunk 拼成一段 SSE 文本。"""
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def adapter_with(payload: str, *, split: int = 0) -> OpenAICompatAdapter:
    """构造一个走注入通道的适配器（可选把载荷切碎，模拟跨 chunk 到达）。"""

    def chunk_source(body: dict):
        if split:
            yield from (payload[i:i + split] for i in range(0, len(payload), split))
        else:
            yield payload

    return OpenAICompatAdapter("deepseek", api_key="test", chunk_source=chunk_source)


def test_satisfies_chat_model_contract():
    assert isinstance(adapter_with(""), ChatModel)


def test_text_delta_from_content():
    a = adapter_with(sse({"choices": [{"delta": {"content": "你"}}]}))
    assert list(a.stream_chat([])) == [TextDelta("你")]


def test_reasoning_delta_is_separate_from_text():
    """思维链和正文是两种语义，必须分开成两种事件。"""
    payload = sse({"choices": [{"delta": {"reasoning_content": "先看需求"}}]},
                  {"choices": [{"delta": {"content": "答案是 42"}}]})
    events = list(adapter_with(payload).stream_chat([]))
    assert events == [ReasoningDelta("先看需求"), TextDelta("答案是 42")]


def test_tool_call_fragments_marked_first_and_accumulate():
    """一次工具调用拆成多片到达：第一片带 id/name，后续片只有 arguments 碎片。"""
    payload = sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "th\": \"a.txt\"}"}}]}}]},
    )
    events = [e for e in adapter_with(payload).stream_chat([]) if isinstance(e, ToolCallDelta)]

    assert [e.is_first for e in events] == [True, False]
    assert events[0].name == "read_file" and events[0].call_id == "call_1"
    # 本层只搬运碎片，**不拼装**——拼装是编排层的事
    assert "".join(e.arguments for e in events) == '{"path": "a.txt"}'


def test_two_concurrent_tool_calls_tracked_by_index():
    payload = sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "read_file", "arguments": "{"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "c1", "function": {"name": "list_dir", "arguments": "{"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "}"}}]}}]},
    )
    events = [e for e in adapter_with(payload).stream_chat([]) if isinstance(e, ToolCallDelta)]
    assert [(e.index, e.is_first, e.name) for e in events] == [
        (0, True, "read_file"), (1, True, "list_dir"), (0, False, None),
    ]


def test_usage_chunk_with_empty_choices():
    """usage 块的 `choices` 是空数组——不做防御取值就会漏掉真实用量。"""
    payload = sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                            "total_tokens": 15}})
    assert Usage(10, 5, 15) in list(adapter_with(payload).stream_chat([]))


def test_finish_reason_maps_to_stop_reason():
    assert map_finish_reason("tool_calls") == "tool_use"
    assert map_finish_reason("length") == "max_tokens"      # 截断是失败信号
    assert map_finish_reason("content_filter") == "stop_sequence"
    assert map_finish_reason("stop") == "end_turn"

    payload = sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    assert MessageEnd("tool_use") in list(adapter_with(payload).stream_chat([]))


def test_request_body_carries_params_tools_and_usage_option():
    a = OpenAICompatAdapter("deepseek", api_key="k", model="m")
    body = a._request_body([{"role": "user", "content": "hi"}],
                           [{"type": "function", "function": {"name": "read_file"}}],
                           ChatParams(temperature=0.1, max_tokens=64))
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.1 and body["max_tokens"] == 64
    assert body["tool_choice"] == "auto" and len(body["tools"]) == 1


def test_http_error_becomes_structured_error_event():
    """429 必须变成"可重试"的结构化错误，而不是让 Loop 去猜字符串里有没有 429。"""

    def chunk_source(body: dict):
        raise StreamOpenError(LLMErrorCode.RATE_LIMITED, "HTTP 429: too many requests", 429)
        yield  # pragma: no cover

    a = OpenAICompatAdapter("deepseek", api_key="k", chunk_source=chunk_source)
    (event,) = list(a.stream_chat([]))
    assert isinstance(event, Error)
    assert event.code is LLMErrorCode.RATE_LIMITED
    assert event.retryable is True and event.status_code == 429


def test_network_failure_becomes_retryable_server_error():
    def chunk_source(body: dict):
        raise httpx.ConnectError("connection reset")
        yield  # pragma: no cover

    a = OpenAICompatAdapter("deepseek", api_key="k", chunk_source=chunk_source)
    (event,) = list(a.stream_chat([]))
    assert event.code is LLMErrorCode.SERVER_ERROR and event.retryable is True


def test_auth_failure_is_not_retryable():
    err = Error.from_code(LLMErrorCode.AUTH_FAILED, "401")
    assert err.retryable is False


@pytest.mark.parametrize("size", [1, 4, 13])
def test_stream_survives_chunk_boundaries(size):
    payload = sse({"choices": [{"delta": {"content": "ab"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]})
    events = list(adapter_with(payload, split=size).stream_chat([]))
    assert events == [TextDelta("ab"), MessageEnd("end_turn")]


def test_provider_defaults_are_applied():
    a = OpenAICompatAdapter("openai", api_key="k")
    assert a.base_url.startswith("https://api.openai.com") and a.model
    assert a.name == "openai"
