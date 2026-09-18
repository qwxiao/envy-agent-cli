"""模块1 · 事件协议。

LLM 客户端把"字节流"翻译成一串 typed 事件吐出去；上层（编排层）消费事件做决策。
客户端只报**事实**（发生了什么），不带**观点**（怎么渲染、下一步干嘛）。

两条铁律（见 ARCHITECTURE.md 第八节）：
1. 工具调用参数只做 `arguments += 碎片` 追加，**绝不中途 json.loads**——单片不是合法 JSON；
   攒成完整串的动作在编排层，不在这一层。
2. 结束判定看 `stop_reason`（服务端明说的原因），**不靠"tool_calls 空不空"猜**。
"""

from dataclasses import dataclass
from typing import Literal

# 结束原因：由厂商的 finish_reason 归一化而来
StopReason = Literal["tool_use", "end_turn", "max_tokens", "stop_sequence"]

LLMEvent = "TextDelta | ToolCallDelta | MessageEnd | Usage | Error"


@dataclass(slots=True)
class TextDelta:
    """正文增量。只负责"有字来了"，要不要打印、怎么打印是上层的事。"""

    text: str


@dataclass(slots=True)
class ToolCallDelta:
    """工具调用的**碎片**。

    一次工具调用会被拆成多片到达：第一片带 index / id / name，后续片只有 index + arguments
    的一小截字符串。本层只做搬运和打标，**不去拼**。
    """

    index: int | None
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""  # JSON 字符串的一小截，不是完整 JSON


@dataclass(slots=True)
class MessageEnd:
    """一条完整消息结束，携带服务端给出的结束原因。"""

    stop_reason: StopReason


@dataclass(slots=True)
class Usage:
    """真实 token 用量（请求带 include_usage 时，流末由服务端给出）。

    与压缩器里的"估算"分工不同：估算管**事前防爆**（触发压缩），这里管**事后记账**（计费/落库/评测）。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class Error:
    """异常事件化：超时、HTTP 错、解析失败都走这条通道。

    流式场景响应已经开了一半，抛异常会让上层在"消费到一半"的状态接裸奔栈，不好收尾。
    **连异常都是数据。**
    """

    message: str


AnyEvent = TextDelta | ToolCallDelta | MessageEnd | Usage | Error


def map_finish_reason(reason: str | None) -> StopReason:
    """把厂商的 finish_reason 归一化成我们的事件词汇。

    tool_calls → tool_use；length → max_tokens；content_filter → stop_sequence；其余 → end_turn。
    """
    raise NotImplementedError("待移植：模块1 已验证的 finish_reason 映射实现")
