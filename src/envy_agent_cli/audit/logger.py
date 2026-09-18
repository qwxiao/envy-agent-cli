"""审计日志：JSONL，用 trace_id 串起来。

**为什么必须是结构化 JSONL 而不是 print**：
一次任务跑 20 轮、调几十个工具，print 出来的日志糊成一片；
并发之后（只读工具并发执行）print 还会交错，根本没法看。

更重要的是——**这份日志就是 M6 评测的 Trajectory 原始数据源**。
改完 Prompt 或工具，跑回归集，从轨迹里能看出"是哪一步退化了"，
而不是只看最终答案变好还是变差。**这是生产级和 Demo 的分水岭。**
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

from envy_agent_cli.trace import TraceContext


@dataclass(slots=True)
class AuditRecord:
    """一次工具执行的审计记录（字段固定，评测要按字段消费）。"""

    ts: str
    trace_id: str
    span_id: str | None
    parent_span: str | None
    tool: str
    args_digest: str          # 参数摘要，不落全量参数（可能含敏感信息）
    status: str               # ok / error
    error_code: str | None = None
    retryable: bool = False
    attempt: int = 1
    duration_ms: float = 0.0


class AuditLogger:
    """JSONL 审计写入器。每个任务一个文件，或全局追加。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fp: IO[str] | None = None

    def record(self, record: AuditRecord) -> None:
        """追加一行 JSON。"""
        raise NotImplementedError

    def trace_lines(self, trace_id: str) -> list[AuditRecord]:
        """按 trace_id 捞出一次完整任务的执行轨迹（评测/回放用）。"""
        raise NotImplementedError

    def __enter__(self) -> "AuditLogger":
        raise NotImplementedError

    def __exit__(self, *exc) -> None:
        raise NotImplementedError


def _digest(args: dict) -> str:
    """把参数压成一行摘要（长值截断），避免审计日志被大 payload 撑爆。"""
    return json.dumps(args, ensure_ascii=False)[:200]


__all__ = ["AuditLogger", "AuditRecord", "asdict"]
