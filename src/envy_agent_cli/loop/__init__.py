"""模块2 · Agent Loop（编排层）。

只做编排：消费事件、攒工具调用、决定继续或结束。执行一律交给 ToolRuntime。

对外四个概念：`run`（跑一次任务）、`AgentResult`（产出）、`Budget` + `NoProgressPolicy`（护栏）、
`Renderer`（怎么说）。三级 trace 定义在顶层 `envy_agent_cli.trace`。
"""

from envy_agent_cli.loop.react import (
    STOP_BUDGET,
    STOP_CONTRACT_FAILED,
    STOP_END_TURN,
    STOP_ERROR,
    STOP_MAX_ROUNDS,
    STOP_MAX_TOKENS,
    STOP_NO_PROGRESS,
    AgentResult,
    Budget,
    NoProgressPolicy,
    RoundResult,
    run,
)
from envy_agent_cli.loop.renderer import ConsoleRenderer, NullRenderer, Renderer
from envy_agent_cli.trace import TraceContext, new_trace, round_span, tool_span

__all__ = [
    "STOP_BUDGET",
    "STOP_CONTRACT_FAILED",
    "STOP_END_TURN",
    "STOP_ERROR",
    "STOP_MAX_ROUNDS",
    "STOP_MAX_TOKENS",
    "STOP_NO_PROGRESS",
    "AgentResult",
    "Budget",
    "ConsoleRenderer",
    "NoProgressPolicy",
    "NullRenderer",
    "Renderer",
    "RoundResult",
    "TraceContext",
    "new_trace",
    "round_span",
    "run",
    "tool_span",
]
