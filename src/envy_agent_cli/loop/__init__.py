"""模块2 · Agent Loop（编排层）。

只做编排：消费事件、攒工具调用、决定继续或结束。执行一律交给 ToolRuntime。

对外四个概念：`run`（跑一次任务）、`AgentResult`（产出）、`Renderer`（怎么说）、
`trace`（三级追踪，定义在顶层 `envy_agent_cli.trace`）。
"""

from envy_agent_cli.loop.react import (
    MAX_ROUNDS,
    NO_PROGRESS_LIMIT,
    AgentResult,
    RoundResult,
    run,
)
from envy_agent_cli.loop.renderer import ConsoleRenderer, NullRenderer, Renderer
from envy_agent_cli.trace import TraceContext, new_trace, round_span, tool_span

__all__ = [
    "MAX_ROUNDS",
    "NO_PROGRESS_LIMIT",
    "AgentResult",
    "ConsoleRenderer",
    "NullRenderer",
    "Renderer",
    "RoundResult",
    "TraceContext",
    "new_trace",
    "round_span",
    "run",
    "tool_span",
]
