"""审计日志：JSONL，用 trace_id 串起来。

**为什么必须是结构化 JSONL 而不是 print**：
一次任务跑 20 轮、调几十个工具，print 出来的日志糊成一片；
并发之后（只读工具并发执行）print 还会交错，根本没法看。

更重要的是——**这份日志是回放与回归评测的 Trajectory 原始数据源**。
改完 Prompt 或工具，跑回归集，能从轨迹里看出"是哪一步退化了"，
而不是只看最终答案变好还是变差。

**记录分两类，用 `kind` 区分**：
- `kind="tool"`：一次工具执行（由 `ToolRuntime` 写）
- `kind="reasoning"` / `"round"` / `"stream_error"` / `"stop"`：编排层事件（由 Loop 写）

审计要能回答的问题包括："模型产生过几次非法调用""这一轮有几个调用是残缺的"
"推理摘要是什么""为什么终止"——这些数字评测要用，丢了就补不回来。
"""

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO

from envy_agent_cli.trace import TraceContext


@dataclass(slots=True)
class AuditRecord:
    """一条审计记录。

    工具执行类记录填 `tool` / `args_digest` / `status` / `executed` 等字段；
    编排层事件类记录把载荷放进 `detail`（推理文本、调用计数、终止原因…）。
    """

    kind: str                                # tool | reasoning | round | stream_error | stop
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    # —— kind="tool" 用 ——
    tool: str | None = None
    seq: int | None = None                   # 全局调用序号：并发执行时靠它还原真实调用次序
    args_digest: str | None = None
    status: str = "ok"                       # ok | error
    error_code: str | None = None
    retryable: bool = False
    executed: bool = True                    # ⭐ 区分"执行了但失败"与"根本没执行"
    attempt: int = 1
    duration_ms: float = 0.0

    # —— 兜底载荷 ——
    detail: dict | None = None

    def to_json(self) -> str:
        import json
        return json.dumps(asdict(self), ensure_ascii=False)


class AuditLogger:
    """JSONL 审计写入器。一个任务一个文件，或全局追加。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fp: IO[str] | None = None

    def record(self, record: AuditRecord) -> None:
        """追加一行 JSON。写盘失败不该让任务崩——审计是旁路，不是主链路。"""
        if self._fp is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = self.path.open("a", encoding="utf8")
        self._fp.write(record.to_json() + "\n")
        self._fp.flush()

    def event(self, kind: str, trace: TraceContext, **detail) -> None:
        """编排层事件的便捷写法：`kind` + trace 三件套 + 任意 detail。"""
        self.record(AuditRecord(
            kind=kind,
            trace_id=trace.trace_id,
            span_id=trace.span_id,
            parent_span_id=trace.parent_span_id,
            detail=detail or None,
        ))

    def trace_lines(self, trace_id: str) -> list[AuditRecord]:
        """按 trace_id 捞出一次完整任务的执行轨迹（回放 / 评测用）。"""
        import json
        if not self.path.is_file():
            return []
        out: list[AuditRecord] = []
        for line in self.path.read_text(encoding="utf8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("trace_id") != trace_id:
                continue
            known = {f for f in AuditRecord.__dataclass_fields__}
            out.append(AuditRecord(**{k: v for k, v in payload.items() if k in known}))
        return out

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def digest(args: dict | None, limit: int = 200) -> str:
    """把参数压成一行摘要（长值截断），避免审计日志被大 payload 撑爆。"""
    import json
    return json.dumps(args or {}, ensure_ascii=False)[:limit]
