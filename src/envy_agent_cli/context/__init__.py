"""模块4 · 上下文预算与压缩。

`compactor.py` 是纯计算层（零 IO），摘要引擎从外部注入；
LLM 档放在 `summary_llm.py`，是唯一会碰模型 IO 的地方。
"""

from envy_agent_cli.context.compactor import (
    ContextBudget,
    ContextEvent,
    ContextPolicy,
    ContextWindowManager,
    NoopContextPolicy,
    PrepareResult,
    SummaryEngine,
    to_context_event,
)
from envy_agent_cli.context.summary_rule import RuleSummaryEngine

__all__ = [
    "ContextBudget",
    "ContextEvent",
    "ContextPolicy",
    "ContextWindowManager",
    "NoopContextPolicy",
    "PrepareResult",
    "RuleSummaryEngine",
    "SummaryEngine",
    "to_context_event",
]
