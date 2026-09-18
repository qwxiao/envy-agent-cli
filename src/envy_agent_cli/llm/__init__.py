"""模块1 · LLM 客户端（传输与协议层）。

对外只暴露三样：事件类型、适配器契约、适配器工厂。
传输实现细节（SSE 分帧、chunk 解析）不外泄。
"""

from envy_agent_cli.llm.events import (
    AnyEvent,
    Error,
    MessageEnd,
    StopReason,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from envy_agent_cli.llm.protocol import ChatMessage, ModelAdapter

__all__ = [
    "AnyEvent",
    "ChatMessage",
    "Error",
    "MessageEnd",
    "ModelAdapter",
    "StopReason",
    "TextDelta",
    "ToolCallDelta",
    "Usage",
]
