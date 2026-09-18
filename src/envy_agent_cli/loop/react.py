"""模块2 · ReAct 主循环（编排层）。

**职责（SHOULD DO）**
- 消费模块1 的事件流，把工具调用碎片按 index 攒成完整调用；
- 维护 messages：assistant 的调用意图 + tool 的执行结果，**两笔账都要记**；
- 用 `stop_reason` 驱动循环：`tool_use` → 执行工具继续，`end_turn` → 收尾返回；
- 生成 trace：任务级 → 轮次级 → 调用级，往下传；
- 预算与护栏：四重预算（轮数 / token / 工具调用数 / 时长）+ 打转检测 + 纠错预算；
- 编排层事件落审计（推理摘要、异常轮次的调用计数、终止原因）。

**边界（SHOULD NOT）**
- ❌ **不执行工具**：一律交给 `ToolRuntime.execute_all()`（绕过 Runtime = 校验/权限/审计全部失效）；
- ❌ 不碰 HTTP / 厂商 SDK（只依赖 `ChatModel` 契约）；
- ❌ 不 print：渲染通过注入的 `Renderer`（终端要打字机、日志要纯文本、将来要 UI，渲染注定多变）。

**一句话**：编排层是"翻译官"——把模型的意图翻译成工具动作，把工具结果翻译回模型能懂的对话。

关于"参数解析"归谁：碎片拼装与 `json.loads` **同属"把碎片变成可用结构"这一件事**，
所以留在编排层；而**语义校验**（必填项、类型、权限）归 `ToolRuntime` 的七职责。
Runtime 的输入契约是"结构化的调用"，**字符串不是它的语言**。
"""

import json
import time
from dataclasses import asdict, dataclass, field

from envy_agent_cli.audit.logger import AuditLogger
from envy_agent_cli.context.compactor import (
    ContextEvent,
    ContextPolicy,
    NoopContextPolicy,
    to_context_event,
)
from envy_agent_cli.llm.adapter import ChatMessage, ChatModel
from envy_agent_cli.llm.events import (
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
from envy_agent_cli.trace import new_trace, round_span, tool_span

#: 终止原因。`budget_exhausted` 用 `AgentResult.detail` 说明耗尽了哪一种预算。
STOP_END_TURN = "end_turn"
STOP_MAX_ROUNDS = "max_rounds"
STOP_BUDGET = "budget_exhausted"
STOP_NO_PROGRESS = "no_progress"
STOP_MAX_TOKENS = "max_tokens"
STOP_CONTRACT_FAILED = "output_contract_failed"
STOP_ERROR = "error"


@dataclass(frozen=True, slots=True)
class Budget:
    """四重预算：单一轮数上限拦不住"20 轮 × 每轮 10 个调用"。

    优先级：token（成本）> 轮数 > 工具调用数 > 时长。
    """

    max_rounds: int = 20
    max_tool_calls: int = 50
    max_tokens: int = 200_000
    max_duration_s: float = 300.0


@dataclass(frozen=True, slots=True)
class NoProgressPolicy:
    """打转检测策略（两级指纹）。

    - 一级指纹 `(工具名, 参数)`：连续 `warn_after` 轮相同 → **先提示**，给模型换策略的机会；
    - 二级指纹 `(工具名, 参数, 结果前若干字)`：连续 `stop_after` 轮相同 → 确认打转，终止。

    只看调用会误判（外部状态变了、重试有意义）；只看结果也会误判（不同调用返回同样的错误）。
    **打转也是反馈，先提示再终止**——直接终止等于剥夺模型的自我修正机会。
    """

    warn_after: int = 2
    stop_after: int = 3
    result_prefix: int = 100


@dataclass(slots=True)
class RoundResult:
    """一轮模型调用的结果（由事件流攒出来的）。"""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = STOP_END_TURN
    usage: Usage | None = None
    error: Error | None = None


@dataclass(slots=True)
class AgentResult:
    """一次任务的最终产出。"""

    text: str
    messages: list[ChatMessage]
    iterations: int
    truncated: bool = False               # 结果不完整：触顶 / 截断 / 打转 / 预算耗尽 / 契约未满足
    stop_reason: str = STOP_END_TURN
    trace_id: str = ""
    detail: str | None = None             # 终止补充说明（哪种预算耗尽、打转指纹、最后校验错误）
    usage: Usage | None = None            # 累计 token（真实值，来自服务端 usage）
    tool_calls_total: int = 0             # 累计工具调用数
    context_events: list[ContextEvent] = field(default_factory=list)  # 每次压缩的记录（只交事件，不交快照）


def run(
    question: str,
    *,
    adapter: ChatModel,
    runtime: ToolRuntime,
    tool_schemas: list[dict] | None = None,
    render: Renderer | None = None,
    params: ChatParams | None = None,
    system_prompt: str | None = None,
    budget: Budget | None = None,
    no_progress: NoProgressPolicy | None = None,
    max_corrections: int = 2,
    output_contract: OutputContract | None = None,
    audit: AuditLogger | None = None,
    audit_reasoning: bool = True,
    context_policy: ContextPolicy | None = None,
) -> AgentResult:
    """跑完一次完整的用户任务。

    依赖全部由参数注入（不在模块顶层 new 全局对象）——这样测试可以塞 FakeAdapter / FakeRuntime，
    也让"换模型不改 Loop"在代码层面成立。

    Args:
        system_prompt: 可选的系统提示。**注入防护第 1 级的"声明"那一半就落在这里**——
            工具结果已经被 Runtime 包进 `<tool_result>`，系统提示负责声明其中的内容不可信。
        context_policy: 可选的上下文策略，每轮发请求前调用一次。**"不压缩"也是一个策略**
            （`NoopContextPolicy`），不是"没实现"——所以默认值用 `None` 哨兵而不是直接 new，
            避免默认参数在定义时求值、被所有调用共享同一个实例。
    """
    render = render or ConsoleRenderer()
    params = params or ChatParams()
    budget = budget or Budget()
    no_progress = no_progress or NoProgressPolicy()
    policy = context_policy or NoopContextPolicy()

    trace = new_trace()
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": question})
    started = time.monotonic()

    tokens_used = 0
    tool_calls_total = 0
    corrections = 0
    prev_signature: tuple | None = None
    prev_full: tuple | None = None
    repeat_call = repeat_full = 0
    round_no = 0
    context_events: list[ContextEvent] = []

    def finish(text: str, stop_reason: str, truncated: bool = False,
               detail: str | None = None) -> AgentResult:
        if audit is not None:
            audit.event("stop", round_span(trace, max(round_no, 1)),
                        stop_reason=stop_reason, truncated=truncated, detail=detail,
                        iterations=round_no, tokens=tokens_used,
                        tool_calls=tool_calls_total)
        return AgentResult(text=text, messages=messages, iterations=round_no,
                           truncated=truncated, stop_reason=stop_reason, trace_id=trace.trace_id,
                           detail=detail,
                           usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=tokens_used),
                           tool_calls_total=tool_calls_total,
                           context_events=context_events)

    for round_no in range(1, budget.max_rounds + 1):
        round_ctx = round_span(trace, round_no)

        # —— 预算前置检查（时长 / token）：不烧下一轮 ——
        elapsed = time.monotonic() - started
        if elapsed > budget.max_duration_s:
            return finish(_last_assistant_text(messages), STOP_BUDGET, True,
                          f"时长预算耗尽（{elapsed:.0f}s > {budget.max_duration_s:.0f}s）")
        if tokens_used >= budget.max_tokens:
            return finish(_last_assistant_text(messages), STOP_BUDGET, True,
                          f"token 预算耗尽（{tokens_used} >= {budget.max_tokens}）")

        # —— 上下文压缩：每轮发请求前一次，且只有这一处。
        # 压缩后 messages 重新绑定：Loop 手里的是"模型看到的"，不是"客观发生的"——
        # 被压掉的原文就此离开主链路，它的去向记录在 context_events 与审计里。
        prepared = policy.prepare(messages, tool_definitions=tool_schemas, trace=round_ctx)
        if prepared.failure and audit is not None:
            audit.event("compress_failed", round_ctx, engine=prepared.summary_engine,
                        error=prepared.failure, triggered_by=prepared.triggered_by)
        if prepared.compressed:
            messages = list(prepared.messages)
            event = to_context_event(prepared, round_ctx.span_id)
            context_events.append(event)
            if audit is not None:
                audit.event("compact", round_ctx,
                            **{k: v for k, v in asdict(event).items() if k != "span_id"})

        events = adapter.stream_chat(messages, tools=tool_schemas, params=params, trace=round_ctx)
        result = _consume_stream(events, render)

        if result.usage is not None:
            tokens_used += result.usage.total_tokens

        if audit is not None and audit_reasoning and result.reasoning:
            audit.event("reasoning", round_ctx, chars=len(result.reasoning), text=result.reasoning)

        # —— 流内错误：不把半截 assistant 消息写进历史，但审计要保住信息 ——
        if result.error is not None:
            if audit is not None:
                complete, partial = _classify_calls(result.tool_calls)
                audit.event("stream_error", round_ctx,
                            planned_calls=len(result.tool_calls),
                            complete_calls=complete, partial_calls=partial,
                            error_code=result.error.code.value,
                            retryable=result.error.retryable)
            render.notice(f"调用失败：{result.error.code.value} "
                          f"(retryable={result.error.retryable})")
            return finish(result.text, STOP_ERROR, True,
                          f"流内错误 {result.error.code.value}；本轮中断前已产出 "
                          f"{len(result.tool_calls)} 个调用（未入历史）")

        _archive_assistant(messages, result)

        if result.stop_reason == "tool_use":
            calls = result.tool_calls
            signature = _calls_signature(calls)

            span = tool_span(round_ctx)
            batch = _to_batch(calls, start_seq=tool_calls_total)
            results = runtime.execute_all(batch, span)
            tool_calls_total += len(calls)
            _archive_tool_results(messages, calls, results)

            repeat_call = repeat_call + 1 if signature == prev_signature else 1
            prev_signature = signature
            full = (signature, _results_fingerprint(results, no_progress.result_prefix))
            repeat_full = repeat_full + 1 if full == prev_full else 1
            prev_full = full

            if repeat_full >= no_progress.stop_after:
                render.notice(f"检测到连续 {repeat_full} 轮完全相同的调用与返回，判定为原地打转")
                return finish(result.text or "任务未完成：连续多轮重复调用同一个工具。",
                              STOP_NO_PROGRESS, True,
                              f"重复 {repeat_full} 轮；指纹={_short_fingerprint(signature)}；"
                              f"起始轮次=r{round_no - repeat_full + 1}")
            # 只在**跨过阈值那一轮**提示一次（`==` 而不是 `>=`）：
            # 一轮一条提示会把对话塞满同样的内容，模型反而更容易忽略
            if repeat_call == no_progress.warn_after:
                render.notice("检测到重复调用，已提示模型换策略")
                messages.append({"role": "user", "content": NO_PROGRESS_HINT})

            # 工具调用数预算：先执行再判（保证 tool 消息与 assistant 调用成对，历史不残缺）
            if tool_calls_total >= budget.max_tool_calls:
                return finish(result.text, STOP_BUDGET, True,
                              f"工具调用数预算耗尽（{tool_calls_total} >= {budget.max_tool_calls}）")
            continue

        if result.stop_reason == "max_tokens":
            render.notice("输出被 max_tokens 截断，结果不完整")
            return finish(result.text, STOP_MAX_TOKENS, True,
                          "输出被 max_tokens 截断（推理型模型的思维链也计入该预算）")

        # —— 输出契约纠错：validator 只产提示，**再发一次请求的是 Loop**，且预算独立 ——
        if output_contract is not None:
            outcome = validate_output(result.text, output_contract)
            if not outcome.ok:
                corrections += 1
                if corrections > max_corrections:
                    render.notice(f"纠错预算耗尽（{max_corrections} 次），输出仍不满足契约")
                    return finish(result.text, STOP_CONTRACT_FAILED, True,
                                  f"纠错 {max_corrections} 次仍不满足契约「{output_contract.name}」；"
                                  f"最后一次校验错误：{outcome.error}")
                render.notice(f"输出不满足契约「{output_contract.name}」：{outcome.error}，重试")
                messages.append({"role": "user", "content": outcome.correction_prompt})
                continue

        return finish(result.text, result.stop_reason)

    return finish(_last_assistant_text(messages) or f"任务未完成：已达轮数上限（{budget.max_rounds} 轮）",
                  STOP_MAX_ROUNDS, True,
                  f"轮数上限 {budget.max_rounds}；频繁触顶通常意味着工具描述或提示词有问题")


#: 打转提示：注入给模型的一条"用户反馈"，不是系统指令——保持它能被当作对话内容处理
NO_PROGRESS_HINT = "你正在重复上一次的工具调用且结果没有变化，请换一个策略或换一个工具。"


# ---------------------------------------------------------------- 内部

def _consume_stream(events, render: Renderer) -> RoundResult:
    """消费一轮事件流：边收边渲染，攒齐文本、推理与工具调用。

    只做"攒"，不做"下一步去哪"——那是主循环的事。
    """
    result = RoundResult()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    slots: dict[int, dict] = {}

    for event in events:
        if isinstance(event, TextDelta):
            render.text(event.text)
            text_parts.append(event.text)
        elif isinstance(event, ReasoningDelta):
            # 思维链只渲染，**不入历史**——推理过程不是对话内容，回传会污染上下文
            render.reasoning(event.text)
            reasoning_parts.append(event.text)
        elif isinstance(event, ToolCallDelta):
            _accumulate(slots, event)
        elif isinstance(event, Usage):
            result.usage = event
        elif isinstance(event, MessageEnd):
            result.stop_reason = event.stop_reason
        elif isinstance(event, Error):
            result.error = event

    result.text = "".join(text_parts)
    result.reasoning = "".join(reasoning_parts)
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
    message: dict = {"role": "assistant", "content": result.text or None}
    if result.tool_calls:
        message["tool_calls"] = result.tool_calls
    messages.append(message)


def _archive_tool_results(messages: list[ChatMessage], calls: list[dict], results: list) -> None:
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


def _to_batch(calls: list[dict], start_seq: int = 0) -> list[dict]:
    """把 API 形态的调用转成执行批：`{"seq","id","name","arguments"(dict)}`。

    `seq` 是全局调用序号（从 `start_seq` 起），便于 Runtime 生成调用级 span 时排序与追溯。

    参数不是合法 JSON 时，**仍然放进批次并显式标上 `parse_error`**：
    这样它能和别的调用一样走到 Runtime、拿到一条结构化错误结果、被记进审计，
    同时 Runtime 不会真的派发 handler。不派发不等于不记账。
    """
    batch: list[dict] = []
    for offset, call in enumerate(calls):
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        entry = {"seq": start_seq + offset, "id": call.get("id"), "name": function.get("name")}
        try:
            entry["arguments"] = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            entry["arguments"] = None
            entry["parse_error"] = str(exc)
        batch.append(entry)
    return batch


def _calls_signature(calls: list[dict]) -> tuple:
    """一级指纹：只看调用（工具名 + 参数原文）。"""
    return tuple((c.get("function", {}).get("name"), c.get("function", {}).get("arguments"))
                 for c in calls)


def _results_fingerprint(results: list, prefix: int) -> tuple:
    """二级指纹的一部分：结果的稳定摘要（截断到前缀长度）。

    只看结果会误判（不同调用可能返回同样的错误），所以它和一级指纹**组合**使用。
    """
    return tuple(str(getattr(r, "content", r))[:prefix] for r in results)


def _short_fingerprint(signature: tuple, limit: int = 120) -> str:
    text = json.dumps(signature, ensure_ascii=False)
    return text[:limit] + ("…" if len(text) > limit else "")


def _classify_calls(calls: list[dict]) -> tuple[int, int]:
    """把一轮的调用分成"完整"和"残缺"——审计要能回答"有几个是半截的"。"""
    complete = partial = 0
    for call in calls:
        function = call.get("function") or {}
        raw = function.get("arguments")
        try:
            json.loads(raw) if isinstance(raw, str) else None
            complete += 1 if function.get("name") and raw else 0
            partial += 0 if (function.get("name") and raw) else 1
        except (json.JSONDecodeError, TypeError):
            partial += 1
    return complete, partial


def _last_assistant_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


__all__ = [
    "AgentResult",
    "Budget",
    "NoProgressPolicy",
    "RoundResult",
    "STOP_BUDGET",
    "STOP_CONTRACT_FAILED",
    "STOP_END_TURN",
    "STOP_ERROR",
    "STOP_MAX_ROUNDS",
    "STOP_MAX_TOKENS",
    "STOP_NO_PROGRESS",
    "run",
]
