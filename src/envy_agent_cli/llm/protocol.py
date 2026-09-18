"""模块1 · 模型适配契约（模型契约的**反面**：这是给 Loop 看的接口）。

Loop 只依赖这一层 Protocol，**不依赖任何厂商 SDK**。
换模型 = 构造一个新的 Adapter 对象再注入，Loop 代码一个字不改。

附带收益（面试可讲）：
- 可以写 FakeAdapter 做不依赖真实模型的单元测试；
- 可以在 Adapter 层统一记录 token / 费用 / trace；
- 路由与 fallback 只发生在工厂里，不在 Loop 内。
"""

from typing import Any, Iterator, Protocol, TypedDict, runtime_checkable

from envy_agent_cli.llm.events import AnyEvent


class ChatMessage(TypedDict, total=False):
    """发给模型的消息。字段与 OpenAI 兼容协议一致（role / content / tool_calls / tool_call_id）。"""

    role: str
    content: str
    tool_calls: list[dict]
    tool_call_id: str


@runtime_checkable
class ModelAdapter(Protocol):
    """所有模型厂商适配器必须满足的契约。

    实现方只需保证：**给定 messages，吐出一串 typed 事件**。
    不许打印、不许渲染、不许替上层决定任务是否结束。
    """

    name: str

    def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        **params: Any,
    ) -> Iterator[AnyEvent]:
        """流式对话：逐条 yield 事件，直到出现 MessageEnd 或 Error。

        Args:
            messages: 完整对话历史（模型无状态，每轮全量重发）。
            tools: 模型契约列表（只有 name/description/input_schema 三样）。
            **params: 采样参数（temperature / max_tokens 等），按"可回归"方式管理。

        Yields:
            TextDelta / ToolCallDelta / MessageEnd / Usage / Error
        """
        ...
