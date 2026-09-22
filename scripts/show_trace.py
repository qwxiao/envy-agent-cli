"""把一次任务的审计轨迹按时间顺序摊开——**通读整体流程的最短路径**。

比读代码快：一次真机任务的 `kind` 序列，就是"数据经过哪几站"的压缩表示。
先看轨迹拿到骨架，再带着骨架去读代码，不容易陷进局部。

用法：
    uv run python scripts/show_trace.py                 # 最后一次任务
    uv run python scripts/show_trace.py <trace_id>      # 指定任务
    uv run python scripts/show_trace.py --list          # 列出审计里所有任务
"""

import json
import sys
from collections import Counter
from pathlib import Path

AUDIT = Path("audit") / "cli.jsonl"


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def brief(record: dict) -> str:
    """一条记录压成一行：时间锚点 + 关键字段，不打印大 payload。"""
    kind = record["kind"]
    span = record.get("span_id") or "-"
    detail = record.get("detail") or {}
    if kind == "tool":
        return (f"tool      seq={record.get('seq')} {record.get('tool')} "
                f"{record.get('status')} executed={record.get('executed')} "
                f"{(record.get('args_digest') or '')[:48]}")
    if kind == "reasoning":
        return f"reasoning 推理 {detail.get('chars')} 字"
    if kind == "compact":
        return (f"compact   {detail.get('before_tokens')} → {detail.get('after_tokens')} tokens，"
                f"摘要 {detail.get('summarized_messages')} 条（{detail.get('triggered_by')}）")
    if kind == "compress_failed":
        return f"compress_failed {detail.get('engine')}: {detail.get('error')}"
    if kind == "stream_error":
        return (f"stream_error {detail.get('error_code')} "
                f"完整调用 {detail.get('complete_calls')} / 残缺 {detail.get('partial_calls')}")
    if kind == "stop":
        return (f"stop      {detail.get('stop_reason')} 轮数={detail.get('iterations')} "
                f"工具={detail.get('tool_calls')} tokens={detail.get('tokens')}")
    return kind


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows = load(AUDIT)
    if not rows:
        print(f"没有审计文件：{AUDIT}（先跑一次 envy）")
        return 1

    order: list[str] = []
    for record in rows:
        if record["trace_id"] not in order:
            order.append(record["trace_id"])

    args = sys.argv[1:]
    if args and args[0] == "--list":
        for trace_id in order:
            kinds = Counter(r["kind"] for r in rows if r["trace_id"] == trace_id)
            print(f"{trace_id}  {dict(kinds)}")
        return 0

    trace_id = args[0] if args else order[-1]
    mine = [r for r in rows if r["trace_id"] == trace_id]
    if not mine:
        print(f"审计里没有 {trace_id}")
        return 1

    print(f"trace_id = {trace_id}（{len(mine)} 条记录）")
    print("轮次      事件")
    print("-" * 78)
    # 工具记录按 seq 排（只读工具并发执行，落盘顺序不等于调用顺序）
    for record in sorted(mine, key=lambda r: (r.get("span_id") or "", r.get("seq") or 0)):
        marker = (record.get("span_id") or "").split(":")[-1]
        print(f"{marker:8s}  {brief(record)}")
    print("-" * 78)
    print("事件类型统计：", dict(Counter(r["kind"] for r in mine)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
