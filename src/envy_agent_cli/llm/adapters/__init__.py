"""模块1 · 厂商适配器。

每个厂商一个模块；共同点（OpenAI 兼容协议）抽到 `openai_compat`。
新增厂商 = 加一个实现 + 在工厂注册，Loop 零改动。
"""

from envy_agent_cli.llm.adapters.openai_compat import OpenAICompatAdapter

__all__ = ["OpenAICompatAdapter"]
