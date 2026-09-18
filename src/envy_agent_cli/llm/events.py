"""模块1 · 事件协议（纯数据，不含行为）。

LLM 客户端把"字节流"翻译成一串 typed 事件吐出去；上层（编排层）消费事件做决策。
客户端只报**事实**（发生了什么），不带**观点**（怎么渲染、下一步干嘛）。

**六种事件**：TextDelta / ReasoningDelta / ToolCallDelta / MessageEnd / Usage / Error。

三条铁律：
1. 工具调用参数只做 `arguments += 碎片` 追加，**绝不中途 json.loads**——单片不是合法 JSON；
   攒成完整串的动作在编排层，不在这一层。
2. 结束判定看 `stop_reason`（服务端明说的原因），**不靠"tool_calls 空不空"猜**。
3. 异常也走事件通道（`Error`），不让上层在"消费到一半"的状态接裸奔栈。

> `Error.retryable` **由 Adapter 判定并显式给出**，Loop 不推导。
> 只有 Adapter 掌握完整上下文（状态码、错误体、第几次尝试），让它去猜字符串里有没有 "429" 是脆弱的。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

#: 结束原因：由厂商的 finish_reason 归一化而来（四值，不再扩）
StopReason = Literal["tool_use", "end_turn", "max_tokens", "stop_sequence"]


class LLMErrorCode(str, Enum):
    """模型调用的结构化错误码。

    和工具层的 `ErrorCode` 是**同一套形状**——Loop 只处理一种错误形态：
    `(code, retryable)` 二元组，不靠字符串匹配判断该不该重试。
    """

    INVALID_REQUEST = "invalid_request"      # 400，本地修，不重试
    AUTH_FAILED = "auth_failed"              # 401/403，终止
    RATE_LIMITED = "rate_limited"            # 429，退避重试
    SERVER_ERROR = "server_error"            # 5xx，退避重试
    TIMEOUT = "timeout"                      # 可重试（幂等前提下）
    CONTENT_FILTERED = "content_filtered"    # 终止
    OUTPUT_INVALID = "output_invalid"        # 走输出契约层纠错
    UNKNOWN = "unknown"                      # 兜底，不重试


#: 允许重试的错误码。其余重试一万次也没用，只会放大成本。
RETRYABLE_LLM_CODES = frozenset({LLMErrorCode.RATE_LIMITED, LLMErrorCode.SERVER_ERROR,
                                 LLMErrorCode.TIMEOUT})


@dataclass(slots=True)
class TextDelta:
    """正文增量。只负责"有字来了"，要不要打印、怎么打印是上层的事。"""

    text: str


@dataclass(slots=True)
class ReasoningDelta:
    """推理（思维链）增量。

    和正文是**两种语义**：正文是产出，推理是过程证据。混在一起会导致
    渲染无法分别显示、压缩器无法判断可压缩性、评测无法单独看推理质量。
    """

    text: str


@dataclass(slots=True)
class ToolCallDelta:
    """工具调用的**碎片**。

    一次工具调用会被拆成多片到达：第一片带 id / name，后续片只有 arguments 的一小截字符串。
    本层只做搬运和打标，**不去拼**。

    Attributes:
        is_first: 是否是该次调用的第一片。显式给字段而不是新增 `ToolCallStart` 事件类型——
        避免下游 isinstance 分支膨胀，同时不用靠"index 是否已在槽位里"这种隐式判断。
    """

    index: int
    is_first: bool = False
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""  # JSON 字符串的一小截，不是完整 JSON


@dataclass(slots=True)
class MessageEnd:
    """一条完整消息结束，携带服务端给出的结束原因。

    ⚠️ `max_tokens` 是**失败不是成功**：说明被截断了，该扩大预算重生成，
    绝不允许"尽量解析截断的 JSON"。
    """

    stop_reason: StopReason


@dataclass(slots=True)
class Usage:
    """真实 token 用量（请求带 include_usage 时，流末由服务端给出）。

    与压缩器里的"估算"分工不同：估算管**事前防爆**（触发压缩），这里管**事后记账**（计费/落库/评测）。
    本层只负责吐，**落库归 M2 审计**。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class Error:
    """异常事件化：超时、HTTP 错、解析失败都走这条通道。

    连异常都是数据——流式场景响应已经开了一半，抛异常会让上层在半途接裸奔栈，不好收尾。
    """

    message: str
    code: LLMErrorCode = LLMErrorCode.UNKNOWN
    retryable: bool = False          # 由 Adapter 判定并显式给出
    status_code: int | None = None

    @classmethod
    def from_code(
        cls,
        code: LLMErrorCode,
        message: str,
        status_code: int | None = None,
    ) -> "Error":
        """按错误码自动推导 retryable——Adapter 侧的统一构造入口。"""
        return cls(message=message, code=code,
                   retryable=code in RETRYABLE_LLM_CODES, status_code=status_code)


AnyEvent = TextDelta | ReasoningDelta | ToolCallDelta | MessageEnd | Usage | Error


def map_http_status(status_code: int) -> LLMErrorCode:
    """HTTP 状态码 → 错误码。归一只做一次，别让每个厂商各写一份。"""
    if status_code == 400:
        return LLMErrorCode.INVALID_REQUEST
    if status_code in (401, 403):
        return LLMErrorCode.AUTH_FAILED
    if status_code == 429:
        return LLMErrorCode.RATE_LIMITED
    if 500 <= status_code < 600:
        return LLMErrorCode.SERVER_ERROR
    return LLMErrorCode.UNKNOWN
