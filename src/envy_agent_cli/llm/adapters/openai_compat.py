"""模块1 · OpenAI 兼容协议适配器。

多数国内厂商（DeepSeek / GLM / Kimi / Step 等）都提供 OpenAI 兼容协议，
**换厂商 = 换 base_url + 模型名 + 凭证**，事件归一化逻辑完全复用本层。

新增一个厂商 = 加一个 Adapter 实现 + 在工厂里注册，**Loop 零改动**——
这是开闭原则在模型层的落地，和"加工具不改 Loop"是同一个设计思想的两次应用。
"""

from typing import Any, Iterator

from envy_agent_cli.llm.events import AnyEvent
from envy_agent_cli.llm.protocol import ChatMessage


class OpenAICompatAdapter:
    """满足 ModelAdapter 契约的通用适配器（OpenAI 兼容协议）。

    Attributes:
        name: 适配器标识（如 "deepseek" / "glm" / "kimi"）。
        base_url: 厂商的 OpenAI 兼容入口。
        model: 模型名。
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 0,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        **params: Any,
    ) -> Iterator[AnyEvent]:
        """实现见 ModelAdapter 契约。

        本层**不做重试**：重试策略属于 Runtime / 工厂，适配器只管"把这一家的流翻译成统一事件"。
        """
        raise NotImplementedError("待移植：复用 client.py 的传输层，仅替换 base_url / 模型 / 凭证")

    def _request_body(self, messages: list[ChatMessage], tools: list[dict] | None, params: dict) -> dict:
        """组装请求体（含 `stream` 与 `stream_options.include_usage`）。"""
        raise NotImplementedError
