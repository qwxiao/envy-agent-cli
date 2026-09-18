"""三级链路追踪（跨层共享的叶子模块）。

三级粒度：

    trace_id                    ← 一次任务（run(question) 开始到结束，跨轮次复用）
      └─ span_id                ← 一轮（第 N 轮）
           └─ parent_span_id    ← 一次调用（第 N 轮的第 M 个工具）

**为什么分三级**：粒度不对就没法归因——只看 trace 级不知道是哪一轮出的问题，
只看轮次级分不清是模型的错还是工具的错。分级之后，一次失败的轨迹能精确落到
"第 3 轮第 2 个工具调用"。

**它同时是 M6 评测的地基**：trace 串起来的审计日志 = Trajectory = 评测的数据源。
没有 trace 就没有 Trajectory，评测就只能"看最终答案好不好"，退化成 Demo 级。

传递链路：**Loop 生成 → Adapter 消费（打 span、记耗时）→ Runtime 消费（写审计）**。

三条防"上帝模块"的约束（设计方定的，改动时别破）：
1. **无依赖**——只 import 标准库，不 import 项目内任何模块；
2. **纯数据**——`TraceContext` 是 frozen dataclass，不含行为、不做 IO；
3. **有守卫**——`tests/test_skeleton.py` 里有"分层方向"测试盯着它。

> 为什么放在顶层而不是 `loop/` 下：追踪上下文跨层共享（循环生成 / 工具消费 / 审计落盘），
> 放 `loop/` 会逼着工具层反向依赖编排层——既造成循环 import，也破坏分层方向。
"""

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class TraceContext:
    """一次任务的追踪上下文。在 `run()` 开头创建，沿调用链往下传。"""

    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None

    @property
    def audit_fields(self) -> dict[str, str | None]:
        """审计日志要落的那三个字段。"""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
        }


def new_trace() -> TraceContext:
    """任务开始：生成 trace_id。格式 `trace-<12位十六进制>`。"""
    return TraceContext(trace_id=f"trace-{uuid4().hex[:12]}")


def round_span(trace: TraceContext, round_no: int) -> TraceContext:
    """每一轮一个 span，父级是任务本身。"""
    return TraceContext(
        trace_id=trace.trace_id,
        span_id=f"{trace.trace_id}:r{round_no}",
        parent_span_id=None,
    )


def tool_span(round_ctx: TraceContext) -> TraceContext:
    """一次工具调用一个子 span，父级是它所属的轮次。

    同一轮的多个工具调用共享轮次 `span_id`，各自的 `parent_span_id` 指向它；
    要区分同轮内的多次调用，看审计日志里的 `tool_call_id`。
    """
    return TraceContext(
        trace_id=round_ctx.trace_id,
        span_id=round_ctx.span_id,
        parent_span_id=round_ctx.span_id,
    )
