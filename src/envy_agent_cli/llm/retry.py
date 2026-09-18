"""模块1 · 重试策略（独立类，跨 Adapter 复用）。

**为什么独立成类而不是塞进工厂**：重试策略跟厂商无关。放工厂里会导致每个厂商
实现一份；独立出来，所有 Adapter 共用同一套退避规则。

有边界重试 = **最大次数 + 指数退避 + jitter + 降级**，四者缺一不可：
- 缺次数限制 → 任务无限等待、成本失控；
- 缺退避 → 对已经过载的服务二次伤害；
- 缺 jitter → 多个任务同时重试形成惊群（thundering herd）；
- 缺降级 → 没有兜底路径（换模型 / 缩上下文 / 转人工）。

分层归属（别搞混）：
- **Adapter**：纯翻译，不重试；
- **工厂**：路由，选哪个 Adapter；
- **RetryPolicy**（本模块）：重试，读 `Error.retryable` 决定再试与退避；
- **Loop**：降级，重试预算耗尽后决定下一步。

⚠️ 两条容易踩的坑：
1. **超时 ≠ 执行失败**——超时只说明没拿到结果，不代表动作没执行；
2. **非幂等的写操作超时后不能盲目重试**——可能已经执行过了。
"""

import random
import time
from dataclasses import dataclass
from typing import Callable, Iterator, TypeVar

from envy_agent_cli.llm.events import AnyEvent, Error

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """退避规则。默认：3 次尝试、0.5s 起、指数增长、±25% 抖动、封顶 20s。"""

    max_attempts: int = 3
    base_delay: float = 0.5
    backoff_factor: float = 2.0
    max_delay: float = 20.0
    jitter_ratio: float = 0.25

    def should_retry(self, error: Error, attempt: int) -> bool:
        """能不能再试一次。**只看 Adapter 给出的 `retryable`**，不推导、不匹配字符串。"""
        return error.retryable and attempt < self.max_attempts

    def delay_for(self, attempt: int) -> float:
        """第 `attempt` 次失败后该等多久（attempt 从 1 开始）。"""
        raw = self.base_delay * (self.backoff_factor ** (attempt - 1))
        raw = min(raw, self.max_delay)
        jitter = raw * self.jitter_ratio
        return max(0.0, raw + random.uniform(-jitter, jitter))

    def retry_stream(
        self,
        open_stream: Callable[[], Iterator[AnyEvent]],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Iterator[AnyEvent]:
        """对流式调用做**有边界**重试。

        关键判断：**只有"还没吐出任何事件就失败"才允许重试**。
        流已经吐了一半再重来，会让上层收到重复的正文/工具碎片（等价于"非幂等操作盲目重试"），
        所以那种情况直接把 Error 交给上层，由 Loop 决定降级。
        """
        attempt = 1
        while True:
            emitted = False
            error: Error | None = None

            for event in open_stream():
                if isinstance(event, Error):
                    error = event
                    break
                emitted = True
                yield event

            if error is None:
                return
            if emitted or not self.should_retry(error, attempt):
                yield error
                return
            sleep(self.delay_for(attempt))
            attempt += 1
