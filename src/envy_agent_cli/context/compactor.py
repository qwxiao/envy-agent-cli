"""模块4 · 上下文压缩器。

messages 是模型的**唯一记忆**——模型无状态，每轮全量重发，历史必然膨胀。
压缩器是这条链路上**唯一能删改 messages 的地方**：其他层只追加，所以历史被搞坏，只可能是这一层干的。

已定型的形态：
- 预算对象：**80% 触发 / 压至 55%** 双阈值（留出空间防抖动）；
- 双触发条件：按比例触发 + 按轮次触发；
- **切分红线：保住工具组完整**——不能让 `tool` 消息与它的 `assistant` 调用请求被切开，
  否则会出现孤儿 `tool_call_id`，API 直接 400。

本文件保持**纯计算、零 IO**：摘要能力由外部注入，这里不 import 任何 `llm/` 的东西。

已落地：
- `estimate_tokens()` —— 本地启发式估算，决定"要不要压"；
- `_is_safe_split()` / `_safe_boundary()` —— 切分红线，本模块唯一的"错了就 400"。

待封版：`ContextWindowManager.prepare()` 的接口形状（选段、摘要、收缩三级策略仍在裁决中）。
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any

#: 每条消息的固定开销（角色标记、分隔符等）。
MESSAGE_OVERHEAD_TOKENS = 4

#: 单个工具定义的固定开销（JSON Schema 的结构符号）。
TOOL_DEFINITION_OVERHEAD_TOKENS = 8

_CJK = re.compile(r"[一-鿿]")


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


# ---------------------------------------------------------------- 切分红线

def _call_pairs(messages: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    """扫一遍消息，返回两张表：`call_id -> 声明处下标`、`call_id -> 应答处下标`。"""
    declared: dict[str, int] = {}
    answered: dict[str, int] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                call_id = call.get("id") if isinstance(call, dict) else None
                if call_id:
                    declared.setdefault(call_id, index)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id:
                answered.setdefault(call_id, index)
    return declared, answered


def _split_violations(messages: list[dict], split: int) -> tuple[list[str], list[str]]:
    """在 `split` 处切开会产生哪些问题。

    返回 `(孤儿工具结果, 悬空的调用声明)`：

    - **孤儿工具结果**：保留区里有一条 `tool` 消息，而它的调用声明被留在了摘要那一侧。
      这是**切分自己制造**的问题，服务端校验直接 400。
    - **悬空的调用声明**：保留区里有一个 `assistant.tool_calls` 没有应答。
      这是历史**本来就有的**（正常循环不会产生，只可能是中途终止留下的），切分造不出来它，
      但**留在保留区里一样发不出去**，所以它和孤儿工具结果同等对待。
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
    工具调用在协议上是一组：`assistant.tool_calls` 是请求，`tool` 是应答。两类残缺都发不出去：

    - 保留区里有一条 `tool` 消息、它的调用声明被摘要掉了 → 找不到发起方，服务端直接 400；
    - 保留区里有一个没有应答的调用声明 → 悬空，多数厂商同样 400。

    第二种正常循环产生不了，只可能是中途终止留下的。遇到它，唯一安全的办法是
    **把整段历史都摘要掉**（切在末尾），让悬空声明随摘要一起消失——
    所以这里判它不安全，把切分点往后推，而不是当作"历史自己的问题、与我无关"。
    """
    if not 0 <= split <= len(messages):
        return False
    orphan_tools, dangling_calls = _split_violations(messages, split)
    return not orphan_tools and not dangling_calls


def _safe_boundary(messages: list[dict], candidate: int) -> int:
    """把候选切分点往后推到最近的安全位置。

    返回 `len(messages)` 表示**没有安全切分点**——历史末尾留着一个没有应答的调用声明，
    任何切法都会把它和它前面的内容分开。调用方遇到这个值应当放弃本次压缩，而不是硬切。
    """
    candidate = min(max(candidate, 0), len(messages))
    while candidate < len(messages) and not _is_safe_split(messages, candidate):
        candidate += 1
    return candidate


# ---------------------------------------------------------------- 待封版

@dataclass(slots=True)
class ContextBudget:
    """上下文预算：按模型窗口算触发线与目标线。

    `trigger_ratio` 是"现在必须动手了"，`target_ratio` 是"动手就多做一点，别叫我马上再来一次"。
    只压到刚好触发线会**抖动**：下一轮立刻又超、又压，每轮都在烧摘要。
    """

    context_window: int
    max_output_tokens: int = 4096
    trigger_ratio: float = 0.80   # 超过就压
    target_ratio: float = 0.55    # 压到这个水平（防反复触发）


@dataclass(slots=True)
class PrepareResult:
    """一轮请求前的准备结果。"""

    messages: list[dict]
    compressed: bool = False
    summarized_messages: int = 0
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0


class ContextWindowManager:
    """每轮请求前的前置检查：超线就把旧轮次摘要掉。"""

    def __init__(self, budget: ContextBudget) -> None:
        self.budget = budget

    def prepare(self, messages: list[dict], tool_definitions: list[dict] | None = None) -> PrepareResult:
        """检查并（必要时）压缩。接口待定。"""
        raise NotImplementedError("接口待定")


__all__: list[Any] = [
    "ContextBudget",
    "ContextWindowManager",
    "PrepareResult",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "estimate_tokens",
]
