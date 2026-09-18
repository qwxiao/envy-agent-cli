"""三级链路追踪（跨层共享的叶子模块）。

| 级别 | 标识 | 对应什么 |
|---|---|---|
| 任务级 | `trace_id` | 一次完整的用户任务（run 开始到结束） |
| 轮次级 | `span_id` | ReAct 的一轮 |
| 调用级 | `parent_span` | 一次工具调用 |

**为什么分三级**：粒度不对就没法归因——只看 trace 不知道是哪一轮出的问题，
只看轮次分不清是模型的错还是工具的错。分级之后，一次失败的轨迹能精确落到
"第 3 轮第 2 个工具调用"。

**它同时是 M6 评测的地基**：trace 串起来的审计日志 = Trajectory = 评测的数据源。

> 为什么这个模块在顶层而不是 `loop/` 下：追踪上下文是**跨层共享**的——
> 循环生成它、工具执行层消费它、审计层落盘它。
> 放在 `loop/` 会让工具层反向依赖编排层（下层依赖上层），既造成循环 import，
> 也破坏了分层方向。**依赖只能自上而下。**
"""

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class TraceContext:
    """一次任务的追踪上下文。在 `run()` 开头创建，沿调用链往下传。"""

    trace_id: str
    span_id: str | None = None
    parent_span: str | None = None

    @property
    def audit_fields(self) -> dict[str, str | None]:
        """审计日志要落的那三个字段。"""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span": self.parent_span,
        }


def new_trace() -> TraceContext:
    """任务开始：生成 trace_id。格式 `trace-<12位十六进制>`。"""
    return TraceContext(trace_id=f"trace-{uuid4().hex[:12]}")


def round_span(trace: TraceContext, round_no: int) -> TraceContext:
    """每一轮一个 span，父级是任务本身。"""
    return TraceContext(
        trace_id=trace.trace_id,
        span_id=f"{trace.trace_id}:r{round_no}",
        parent_span=None,
    )


def tool_span(round_ctx: TraceContext) -> TraceContext:
    """一次工具调用一个子 span，父级是它所属的轮次。

    同一轮的多个工具调用共享轮次 `span_id`，各自的 `parent_span` 指向它；
    要区分同轮内的多次调用，看审计日志里的 `tool_call_id`。
    """
    return TraceContext(
        trace_id=round_ctx.trace_id,
        span_id=round_ctx.span_id,
        parent_span=round_ctx.span_id,
    )
