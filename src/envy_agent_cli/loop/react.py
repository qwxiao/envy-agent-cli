"""模块2 · ReAct 主循环（编排层）。

**职责（SHOULD DO）**
- 消费模块1 的事件流，把工具调用碎片按 index 攒成完整调用；
- 维护 messages：assistant 的调用意图 + tool 的执行结果，**两笔账都要记**；
- 用 `stop_reason` 驱动循环：`tool_use` → 执行工具继续，`end_turn` → 收尾返回；
- 生成 trace：任务级 → 轮次级 → 调用级，往下传；
- 死循环检测（`no_progress`）：连续多轮调用完全相同 → 提前收尾；
- 输出契约纠错：`validator` 产提示，**Loop 负责再发一次请求**。

**边界（SHOULD NOT）**
- ❌ **不执行工具**：一律交给 `ToolRuntime.execute_all()`（绕过 Runtime = 校验/权限/审计全部失效）；
- ❌ 不碰 HTTP / 厂商 SDK（只依赖 `ChatModel` 契约）；
- ❌ 不 print：渲染通过注入的 `Renderer`（终端要打字机、日志要纯文本、将来要 UI，渲染注定多变）。

**一句话**：编排层是"翻译官"——把模型的意图翻译成工具动作，把工具结果翻译回模型能懂的对话。

关于"参数解析"归谁：碎片拼装与 `json.loads` **同属"把碎片变成可用结构"这一件事**，
所以留在编排层；而**语义校验**（必填项、类型、权限）归 `ToolRuntime` 的七职责。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from envy_agent_cli.llm.adapter import ChatMessage, ChatModel
from envy_agent_cli.llm.events import (
    AnyEvent,
    Error,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from envy_agent_cli.llm.params import ChatParams
from envy_agent_cli.llm.validator import OutputContract, validate_output
from envy_agent_cli.loop.renderer import ConsoleRenderer, Renderer
from envy_agent_cli.tools.runtime import ToolRuntime
from envy_agent_cli.trace import TraceContext, new_trace, round_span, tool_span

#: 循环上限：模型可能死循环调用同一个工具，这是生产级 Agent 的基本护栏
MAX_ROUNDS = 20

#: 连续多少轮"完全相同的工具调用"就判定为原地打转
NO_PROGRESS_LIMIT = 3


@dataclass(slots=True)
class RoundResult:
    """一轮模型调用的结果（由事件流攒出来的）。"""

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage | None = None
    error: Error | None = None


@dataclass(slots=True)
class AgentResult:
    """一次任务的最终产出。"""

    text: str
    messages: list[ChatMessage]
    iterations: int
    truncated: bool = False          # 结果不完整：触顶 / 截断 / 原地打转
    stop_reason: str = "end_turn"
    trace_id: str = ""


def run(
    question: str,
    *,
    adapter: ChatModel,
    runtime: ToolRuntime,
    tool_schemas: list[dict] | None = None,
    render: Renderer | None = None,
    params: ChatParams | None = None,
    max_rounds: int = MAX_ROUNDS,
    no_progress_limit: int = NO_PROGRESS_LIMIT,
    output_contract: OutputContract | None = None,
) -> AgentResult:
    """跑完一次完整的用户任务。

    依赖全部由参数注入（不在模块顶层 new 全局对象）——这样测试可以塞 FakeAdapter / FakeRuntime，
    也让"换模型不改 Loop"在代码层面成立。
    """
    render = render or ConsoleRenderer()
    params = params or ChatParams()
    trace = new_trace()
    messages: list[ChatMessage] = [{"role": "user", "content": question}]

    last_signature: tuple | None = None
    repeated = 0

    for round_no in range(1, max_rounds + 1):
        round_ctx = round_span(trace, round_no)
        events = adapter.stream_chat(messages, tools=tool_schemas, params=params, trace=round_ctx)
        result = _consume_stream(events, render)

        # 流内错误：不把半截 assistant 消息写进历史（会污染后续每一轮）
        if result.error is not None:
            render.notice(f"调用失败：{result.error.code.value} "
                          f"(retryable={result.error.retryable})")
            return AgentResult(text=result.text, messages=messages, iterations=round_no,
                               truncated=True, stop_reason="error", trace_id=trace.trace_id)

        # 模型说的话是历史的一部分（即使它同时要调工具）
        _archive_assistant(messages, result)

        if result.stop_reason == "tool_use":
            signature = _calls_signature(result.tool_calls)
            repeated = repeated + 1 if signature == last_signature else 0
            last_signature = signature
            if repeated + 1 >= no_progress_limit:
                render.notice(f"连续 {repeated + 1} 轮相同的工具调用，判定为原地打转，提前收尾")
                return AgentResult(text=result.text or "任务未完成：连续多轮重复调用同一个工具。",
                                   messages=messages, iterations=round_no,
                                   truncated=True, stop_reason="no_progress",
                                   trace_id=trace.trace_id)

            span = tool_span(round_ctx)
            results = runtime.execute_all(_to_batch(result.tool_calls), span)
            _archive_tool_results(messages, result.tool_calls, results)
            continue

        # 截断：结果不完整，交给调用方决定降级（换模型 / 扩大预算 / 转人工）
        if result.stop_reason == "max_tokens":
            render.notice("输出被 max_tokens 截断，结果不完整")
            return AgentResult(text=result.text, messages=messages, iterations=round_no,
                               truncated=True, stop_reason="max_tokens",
                               trace_id=trace.trace_id)

        # 输出契约纠错：validator 只产提示，**再发一次请求的是 Loop**
        if output_contract is not None:
            outcome = validate_output(result.text, output_contract)
            if not outcome.ok:
                render.notice(f"输出不满足契约「{output_contract.name}」：{outcome.error}，重试")
                messages.append({"role": "user", "content": outcome.correction_prompt})
                continue

        return AgentResult(text=result.text, messages=messages, iterations=round_no,
                           stop_reason=result.stop_reason, trace_id=trace.trace_id)

    render.notice(f"已达轮数上限 {max_rounds}，强制收尾。频繁触顶通常意味着工具描述或提示词有问题")
    return AgentResult(text=_last_assistant_text(messages) or f"任务未完成：已达轮数上限（{max_rounds} 轮）",
                       messages=messages, iterations=max_rounds, truncated=True,
                       stop_reason="max_rounds", trace_id=trace.trace_id)


# ---------------------------------------------------------------- 内部

def _consume_stream(events, render: Renderer) -> RoundResult:
    """消费一轮事件流：边收边渲染，攒齐文本与工具调用。

    只做"攒"，不做"下一步去哪"——那是主循环的事。
    """
    result = RoundResult()
    text_parts: list[str] = []
    slots: dict[int, dict] = {}

    for event in events:
        if isinstance(event, TextDelta):
            render.text(event.text)
            text_parts.append(event.text)
        elif isinstance(event, ReasoningDelta):
            # 思维链只渲染，**不入历史**——推理过程不是对话内容，回传会污染上下文
            render.reasoning(event.text)
        elif isinstance(event, ToolCallDelta):
            _accumulate(slots, event)
        elif isinstance(event, Usage):
            result.usage = event
        elif isinstance(event, MessageEnd):
            result.stop_reason = event.stop_reason
        elif isinstance(event, Error):
            result.error = event

    result.text = "".join(text_parts)
    result.tool_calls = _assemble(slots)
    return result


def _accumulate(slots: dict[int, dict], delta: ToolCallDelta) -> None:
    """把工具调用碎片收进槽位：第一片建槽，后续片只追加 arguments。

    流里**绝不中途 json.loads**——单片不是合法 JSON，拼完整串才解析。
    """
    slot = slots.get(delta.index)
    if slot is None or delta.is_first:
        slot = {"id": delta.call_id, "name": delta.name, "arguments": ""}
        slots[delta.index] = slot
    if delta.call_id:
        slot["id"] = delta.call_id
    if delta.name:
        slot["name"] = delta.name
    if delta.arguments:
        slot["arguments"] += delta.arguments


def _assemble(slots: dict[int, dict]) -> list[dict]:
    """按 index 升序还原成 API 协议的 tool_calls 结构。"""
    return [
        {"id": slots[i]["id"], "type": "function",
         "function": {"name": slots[i]["name"], "arguments": slots[i]["arguments"]}}
        for i in sorted(slots)
    ]


def _archive_assistant(messages: list[ChatMessage], result: RoundResult) -> None:
    """把模型的调用意图按 API 协议格式存档。

    ⚠️ 不能只存工具结果：协议要求 tool 消息必须配对前面 assistant 的调用请求，
    不存这一条，模型下一轮会失忆、重复调用。
    正文为空时 content 置 None（有工具调用的消息不允许空字符串正文）。
    """
    message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
    if result.tool_calls:
        message["tool_calls"] = result.tool_calls
    messages.append(message)


def _archive_tool_results(
    messages: list[ChatMessage],
    calls: list[dict],
    results: list[Any],
) -> None:
    """把工具结果作为 role=tool 的消息回填（用 tool_call_id 对上调用）。

    无论成功失败都回填：`Tool Result 只是新的 observation`，不是终点。
    运行时返回的顺序与入参一一对应，这里按下标配对，避免依赖返回值里的 id 字段。
    """
    for call, result in zip(calls, results):
        messages.append({
            "role": "tool",
            "tool_call_id": call.get("id"),
            "content": getattr(result, "content", str(result)),
        })


def _to_batch(calls: list[dict]) -> list[dict]:
    """把 API 形态的调用转成执行批：`{"id","name","arguments"(dict)}`。

    参数不是合法 JSON 时，**仍然放进批次并显式标上 `parse_error`**：
    这样它能和别的调用一样走到 Runtime、拿到一条结构化错误结果、被记进审计，
    同时 Runtime 不会真的派发 handler。不派发不等于不记账。
    """
    batch: list[dict] = []
    for call in calls:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            batch.append({"id": call.get("id"), "name": function.get("name"),
                          "arguments": None, "parse_error": f"{exc}"})
            continue
        batch.append({"id": call.get("id"), "name": function.get("name"), "arguments": arguments})
    return batch


def _calls_signature(calls: list[dict]) -> tuple:
    """一轮调用的指纹：用于识别"原地打转"。"""
    return tuple((c.get("function", {}).get("name"), c.get("function", {}).get("arguments"))
                 for c in calls)


def _last_assistant_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


__all__ = ["AgentResult", "MAX_ROUNDS", "NO_PROGRESS_LIMIT", "RoundResult", "run"]
