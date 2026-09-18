"""模块4 · 上下文压缩器（目录占位，接口待定）。

messages 是模型的**唯一记忆**——模型无状态，每轮全量重发，历史必然膨胀。
压缩器是唯一能让 messages 瘦下来的地方，也是"长任务会不会失忆"的答案。

已确定的形态（来自既有验证过的实现）：
- 预算对象：**80% 触发 / 压至 55%** 双阈值（留出空间防抖动）；
- 双触发条件：按比例触发 + 按轮次触发；
- **切分红线：保住工具组完整**——不能让 `tool` 消息与它的 `assistant` 调用请求被切开，
  否则会出现孤儿 `tool_call_id`，API 直接 400。

接口在 M2 之前定，本文件暂不封版。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ContextBudget:
    """上下文预算：按模型窗口算触发线与目标线。"""

    context_window: int
    max_output_tokens: int = 4096
    trigger_ratio: float = 0.80   # 超过就压
    target_ratio: float = 0.55    # 压到这个水平（防反复触发）


@dataclass(slots=True)
class PrepareResult:
    """一轮请求前的准备结果。"""

    messages: list[dict]
    compressed: bool = False
    summarized_messages: int = 0
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0


class ContextWindowManager:
    """每轮请求前的前置检查：超线就把旧轮次摘要掉。"""

    def __init__(self, budget: ContextBudget) -> None:
        self.budget = budget

    def prepare(self, messages: list[dict], tool_definitions: list[dict] | None = None) -> PrepareResult:
        """检查并（必要时）压缩。接口待定，M2 前封版。"""
        raise NotImplementedError("接口待定")


__all__: list[Any] = ["ContextBudget", "ContextWindowManager", "PrepareResult"]
