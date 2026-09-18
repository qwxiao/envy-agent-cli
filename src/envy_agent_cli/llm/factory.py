"""模块1 · Adapter 工厂：厂商注册表 + 路由 / fallback 的唯一发生地。

**为什么要有工厂**：Loop 只认识 `ModelAdapter` Protocol，它不该知道有几个厂商、
谁是主力谁是备胎、失败之后换谁。这些决策集中在工厂里，Loop 保持零感知。

⚠️ 与"模型 Gateway"划清边界：本工厂是**进程内**的一个模块，不是独立服务。
只有出现"多应用共享 / 多租户 / 统一密钥路由限流计费"时，才把这块抽成独立的 LLM Gateway。
"""

from typing import Callable

from envy_agent_cli.llm.protocol import ModelAdapter

_REGISTRY: dict[str, Callable[..., ModelAdapter]] = {}


def register(provider: str, builder: Callable[..., ModelAdapter]) -> None:
    """注册一个厂商的构造器。新增厂商只动这里，不动 Loop。"""
    raise NotImplementedError


def build(provider: str, **options) -> ModelAdapter:
    """按厂商名构造适配器。"""
    raise NotImplementedError


def build_with_fallback(providers: list[str], **options) -> ModelAdapter:
    """按顺序构造"主 + 备"适配器，主失败时降级到备。

    降级只发生在这里：Loop 永远只会拿到一个可用的适配器。
    """
    raise NotImplementedError
