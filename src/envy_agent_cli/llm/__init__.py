"""模块1 · LLM 客户端（传输 + 协议 + 适配 + 输出契约）。

对外四个概念：
- **events**：事件协议（六种事件，Error 带 code/retryable）
- **ChatModel**：模型适配契约（Loop 只依赖它）
- **ChatParams**：采样参数（显式对象，可版本化）
- **工厂 / RetryPolicy / validator**：路由、重试、输出契约各管一段

分工（别搞混）：
传输层只认字节和协议 → Adapter 只做归一化 → 工厂管路由 → RetryPolicy 管重试 → Loop 管降级。
"""

from envy_agent_cli.llm.adapter import ChatMessage, ChatModel, OpenAICompatAdapter
from envy_agent_cli.llm.events import (
    RETRYABLE_LLM_CODES,
    AnyEvent,
    Error,
    LLMErrorCode,
    MessageEnd,
    ReasoningDelta,
    StopReason,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from envy_agent_cli.llm.factory import available, build, build_with_fallback, register
from envy_agent_cli.llm.params import ChatParams
from envy_agent_cli.llm.retry import RetryPolicy
from envy_agent_cli.llm.validator import OutputContract, ValidationOutcome, validate_output

__all__ = [
    "RETRYABLE_LLM_CODES",
    "AnyEvent",
    "ChatMessage",
    "ChatModel",
    "ChatParams",
    "Error",
    "LLMErrorCode",
    "MessageEnd",
    "OpenAICompatAdapter",
    "OutputContract",
    "ReasoningDelta",
    "RetryPolicy",
    "StopReason",
    "TextDelta",
    "ToolCallDelta",
    "Usage",
    "ValidationOutcome",
    "available",
    "build",
    "build_with_fallback",
    "register",
    "validate_output",
]
