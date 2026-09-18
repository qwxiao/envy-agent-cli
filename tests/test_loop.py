"""模块2 测试：ReAct 主循环。

全部离线——FakeAdapter 按脚本吐事件（含真实的碎片形态），FakeRuntime 记录被调用的东西。
这样能断言"循环做了什么决策"，而不是只能看最终输出对不对。
"""

from dataclasses import dataclass, field

import pytest

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
from envy_agent_cli.llm.validator import OutputContract
from envy_agent_cli.loop.react import MAX_ROUNDS, NO_PROGRESS_LIMIT, run
from envy_agent_cli.loop.renderer import NullRenderer
from envy_agent_cli.tools.result import ToolResult


class RecordingRenderer(NullRenderer):
    """记录渲染内容的渲染器：编排层"说了什么"要能被断言，而不是抓 stdout。"""

    def __init__(self):
        self.texts: list[str] = []
        self.reasonings: list[str] = []
        self.notices: list[str] = []

    def text(self, chunk: str) -> None:
        self.texts.append(chunk)

    def reasoning(self, chunk: str) -> None:
        self.reasonings.append(chunk)

    def notice(self, message: str) -> None:
        self.notices.append(message)


class FakeAdapter:
    """按脚本吐事件的假适配器。每个脚本项是"一轮的事件列表"。"""

    name = "fake"

    def __init__(self, rounds: list[list]) -> None:
        self.rounds = rounds
        self.calls: list[dict] = []

    def stream_chat(self, messages, tools=None, params=ChatParams(), trace=None):
        self.calls.append({"messages": list(messages), "tools": tools, "params": params, "trace": trace})
        events = self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)]
        yield from events


@dataclass
class FakeRuntime:
    """假的执行器：记录收到的调用与 trace，按脚本返回结果。"""

    results: list[str] = field(default_factory=lambda: ["ok"])
    calls: list[list] = field(default_factory=list)
    traces: list = field(default_factory=list)

    def execute_all(self, calls, trace):
        self.calls.append(calls)
        self.traces.append(trace)
        out = []
        for i, call in enumerate(calls):
            if call.get("parse_error"):
                out.append(ToolResult(content=f"参数不是合法 JSON：{call['parse_error']}",
                                      is_error=True, tool_call_id=call["id"]))
            else:
                content = self.results[i % len(self.results)]
                out.append(ToolResult(content=content, tool_call_id=call["id"]))
        return out


# ---------------------------------------------------------------- 事件构造

def text(chunk: str) -> TextDelta:
    return TextDelta(chunk)


def end(reason: str = "end_turn") -> MessageEnd:
    return MessageEnd(reason)


def call_deltas(index: int, name: str, arguments: str, call_id: str | None = None) -> list:
    """按真实厂商的形态产出碎片：第一片带 id/name，后续片只有 arguments。"""
    return [
        ToolCallDelta(index=index, is_first=True, call_id=call_id or f"call_{index}", name=name,
                      arguments=arguments[:4]),
        ToolCallDelta(index=index, is_first=False, arguments=arguments[4:]),
    ]


def bare_question_result(**kwargs):
    """跑一次任务，返回 (结果, 渲染器, 适配器, 执行器)。"""
    adapter = kwargs.pop("adapter")
    runtime = kwargs.pop("runtime", FakeRuntime())
    renderer = kwargs.pop("render", RecordingRenderer())
    return run("帮我看看", adapter=adapter, runtime=runtime, render=renderer, **kwargs), \
        renderer, adapter, runtime


# ---------------------------------------------------------------- 主流程

def test_single_round_path():
    adapter = FakeAdapter([[text("你好"), end()]])
    result, renderer, _, runtime = bare_question_result(adapter=adapter)

    assert result.text == "你好" and result.iterations == 1
    assert result.truncated is False and result.stop_reason == "end_turn"
    assert [m["role"] for m in result.messages] == ["user", "assistant"]
    assert runtime.calls == [], "没有工具调用就不该碰执行器"


def test_tool_round_then_answer():
    """一轮调工具 + 一轮收尾：消息配对必须完整（协议要求 tool 紧跟其 assistant）。"""
    adapter = FakeAdapter([
        [text("我先看目录。"), *call_deltas(0, "list_dir", '{"path": "."}'), end("tool_use")],
        [text("目录里有 a.txt。"), end()],
    ])
    result, _, _, runtime = bare_question_result(adapter=adapter)

    assert result.text == "目录里有 a.txt。" and result.iterations == 2
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.messages[1]["content"] == "我先看目录。"
    assert result.messages[1]["tool_calls"][0]["function"]["name"] == "list_dir"
    assert result.messages[2]["tool_call_id"] == "call_0"

    # 执行走的是 Runtime，且拿到的是解析后的 dict 参数
    assert runtime.calls == [[{"id": "call_0", "name": "list_dir", "arguments": {"path": "."}}]]


def test_arguments_are_assembled_from_fragments_before_execution():
    """碎片要拼成完整 JSON 才解析——流里绝不中途 json.loads。"""
    adapter = FakeAdapter([
        [*call_deltas(0, "read_file", '{"path": "a.txt"}'), end("tool_use")],
        [text("读完了"), end()],
    ])
    _, _, _, runtime = bare_question_result(adapter=adapter)

    assert runtime.calls[0][0]["arguments"] == {"path": "a.txt"}


def test_multiple_tool_calls_in_one_round_are_all_executed():
    adapter = FakeAdapter([
        [*call_deltas(0, "list_dir", "{}"), *call_deltas(1, "read_file", '{"path": "a.txt"}'),
         end("tool_use")],
        [text("done"), end()],
    ])
    result, _, _, runtime = bare_question_result(adapter=adapter)

    assert [c["name"] for c in runtime.calls[0]] == ["list_dir", "read_file"]
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "tool", "assistant"]


def test_tool_error_result_is_fed_back_not_raised():
    """工具失败也是 observation：错误文本回灌给模型，循环继续。"""
    adapter = FakeAdapter([
        [*call_deltas(0, "write_file", '{"path": "a.txt"}'), end("tool_use")],
        [text("写不了，我换个办法。"), end()],
    ])
    runtime = FakeRuntime(results=["权限不足"])
    result, _, _, _ = bare_question_result(adapter=adapter, runtime=runtime)

    assert result.messages[2]["content"] == "权限不足"
    assert result.iterations == 2


def test_malformed_arguments_are_dispatched_as_marked_entry():
    """参数不是合法 JSON：仍进批次（要记账、要让模型看到），但带 parse_error 标记。"""
    adapter = FakeAdapter([
        [*call_deltas(0, "read_file", "{这不是 JSON"), end("tool_use")],
        [text("我改一下调用方式。"), end()],
    ])
    result, _, _, runtime = bare_question_result(adapter=adapter)

    entry = runtime.calls[0][0]
    assert entry["arguments"] is None and entry["parse_error"]
    assert "不是合法 JSON" in result.messages[2]["content"]


# ---------------------------------------------------------------- 终止条件

def test_max_rounds_reached_is_graceful():
    """触顶不抛异常：标记 truncated，把已有内容作为结论返回。

    每轮调用**故意不同**——否则会先被 no_progress 拦下，测不到触顶这条路径。
    """
    rounds = [[*call_deltas(0, "list_dir", f'{{"path": "{i}"}}'), end("tool_use")]
              for i in range(MAX_ROUNDS + 1)]
    adapter = FakeAdapter(rounds)
    result, renderer, _, _ = bare_question_result(adapter=adapter, max_rounds=3)

    assert result.truncated is True
    assert result.stop_reason == "max_rounds"
    assert result.iterations == 3
    assert any("轮数上限" in n for n in renderer.notices)


def test_no_progress_detection_stops_early():
    """连续多轮完全相同的调用 = 原地打转，提前收尾（不必等到轮数上限）。"""
    same_round = [*call_deltas(0, "list_dir", '{"path": "."}'), end("tool_use")]
    adapter = FakeAdapter([same_round] * (NO_PROGRESS_LIMIT + 2))
    result, renderer, _, _ = bare_question_result(adapter=adapter)

    assert result.stop_reason == "no_progress"
    assert result.truncated is True
    assert result.iterations == NO_PROGRESS_LIMIT
    assert result.iterations < MAX_ROUNDS, "应当在触顶之前就被拦下"
    assert any("原地打转" in n for n in renderer.notices)


def test_different_calls_do_not_trigger_no_progress():
    """每轮调用不同就不算打转——检测的是"完全相同"。"""
    adapter = FakeAdapter([
        [*call_deltas(0, "list_dir", '{"path": "a"}'), end("tool_use")],
        [*call_deltas(0, "list_dir", '{"path": "b"}'), end("tool_use")],
        [*call_deltas(0, "list_dir", '{"path": "c"}'), end("tool_use")],
        [text("ok"), end()],
    ])
    result, _, _, _ = bare_question_result(adapter=adapter)

    assert result.stop_reason == "end_turn" and result.iterations == 4


def test_stream_error_ends_task_without_polluting_history():
    """流内错误：不把半截 assistant 消息写进历史，否则后续每一轮都被污染。"""
    adapter = FakeAdapter([[text("说到一半"), Error("连接断开", LLMErrorCode.SERVER_ERROR, True)]])
    result, renderer, _, _ = bare_question_result(adapter=adapter)

    assert result.stop_reason == "error" and result.truncated is True
    assert [m["role"] for m in result.messages] == ["user"]
    assert any("server_error" in n for n in renderer.notices)


def test_max_tokens_is_treated_as_incomplete():
    adapter = FakeAdapter([[text("被截断的半句"), end("max_tokens")]])
    result, renderer, _, _ = bare_question_result(adapter=adapter)

    assert result.truncated is True and result.stop_reason == "max_tokens"
    assert any("截断" in n for n in renderer.notices)


# ---------------------------------------------------------------- 上下文与追踪

def test_reasoning_is_rendered_but_not_archived():
    """思维链只在终端显示，不进 messages——推理过程不是对话内容。"""
    adapter = FakeAdapter([[ReasoningDelta("让我想想……"), text("答案是 42"), end()]])
    result, renderer, _, _ = bare_question_result(adapter=adapter)

    assert renderer.reasonings == ["让我想想……"]
    assert result.messages[1]["content"] == "答案是 42"
    assert "让我想想" not in str(result.messages)


def test_trace_is_generated_and_passed_down():
    adapter = FakeAdapter([
        [*call_deltas(0, "list_dir", "{}"), end("tool_use")],
        [text("ok"), end()],
    ])
    result, _, _, runtime = bare_question_result(adapter=adapter)

    task_trace = adapter.calls[0]["trace"]
    assert task_trace.trace_id == result.trace_id and task_trace.trace_id.startswith("trace-")

    round_trace = adapter.calls[1]["trace"]
    assert round_trace.span_id == f"{result.trace_id}:r2"

    tool_trace = runtime.traces[0]
    assert tool_trace.trace_id == result.trace_id
    assert tool_trace.parent_span_id == f"{result.trace_id}:r1"    # 父级是它所属的轮次


def test_usage_and_params_flow_through():
    adapter = FakeAdapter([[text("hi"), Usage(10, 5, 15), end()]])
    bare_question_result(adapter=adapter, params=ChatParams(temperature=0.1), tool_schemas=[{"x": 1}])

    assert adapter.calls[0]["params"].temperature == 0.1
    assert adapter.calls[0]["tools"] == [{"x": 1}]


# ---------------------------------------------------------------- 输出契约纠错

def test_invalid_output_triggers_one_correction_round():
    """validator 只产提示，**再发一次请求的是 Loop**。"""
    contract = OutputContract("answer", {"type": "object", "required": ["summary"]})
    adapter = FakeAdapter([
        [text("我随便说说"), end()],                      # 不合契约
        [text('{"summary": "合规了"}'), end()],            # 补一次就合规
    ])
    result, renderer, _, _ = bare_question_result(adapter=adapter, output_contract=contract)

    assert result.text == '{"summary": "合规了"}' and result.iterations == 2
    correction = result.messages[2]
    assert correction["role"] == "user" and "answer" in correction["content"]
    assert any("重试" in n for n in renderer.notices)


def test_valid_output_does_not_trigger_correction():
    contract = OutputContract("answer", {"type": "object", "required": ["summary"]})
    adapter = FakeAdapter([[text('{"summary": "ok"}'), end()]])
    result, _, _, _ = bare_question_result(adapter=adapter, output_contract=contract)

    assert result.iterations == 1
    assert [m["role"] for m in result.messages] == ["user", "assistant"]


# ---------------------------------------------------------------- 边界守卫

def test_loop_does_not_print():
    """渲染必须走注入的 Renderer——编排层里出现 print，就等于把展示方式焊进业务逻辑。"""
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "src" / "envy_agent_cli" / "loop" / "react.py"
    code_lines = [ln for ln in source.read_text(encoding="utf8").splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    # 允许文档字符串里出现 print 这个词，但不允许真的调用
    assert not [ln for ln in code_lines if "print(" in ln]


def test_renderer_protocol_is_satisfied_by_default_implementations():
    from envy_agent_cli.loop.renderer import ConsoleRenderer, NullRenderer, Renderer

    for impl in (ConsoleRenderer(show_reasoning=False), NullRenderer()):
        assert isinstance(impl, Renderer)


@pytest.mark.parametrize("limit", [1, 2])
def test_no_progress_limit_is_configurable(limit):
    same_round = [*call_deltas(0, "list_dir", "{}"), end("tool_use")]
    adapter = FakeAdapter([same_round] * 5)
    result, _, _, _ = bare_question_result(adapter=adapter, no_progress_limit=limit)

    assert any(result.stop_reason == expected
               for expected in ("no_progress", "max_rounds"))
