"""模块4 · 上下文压缩器。

messages 是模型的**唯一记忆**——模型无状态，每轮全量重发，历史必然膨胀。
压缩器是这条链路上**唯一能删改 messages 的地方**：其他层只追加，所以历史被搞坏，只可能是这一层干的。

三条纪律：

1. **切分的单位是"工具包"，不是消息。** 工具调用在协议上是一组
   （`assistant.tool_calls` 请求 + N 条 `tool` 应答），拆开就是孤儿，请求直接 400。
   对齐发生冲突时**永远多保留**——保留得多是安全方向。
2. **摘要是有损的二手信息**，必须显式标记、必须能落审计、**必须允许失败**。
   压缩是尽力而为的旁路，永不抛出：真超窗口了走模块1 已有的错误路径，比在这里造一个新失败模式诚实。
3. **两本账分开。** 本地估算管"要不要压"，服务端 `Usage` 管成本核算，**两者刻意不联动**——
   一旦用真值校准估算，组件就有了跨调用状态，同样的输入在不同轮次会压出不同结果，
   所有离线断言都会变成"取决于之前跑过什么"。

本文件**纯计算、零 IO**：摘要能力由外部注入，这里不 import 任何 `llm/` 的东西。
"""

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from envy_agent_cli.trace import TraceContext

# ---------------------------------------------------------------- 常量

TRIGGER_NONE = "none"      # 未触发（含 Noop）
TRIGGER_RATIO = "ratio"
TRIGGER_COUNT = "count"
TRIGGER_BOTH = "both"

ENGINE_RULE = "rule"
ENGINE_LLM = "llm"

SYSTEM_ROLE = "system"
TOOL_ROLE = "tool"
ASSISTANT_ROLE = "assistant"
USER_ROLE = "user"

#: 摘要消息的标签。它**不是**用来补救角色的（角色本身已经是正确的），
#: 而是给审计和评测做锚点——`grep history_summary` 能定位到摘要。
SUMMARY_TAG = "history_summary"

#: 每条消息的固定开销（角色标记、分隔符等）。
MESSAGE_OVERHEAD_TOKENS = 4

#: 单个工具定义的固定开销（JSON Schema 的结构符号）。
TOOL_DEFINITION_OVERHEAD_TOKENS = 8

#: 截断标记：让模型知道"这段我掐了"，不会拿残缺内容当完整事实下结论。
TRUNCATION_MARKER = "\n...[tool result truncated; {removed} characters omitted]"

_CJK = re.compile(r"[一-鿿]")
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------- token 估算

def estimate_text_tokens(text: str) -> int:
    """文本的启发式 token 估算：中文单字 ≈ 1 token，其余 3 字符 ≈ 1 token，向上取整。

    **策略是宁高不宁低**：高估的代价是多压一次，低估的代价是把请求发爆、吃一个 400。
    真值只在服务端 `Usage` 里，那是**成本核算**用的，与这里**不要混**——
    决策用估算（够用且无状态），成本用真值（准确）。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil(max(0, len(text) - cjk) / 3)


def _text_of(message: dict) -> str:
    """取消息正文：None/缺失返回空串，非字符串（多段 content）转紧凑 JSON。"""
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def estimate_message_tokens(message: dict) -> int:
    """单条消息的估算 = 固定开销 + 正文 + 调用声明（调用声明单独序列化）。"""
    calls = message.get("tool_calls")
    calls_json = json.dumps(calls, ensure_ascii=False) if calls else ""
    return (
        MESSAGE_OVERHEAD_TOKENS
        + estimate_text_tokens(_text_of(message))
        + estimate_text_tokens(calls_json)
    )


def estimate_tokens(messages: list[dict], tool_definitions: list[dict] | None = None) -> int:
    """一次请求的输入估算 = 全部消息 + 工具定义。

    工具定义**每轮都要重发**，几十个工具的 JSON Schema 是实打实的几千 token——
    算预算时不含它就是算错账。
    """
    total = sum(estimate_message_tokens(message) for message in messages)
    if tool_definitions:
        schema = json.dumps(tool_definitions, ensure_ascii=False, separators=(",", ":"))
        total += estimate_text_tokens(schema) + len(tool_definitions) * TOOL_DEFINITION_OVERHEAD_TOKENS
    return total


# 注入点：把"怎么数 token"和"什么时候压"解耦，将来换真 tokenizer 只改这一个函数。
TokenEstimator = Callable[[list[dict], list[dict] | None], int]


# ---------------------------------------------------------------- 切分红线

def _call_pairs(messages: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    """扫一遍消息，返回两张表：`call_id -> 声明处下标`、`call_id -> 应答处下标`。"""
    declared: dict[str, int] = {}
    answered: dict[str, int] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == ASSISTANT_ROLE:
            for call in message.get("tool_calls") or ():
                call_id = call.get("id") if isinstance(call, dict) else None
                if call_id:
                    declared.setdefault(call_id, index)
        elif role == TOOL_ROLE:
            call_id = message.get("tool_call_id")
            if call_id:
                answered.setdefault(call_id, index)
    return declared, answered


def _split_violations(messages: list[dict], split: int) -> tuple[list[str], list[str]]:
    """在 `split` 处切开会产生哪些问题。

    返回 `(孤儿工具结果, 悬空的调用声明)`：

    - **孤儿工具结果**：保留区里有一条 `tool` 消息，而它的调用声明被留在了摘要那一侧。
    - **悬空的调用声明**：保留区里有一个 `assistant.tool_calls` 没有应答。

    两类都发不出去。第二种正常循环产生不了，只可能是中途终止留下的。
    """
    declared, answered = _call_pairs(messages)
    orphan_tools = [
        call_id
        for call_id, answered_at in answered.items()
        if answered_at >= split and declared.get(call_id, -1) < split
    ]
    dangling_calls = [
        call_id
        for call_id, declared_at in declared.items()
        if declared_at >= split and call_id not in answered
    ]
    return orphan_tools, dangling_calls


def _is_safe_split(messages: list[dict], split: int) -> bool:
    """`split` 处能不能切：切完两侧都不能出现孤儿。

    **这是本模块唯一的"错了就 400"红线。**
    工具调用在协议上是一组：`assistant.tool_calls` 是请求，`tool` 是应答，拆开就是孤儿。
    """
    if not 0 <= split <= len(messages):
        return False
    orphan_tools, dangling_calls = _split_violations(messages, split)
    return not orphan_tools and not dangling_calls


def _summarizable(messages: list[dict]) -> list[dict]:
    """可进摘要的消息：**所有 `system` 消息都不算**。

    保护 system 靠**角色判断**，不靠位置假设——位置假设是脆的，把 system 放中间的那天就是它失效的那天。
    """
    return [message for message in messages if message.get("role") != SYSTEM_ROLE]


# ---------------------------------------------------------------- 数据契约

@dataclass(slots=True, frozen=True)
class ContextBudget:
    """上下文预算：窗口、预留、阈值，以及保留区与截断线的规模。

    `trigger_ratio` 是"现在必须动手了"，`target_ratio` 是"动手就多做一点，别叫我马上再来一次"。
    只压到刚好触发线会**抖动**：下一轮立刻又超、又压，每轮都在烧摘要。
    """

    context_window: int
    max_output_tokens: int = 4096
    reserve_tokens: int = 2048              # 序列化开销、角色标记、结构包装的余量
    trigger_ratio: float = 0.80             # 超过就压
    target_ratio: float = 0.55              # 压到这个水平（防反复触发）
    min_recent_messages: int = 6            # 保留区规模：从尾部数这么多条
    max_history_messages: int = 100         # 条数触发线：防"token 没超但轮次失控"
    tool_result_max_chars: int = 4000       # 单条工具结果的截断线
    summary_max_chars: int = 6000           # 摘要消息的长度上限

    def __post_init__(self) -> None:
        """配置项的合法性检查就该在这个位置——错配的后果是**压缩静默永不发生**，不报错的坑最难查。"""
        if self.min_recent_messages >= self.max_history_messages:
            raise ValueError("保留区不能大于等于历史上限，否则永不压缩")
        if not 0 < self.target_ratio < self.trigger_ratio < 1:
            raise ValueError("必须满足 0 < target_ratio < trigger_ratio < 1")

    @property
    def effective_window(self) -> int:
        """有效窗口 = 总窗口 − 输出预留 − 杂项预留。

        **这两项必须计入触发判定**，不能只在估算时减掉——
        否则会出现"估算看着没超、真发出去就超了"，还是那个 400。
        """
        return self.context_window - self.max_output_tokens - self.reserve_tokens

    @property
    def trigger_tokens(self) -> int:
        return int(self.effective_window * self.trigger_ratio)

    @property
    def target_tokens(self) -> int:
        return int(self.effective_window * self.target_ratio)


@dataclass(slots=True)
class PrepareResult:
    """一轮请求前的准备结果。四个统计字段是给"归约只搬运不计算"用的。"""

    messages: list[dict]
    compressed: bool = False
    summarized_messages: int = 0
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    truncated_tool_results: int = 0
    triggered_by: str = TRIGGER_NONE        # none | ratio | count | both
    summary_engine: str = ENGINE_RULE       # rule | llm
    shrank_twice: bool = False
    degraded: bool = False                  # 尽力压完仍在目标线上，已如实发送（正常状态，不是错误）
    failure: str | None = None              # 摘要引擎抛异常时的原因（压缩整体回退，不抛出）


@dataclass(slots=True)
class ContextEvent:
    """一次压缩的对外记录。**只交事件，不交快照**——快照会随轮次线性增长，且绝大多数没人看。"""

    span_id: str
    before_tokens: int
    after_tokens: int
    summarized_messages: int
    triggered_by: str
    summary_engine: str
    degraded: bool


def to_context_event(result: PrepareResult, span_id: str) -> ContextEvent:
    """归约：**只搬运，不推导、不重算**。审计与事件同源，靠的就是这一份数据。"""
    return ContextEvent(
        span_id=span_id,
        before_tokens=result.estimated_tokens_before,
        after_tokens=result.estimated_tokens_after,
        summarized_messages=result.summarized_messages,
        triggered_by=result.triggered_by,
        summary_engine=result.summary_engine,
        degraded=result.degraded,
    )


def build_summary_message(text: str) -> dict:
    """摘要消息：`user` + 纯文本，**绝不能带 `tool_calls`**。

    **角色由协议决定，不由语义偏好决定。** 隔离测试过：摘要放 `assistant` 时，
    DeepSeek 与 GLM **都**返回 400（system 之后的第一条消息必须是 `user`；
    DeepSeek 的报错文本会误导成 `reasoning_content`，与它无关）。
    语义上摘要更像"我此前说过什么的注记"，但那属于内部偏好——
    **协议约束违反的后果是请求根本发不出去，两者不是同一个量级。**

    `<history_summary>` 标签承担两件事：让模型知道这段是有损的压缩稿，以及给审计当锚点。
    """
    return {"role": USER_ROLE, "content": f"<{SUMMARY_TAG}>\n{text}\n</{SUMMARY_TAG}>"}


# ---------------------------------------------------------------- 契约

@runtime_checkable
class SummaryEngine(Protocol):
    """摘要引擎。规则档与 LLM 档共用这个契约，替换实现不改压缩逻辑。"""

    name: str

    def summarize(self, turns: list[dict]) -> str: ...


@runtime_checkable
class ContextPolicy(Protocol):
    """上下文策略。编排层只认这个——"不压缩"也是一个策略，不是"没实现"。

    它比 `Renderer` 多一个 `trace` 参数：Renderer 只输出不落盘，policy 要落审计。
    **落盘者必须能归因。**
    """

    def prepare(self, messages: list[dict], *, tool_definitions: list[dict] | None = None,
                trace: TraceContext | None = None) -> PrepareResult: ...


class NoopContextPolicy:
    """恒等实现：原样返回，不压缩。"""

    def prepare(self, messages: list[dict], *, tool_definitions: list[dict] | None = None,
                trace: TraceContext | None = None) -> PrepareResult:
        return PrepareResult(messages=list(messages), triggered_by=TRIGGER_NONE)


# ---------------------------------------------------------------- 管理器

class ContextWindowManager:
    """超线就压：找切分点 → 摘要旧轮次 → 截断超大工具结果 → 收缩摘要。

    它是 `ContextPolicy` 的真实现，`prepare()` 的签名与契约逐字一致。
    """

    def __init__(self, budget: ContextBudget, *, summary_engine: SummaryEngine,
                 estimator: TokenEstimator = estimate_tokens) -> None:
        self.budget = budget
        self.summary_engine = summary_engine
        self._estimator = estimator

    def prepare(self, messages: list[dict], *, tool_definitions: list[dict] | None = None,
                trace: TraceContext | None = None) -> PrepareResult:
        """每轮请求前调用一次。不超线就走快路径原样返回；超线压一次到位，**绝不循环**。"""
        before = self._estimator(messages, tool_definitions)
        plan = self._find_split_point(messages, before)
        if plan is None:
            return PrepareResult(messages=list(messages), estimated_tokens_before=before,
                                 estimated_tokens_after=before, summary_engine=self.summary_engine.name)
        split, triggered_by = plan

        older = messages[:split]
        kept = list(messages[split:])
        # system 提出来原样留在最前，摘要插在最后一条 system 之后、保留区之前
        leading = [message for message in older if message.get("role") == SYSTEM_ROLE]
        turns = _summarizable(older)

        try:
            summary = self.summary_engine.summarize(turns)
        except Exception as exc:  # noqa: BLE001 — 压缩是旁路，任何失败都不该拖垮任务
            return PrepareResult(messages=list(messages), estimated_tokens_before=before,
                                 estimated_tokens_after=before,
                                 triggered_by=triggered_by, summary_engine=self.summary_engine.name,
                                 failure=f"{type(exc).__name__}: {exc}")

        compacted = leading + [build_summary_message(summary)] + kept
        compacted, truncated = self._truncate_tool_payloads(compacted)

        # 第一级收缩（截断工具结果）之后仍超目标线，才做第二级；做完就用结果，不循环
        after = self._estimator(compacted, tool_definitions)
        shrank_twice = False
        if after > self.budget.target_tokens:
            compacted = self._shrink_summary(compacted)
            after = self._estimator(compacted, tool_definitions)
            shrank_twice = True

        return PrepareResult(
            messages=compacted,
            compressed=True,
            summarized_messages=len(turns),
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            truncated_tool_results=truncated,
            triggered_by=triggered_by,
            summary_engine=self.summary_engine.name,
            shrank_twice=shrank_twice,
            degraded=after > self.budget.target_tokens,
        )

    # ---- 切分点：全局唯一的出口（触发判定 + 对齐 + 下界） ----

    def _find_split_point(self, messages: list[dict], before: int) -> tuple[int, str] | None:
        """返回 `(切分点, 触发原因)`；未触发、或前面没有可压内容时返回 `None`。

        触发判定、对齐方向、下界三件事合并在这一个函数里，
        这样"不假压"只需要在一处保证——**单一出口 = 单一真相**，不会有某条路径绕过检查。
        """
        over_ratio = before > self.budget.trigger_tokens
        over_count = len(messages) > self.budget.max_history_messages
        if not (over_ratio or over_count):
            return None
        triggered_by = (
            TRIGGER_BOTH if over_ratio and over_count
            else TRIGGER_RATIO if over_ratio
            else TRIGGER_COUNT
        )

        # 保留区只由 min_recent_messages 定义：从尾部数这么多条。
        # 这里**没有"轮"的概念**——"以 user 消息为界"在单用户长任务下会让保留区等于全部历史，
        # 压缩永不发生。切分边界要锚协议强约束（工具组成对），不锚语义猜测。
        candidate = len(messages) - self.budget.min_recent_messages

        # 对齐冲突时向前移（保留区变大）——多保留是安全方向，少保留可能把在用的上下文压掉。
        while candidate > 0 and not _is_safe_split(messages, candidate):
            candidate -= 1

        if not _is_safe_split(messages, candidate) or not _summarizable(messages[:candidate]):
            return None
        return candidate, triggered_by

    # ---- 两级收缩 ----

    def _truncate_tool_payloads(self, messages: list[dict]) -> tuple[list[dict], int]:
        """第一级：截断保留区里超长的工具结果，末尾打标记。

        降 token **只靠截断内容，不靠切开工具组**——
        截断是"信息降级"（模型知道不全，能自我修正），切开是"结构破坏"（拿到一个无法解释的孤儿结果）。
        **降级可恢复，破坏不可恢复。**
        """
        out: list[dict] = []
        truncated = 0
        for message in messages:
            content = message.get("content")
            if (message.get("role") == TOOL_ROLE and isinstance(content, str)
                    and len(content) > self.budget.tool_result_max_chars):
                removed = len(content) - self.budget.tool_result_max_chars
                clone = dict(message)
                clone["content"] = (
                    content[: self.budget.tool_result_max_chars]
                    + TRUNCATION_MARKER.format(removed=removed)
                )
                out.append(clone)
                truncated += 1
            else:
                out.append(dict(message))
        return out, truncated

    def _shrink_summary(self, messages: list[dict]) -> list[dict]:
        """第二级：把摘要再压一截。

        **按"找出摘要那条消息"定位，不按下标 0 定位**——system 在 messages 里时下标 0 不是摘要，
        按下标找会静默失配（不报错，只是永远不生效）。
        """
        out = [dict(message) for message in messages]
        index = next((i for i, message in enumerate(out)
                      if f"<{SUMMARY_TAG}>" in _text_of(message)), None)
        if index is None:
            return out
        others = out[:index] + out[index + 1:]
        remaining = max(128, self.budget.target_tokens - self._estimator(others, None))
        limit = max(256, min(self.budget.summary_max_chars, remaining * 3))
        content = _text_of(out[index])
        if len(content) > limit:
            out[index] = {**out[index], "content": content[: limit - 3] + "..."}
        return out


__all__: list[Any] = [
    "ContextBudget",
    "ContextEvent",
    "ContextPolicy",
    "ContextWindowManager",
    "ENGINE_LLM",
    "ENGINE_RULE",
    "NoopContextPolicy",
    "PrepareResult",
    "SUMMARY_TAG",
    "SummaryEngine",
    "TRIGGER_BOTH",
    "TRIGGER_COUNT",
    "TRIGGER_NONE",
    "TRIGGER_RATIO",
    "TokenEstimator",
    "build_summary_message",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "estimate_tokens",
    "to_context_event",
]
