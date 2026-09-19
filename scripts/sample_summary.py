"""跑一次真实长任务，把最长的一条真实摘要原样导出——用于人工核对"压缩后有没有失忆"。

用法：
    uv run python scripts/sample_summary.py <输出文件> [问题]
"""

import sys
from pathlib import Path

from envy_agent_cli.context import ContextBudget, ContextWindowManager, RuleSummaryEngine
from envy_agent_cli.context.compactor import SUMMARY_TAG
from envy_agent_cli.loop import run
from envy_agent_cli.loop.renderer import NullRenderer
from envy_agent_cli.tools.builtin import register_builtin_tools
from envy_agent_cli.tools.registry import to_model_schemas
from envy_agent_cli.tools.runtime import ToolRuntime

DEFAULT_QUESTION = (
    "逐个读取 src/envy_agent_cli/llm/ 目录下的每一个 .py 文件（一个都不能跳过），"
    "读完后总结这一层的职责划分"
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out = Path(sys.argv[1])
    question = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_QUESTION
    window = int(sys.argv[3]) if len(sys.argv) > 3 else 12000
    min_recent = int(sys.argv[4]) if len(sys.argv) > 4 else 6

    from envy_agent_cli.config import load_settings
    from envy_agent_cli.llm import ChatParams, build

    settings = load_settings()
    real = build(settings.provider, settings.api_key,
                 model=settings.model, base_url=settings.base_url)
    seen: list[list] = []

    class Recorder:
        name = real.name
        model = real.model

        def stream_chat(self, messages, tools=None, params=ChatParams(), trace=None):
            seen.append([dict(m) for m in messages])
            yield from real.stream_chat(messages, tools=tools, params=params, trace=trace)

    register_builtin_tools(Path.cwd())
    result = run(question, adapter=Recorder(),
                 runtime=ToolRuntime(workspace=Path.cwd(), hitl_mode="never"),
                 tool_schemas=to_model_schemas(), render=NullRenderer(),
                 params=ChatParams(max_tokens=2048), system_prompt="你是终端编程助手。",
                 context_policy=ContextWindowManager(
                     ContextBudget(context_window=window, max_output_tokens=2048,
                                   min_recent_messages=min_recent),
                     summary_engine=RuleSummaryEngine()))

    grew = [e for e in result.context_events if e.after_tokens > e.before_tokens]
    lines = [
        "# M4 真实摘要样本（供人工核对「压缩后有没有失忆」）",
        "",
        f"> 厂商 {settings.provider} · 模型 {settings.model} · 窗口 {window} · 保留区 {min_recent} 条",
        f"> 任务：{question}",
        f"> 结果：stop={result.stop_reason} 轮数={result.iterations} "
        f"工具调用={result.tool_calls_total} 压缩={len(result.context_events)}次 "
        f"压完变大的次数={len(grew)}",
        "",
        "## 各次压缩的 before → after",
        "",
    ]
    for event in result.context_events:
        lines.append(f"- `{event.span_id}` {event.before_tokens} → {event.after_tokens} tokens，"
                     f"摘要 {event.summarized_messages} 条（{event.triggered_by}）")
    best = max((str(m.get("content") or "") for msgs in seen for m in msgs
                if SUMMARY_TAG in str(m.get("content") or "")), key=len, default="(本次没有触发压缩)")
    lines += ["", "## 最长的一条真实摘要（原文照贴，未加工）", "", "```", best, "```", ""]
    out.write_text("\n".join(lines), encoding="utf8")
    print(f"样本已写入 {out}（长度 {len(best)}）")
    print(f"压缩 {len(result.context_events)} 次，压完变大的 {len(grew)} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
