"""模块2 · Agent Loop（编排层）。

只做编排：消费事件、攒工具调用、决定继续或结束。执行一律交给 ToolRuntime。
"""

from envy_agent_cli.loop.react import MAX_ROUNDS, run
from envy_agent_cli.trace import TraceContext, new_trace, round_span, tool_span

__all__ = ["MAX_ROUNDS", "TraceContext", "new_trace", "round_span", "run", "tool_span"]
