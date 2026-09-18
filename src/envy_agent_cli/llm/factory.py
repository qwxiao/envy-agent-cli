"""模块1 · Adapter 工厂：厂商注册表 + 路由的唯一发生地。

**为什么要有工厂**：Loop 只认识 `ChatModel` 契约，它不该知道有几个厂商、谁是主力谁是备胎、
凭证从哪来。这些决策集中在工厂里，Loop 保持零感知。

⚠️ 与"模型 Gateway"划清边界：本工厂是**进程内**的一个模块，不是独立服务。
只有出现"多应用共享 / 多租户 / 统一密钥路由限流计费"时，才把这块抽成独立 LLM Gateway。
"""

from typing import Callable

from envy_agent_cli.llm.adapter import ChatModel, OpenAICompatAdapter

#: provider → 构造器。新增厂商只动这里（Loop 零改动）。
_REGISTRY: dict[str, Callable[..., ChatModel]] = {
    "deepseek": OpenAICompatAdapter,
    "glm": OpenAICompatAdapter,
    "openai": OpenAICompatAdapter,
}


def register(provider: str, builder: Callable[..., ChatModel]) -> None:
    """注册一个厂商的构造器。"""
    _REGISTRY[provider] = builder


def available() -> list[str]:
    """已注册的厂商名。"""
    return sorted(_REGISTRY)


def build(provider: str, api_key: str, **options) -> ChatModel:
    """按厂商名构造适配器。

    Raises:
        ValueError: 厂商未注册——fail loud，不要静默回退到某个默认厂商。
    """
    builder = _REGISTRY.get(provider)
    if builder is None:
        raise ValueError(f"未注册的厂商: {provider}（可用: {', '.join(available())}）")
    return builder(provider=provider, api_key=api_key, **options)


def build_with_fallback(providers: list[str], api_keys: dict[str, str], **options) -> "FallbackAdapter":
    """构造"主 + 备"适配器：主失败且可重试时降级到备。

    降级只发生在这里——Loop 永远只会拿到一个可用的适配器，它不知道背后换过厂商
    （这也是不做 `ProviderSwitch` 事件的原因：上层一旦知道，就会写出 `if provider == ...` 的依赖）。
    """
    if not providers:
        raise ValueError("providers 不能为空")
    chain = [build(p, api_keys[p], **options) for p in providers]
    return FallbackAdapter(chain)


class FallbackAdapter:
    """按顺序尝试一组适配器，把"换厂商"对上层完全隐藏。

    只对**还没吐出任何事件就失败**的情况降级——流中途失败不重放（会重复正文/工具碎片）。
    """

    def __init__(self, chain: list[ChatModel]) -> None:
        self._chain = chain
        self.name = chain[0].name

    def stream_chat(self, messages, tools=None, params=None, trace=None):
        raise NotImplementedError("待实现：与 RetryPolicy.retry_stream 同构的降级逻辑")


__all__ = ["ChatModel", "FallbackAdapter", "available", "build", "build_with_fallback", "register"]
