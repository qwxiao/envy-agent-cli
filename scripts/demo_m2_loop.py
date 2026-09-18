"""手动冒烟：连真实模型跑一次多轮工具调用，验证模块2 的 ReAct 主循环。

单元测试用的是脚本化的假适配器（确定性、离线）；这个脚本补的是"真网络 + 真模型 +
真工具调用往返"那一段——模型会不会真的按 schema 发起调用、多轮往返能不能收敛。

工具执行用**内存版执行器**（读一个字典，不碰文件系统），因为 `ToolRuntime` 属于模块3、
目前还是桩。这里只借它验证"循环能不能把工具结果正确地喂回模型"。

用法：
    uv run python scripts/demo_m2_loop.py                 # 默认厂商
    uv run python scripts/demo_m2_loop.py glm             # 指定厂商
    uv run python scripts/demo_m2_loop.py deepseek "帮我看下有哪些笔记，再把 ReAct 那条读出来"
"""

import sys
from pathlib import Path

from envy_agent_cli.audit.logger import AuditLogger
from envy_agent_cli.config import load_settings
from envy_agent_cli.llm import ChatParams, build
from envy_agent_cli.loop import ConsoleRenderer, run
from envy_agent_cli.tools.registry import register, to_model_schemas
from envy_agent_cli.tools.result import ToolResult
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec

DEFAULT_QUESTION = "先看看有哪些笔记，然后把关于 ReAct 的那条读给我。"

#: 内存里的"笔记库"——演示用，不碰磁盘
NOTES = {
    "ReAct": "ReAct = 推理 + 行动交替：先想下一步做什么，再调工具，再根据结果继续想。",
    "SSE": "SSE 用空行分隔事件块，且可能跨多个网络分片到达，所以必须按协议分帧。",
    "契约": "工具的三个契约：模型契约、执行契约、注册契约，各自暴露的信息面不同。",
}


def list_notes() -> str:
    return "、".join(NOTES)


def read_note(name: str) -> str:
    if name not in NOTES:
        raise KeyError(f"没有这条笔记：{name}（可用：{'、'.join(NOTES)}）")
    return NOTES[name]


def register_demo_tools() -> None:
    register(RegisteredTool(
        spec=ToolSpec(name="list_notes", description="列出所有笔记标题。",
                      input_model={"type": "object", "properties": {}},
                      permission=Permission(), risk=Risk.LOW),
        handler=list_notes, read_only=True))
    register(RegisteredTool(
        spec=ToolSpec(name="read_note", description="按标题读取一条笔记的正文。",
                      input_model={"type": "object",
                                   "properties": {"name": {"type": "string"}},
                                   "required": ["name"]},
                      permission=Permission(), risk=Risk.LOW),
        handler=read_note, read_only=True))


class InMemoryRuntime:
    """模块3 落地前的最小执行器替身：查表 → 调函数 → 归一化结果。

    故意保持极简（没有并发分组、没有 HITL、没有审计），那些是 ToolRuntime 的活。
    它对内只保证一件事：**任何失败都变成一条结果，不抛给主循环**。
    """

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def execute_all(self, calls: list[dict], trace) -> list[ToolResult]:
        from envy_agent_cli.tools.registry import get

        results = []
        for call in calls:
            self.seen.append(call)
            tool = get(call.get("name") or "")
            if call.get("parse_error"):
                results.append(ToolResult(content=f"参数不是合法 JSON：{call['parse_error']}",
                                          is_error=True, tool_call_id=call["id"]))
                continue
            if tool is None:
                results.append(ToolResult(content=f'工具 "{call.get("name")}" 不存在',
                                          is_error=True, tool_call_id=call["id"]))
                continue
            try:
                content = tool.handler(**(call.get("arguments") or {}))
                results.append(ToolResult(content=str(content), tool_call_id=call["id"]))
            except Exception as exc:                      # 失败也是 observation
                results.append(ToolResult(content=f"{type(exc).__name__}: {exc}",
                                          is_error=True, tool_call_id=call["id"]))
        return results


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    argv = sys.argv[1:]
    provider = argv[0] if argv else None
    question = argv[1] if len(argv) > 1 else DEFAULT_QUESTION

    try:
        settings = load_settings(provider=provider)
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 2

    adapter = build(settings.provider, settings.api_key,
                    model=settings.model, base_url=settings.base_url)
    register_demo_tools()
    runtime = InMemoryRuntime()

    print(f"=== {adapter.name} · {adapter.model} ===")
    print(f"问题：{question}\n--- 模型输出 ---")

    audit = AuditLogger(Path("audit") / "demo_m2.jsonl")
    result = run(question, adapter=adapter, runtime=runtime,
                 tool_schemas=to_model_schemas(),
                 render=ConsoleRenderer(),
                 params=ChatParams(max_tokens=1024),
                 audit=audit)

    print("\n--- 结果 ---")
    print(f"轮数={result.iterations}  stop_reason={result.stop_reason}  结果完整={not result.truncated}")
    print(f"trace_id={result.trace_id}")
    print(f"工具调用 {result.tool_calls_total} 次：{[c.get('name') for c in runtime.seen]}")
    print(f"消息角色序列：{[m['role'] for m in result.messages]}")

    records = audit.trace_lines(result.trace_id)
    kinds: dict[str, int] = {}
    for record in records:
        kinds[record.kind] = kinds.get(record.kind, 0) + 1
    print(f"审计落盘：{audit.path}（本次任务 {len(records)} 条，按类型 {kinds}）")
    return 1 if result.truncated else 0


if __name__ == "__main__":
    sys.exit(main())
