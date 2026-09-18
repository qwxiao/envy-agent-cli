"""渲染层：把"给人看"的动作从编排逻辑里拆出去。

为什么单独一层：**print 是渲染策略，不是编排逻辑**。
终端要打字机效果、日志要纯文本、以后接 UI 要事件——渲染注定多变，
焊死在编排层里等于把展示方式写进了业务逻辑。

编排层只负责"决定说什么"，渲染层负责"怎么说"。测试里注入一个记录用的渲染器，
就能断言"循环说了什么"而不捕获 stdout。
"""

import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    """编排层对渲染层的全部要求。"""

    def text(self, chunk: str) -> None:
        """正文增量（模型的答案，是产出）。"""
        ...

    def reasoning(self, chunk: str) -> None:
        """思维链增量（推理过程，是过程证据，不是答案）。"""
        ...

    def notice(self, message: str) -> None:
        """运行提示：触顶告警、纠错重试、降级等。不是答案，也不是错误。"""
        ...


class ConsoleRenderer:
    """终端渲染：正文走 stdout（重定向时答案干净），其余走 stderr。"""

    def __init__(self, show_reasoning: bool = True) -> None:
        self.show_reasoning = show_reasoning

    def text(self, chunk: str) -> None:
        print(chunk, end="", flush=True)

    def reasoning(self, chunk: str) -> None:
        if self.show_reasoning:
            print(chunk, end="", file=sys.stderr, flush=True)

    def notice(self, message: str) -> None:
        print(f"\n[{message}]", file=sys.stderr, flush=True)


class NullRenderer:
    """什么都不做：批量跑任务 / 测试时用。"""

    def text(self, chunk: str) -> None:
        pass

    def reasoning(self, chunk: str) -> None:
        pass

    def notice(self, message: str) -> None:
        pass
