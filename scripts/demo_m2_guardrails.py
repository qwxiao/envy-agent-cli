"""场景验证台：把模块2 的四类护栏逐条跑给你看。

**为什么需要它**：预算耗尽、原地打转、输出不合契约、流中断这些路径，
用真实模型**没法按需触发**（你没法让模型故意死循环）。所以这里用脚本化的假适配器
按剧本吐事件，把每条护栏的真实行为（终止原因、`detail`、注入的消息、审计记录）打出来。

用法：
    uv run python scripts/demo_m2_guardrails.py                 # 跑全部场景
    uv run python scripts/demo_m2_guardrails.py no_progress     # 只跑一个
    uv run python scripts/demo_m2_guardrails.py --list          # 列出场景

真机验证（真实模型 + 多轮工具调用）请用 scripts/demo_m2_loop.py。
"""

import sys
from pathlib import Path

from envy_agent_cli.audit.logger import AuditLogger
from envy_agent_cli.llm.events import (
    Error,
    LLMErrorCode,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from envy_agent_cli.llm.params import ChatParams
from envy_agent_cli.llm.validator import OutputContract
from envy_agent_cli.loop import Budget, NoProgressPolicy, run
from envy_agent_cli.loop.renderer import NullRenderer
from envy_agent_cli.tools.result import ToolResult

AUDIT_DIR = Path("audit")


# ---------------------------------------------------------------- 假件

class ScriptedAdapter:
    """按剧本吐事件。剧本是"每轮的事件列表"，轮数超出后重复最后一轮。"""

    name = "scripted"

    def __init__(self, rounds: list[list]):
        self.rounds = rounds
        self.round = 0

    def stream_chat(self, messages, tools=None, params=ChatParams(), trace=None):
        events = self.rounds[min(self.round, len(self.rounds) - 1)]
        self.round += 1
        yield from events


class ScriptedRuntime:
    """按名字派发的假执行器：`behavior` 里是 name -> 处理函数。抛异常 = 工具失败。"""

    def __init__(self, behavior: dict):
        self.behavior = behavior
        self.seen: list[str] = []

    def execute_all(self, calls, trace):
        out = []
        for call in calls:
            self.seen.append(call.get("name") or "?")
            if call.get("parse_error"):
                out.append(ToolResult(content=f"参数不是合法 JSON：{call['parse_error']}",
                                      is_error=True, tool_call_id=call["id"], executed=False))
                continue
            handler = self.behavior.get(call.get("name"))
            if handler is None:
                out.append(ToolResult(content=f'工具 "{call.get("name")}" 不存在',
                                      is_error=True, tool_call_id=call["id"], executed=False))
                continue
            try:
                out.append(ToolResult(content=str(handler(call.get("arguments") or {})),
                                      tool_call_id=call["id"]))
            except Exception as exc:
                out.append(ToolResult(content=f"{type(exc).__name__}: {exc}",
                                      is_error=True, tool_call_id=call["id"]))
        return out


class TraceRenderer(NullRenderer):
    """把渲染内容收集起来，跑完一次性打出来（比流式更能看清）。"""

    def __init__(self):
        self.notices: list[str] = []
        self.texts: list[str] = []
        self.reasonings: list[str] = []

    def text(self, chunk: str) -> None:
        self.texts.append(chunk)

    def reasoning(self, chunk: str) -> None:
        self.reasonings.append(chunk)

    def notice(self, message: str) -> None:
        self.notices.append(message)


# ---------------------------------------------------------------- 事件小工具

def txt(s: str) -> TextDelta:
    return TextDelta(s)


def end(reason: str = "end_turn") -> MessageEnd:
    return MessageEnd(reason)


def call(name: str, args: str, index: int = 0, call_id: str | None = None) -> list:
    """一段工具调用碎片：第一片带 id/name，第二片补参数——和真实厂商一致。"""
    return [
        ToolCallDelta(index=index, is_first=True, call_id=call_id or f"c{index}",
                      name=name, arguments=args[:4]),
        ToolCallDelta(index=index, is_first=False, arguments=args[4:]),
    ]


def tool_round(name="lookup", args='{"id": "a"}', index=0) -> list:
    return [*call(name, args, index), end("tool_use")]


CONTRACT = OutputContract("answer", {"type": "object", "required": ["summary"]})


# ---------------------------------------------------------------- 场景

def scenario_happy() -> tuple:
    """正常流程：一轮工具 + 一轮收尾，看 AgentResult 的完整字段。"""
    return (
        ScriptedAdapter([
            [txt("我先查一下。"), *tool_round(), end("tool_use")],
            [txt("查到了：a 的值是 42。"), Usage(100, 20, 120), end()],
        ]),
        ScriptedRuntime({"lookup": lambda a: "42"}),
        {},
    )


def scenario_tool_error_recovery() -> tuple:
    """工具报错 → 模型读到错误后换策略：这是"失败驱动自我修正"的最小复现。"""
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("没有这条记录：a")
        return "42"

    return (
        ScriptedAdapter([
            [*call("lookup", '{"id": "a"}'), end("tool_use")],
            [txt("刚才的 id 不对，我换一个。"), *call("lookup", '{"id": "b"}'), end("tool_use")],
            [txt("查到了：42。"), end()],
        ]),
        ScriptedRuntime({"lookup": flaky}),
        {},
    )


def scenario_no_progress() -> tuple:
    """原地打转：第 2 轮先注入提示，第 3 轮仍相同才终止。"""
    return (
        ScriptedAdapter([tool_round()] * 6),
        ScriptedRuntime({"lookup": lambda a: "42"}),
        {},
    )


def scenario_repeated_but_changing() -> tuple:
    """调用相同但结果在变 → 只提示，不终止（两级指纹的意义）。"""
    counter = {"n": 0}

    def ticking(args):
        counter["n"] += 1
        return f"结果-{counter['n']}"

    return (
        ScriptedAdapter([tool_round()] * 6),
        ScriptedRuntime({"lookup": ticking}),
        {"budget": Budget(max_rounds=4)},
    )


def scenario_budget_rounds() -> tuple:
    return (ScriptedAdapter([tool_round(args=f'{{"id": "{i}"}}') for i in range(9)]),
            ScriptedRuntime({"lookup": lambda a: "42"}),
            {"budget": Budget(max_rounds=3)})


def scenario_budget_tokens() -> tuple:
    """每轮参数不同——否则会先被打转检测拦下，测不到 token 预算这条路径。"""
    return (ScriptedAdapter([
        [*call("lookup", f'{{"id": "{i}"}}'), Usage(0, 0, 100), end("tool_use")]
        for i in range(9)
    ]),
        ScriptedRuntime({"lookup": lambda a: "42"}),
        {"budget": Budget(max_tokens=150)})


def scenario_budget_tool_calls() -> tuple:
    return (ScriptedAdapter([[
        *call("lookup", f'{{"id": "{n}"}}', index=0),
        *call("lookup", f'{{"id": "{n}-2"}}', index=1),
        end("tool_use")] for n in range(9)]),
        ScriptedRuntime({"lookup": lambda a: "42"}),
        {"budget": Budget(max_tool_calls=2)})


def scenario_budget_duration() -> tuple:
    return (ScriptedAdapter([tool_round()]),
            ScriptedRuntime({"lookup": lambda a: "42"}),
            {"budget": Budget(max_duration_s=-1)})


def scenario_contract_correction() -> tuple:
    """输出不合契约 → 注入纠错提示 → 第二次合规。"""
    return (
        ScriptedAdapter([
            [txt('{"other": 1}'), end()],
            [txt('{"summary": "合规了"}'), end()],
        ]),
        ScriptedRuntime({}),
        {"output_contract": CONTRACT},
    )


def scenario_contract_budget_exhausted() -> tuple:
    """一直不合规 → 独立预算耗尽，归因为 output_contract_failed（不是 max_rounds）。"""
    return (
        ScriptedAdapter([[txt('{"other": 1}'), end()]]),
        ScriptedRuntime({}),
        {"output_contract": CONTRACT, "max_corrections": 2, "budget": Budget(max_rounds=20)},
    )


def scenario_stream_error() -> tuple:
    """流中途断：不入半截历史，但审计要记下"这轮产出了几个调用、几个残缺"。"""
    return (
        ScriptedAdapter([[
            txt("我开始查"),
            *call("lookup", '{"id": "a"}', index=0),
            ToolCallDelta(index=1, is_first=True, call_id="c1", name="lookup",
                          arguments='{"id":  残缺'),
            Error("连接被重置", LLMErrorCode.SERVER_ERROR, retryable=True),
        ]]),
        ScriptedRuntime({"lookup": lambda a: "42"}),
        {},
    )


def scenario_parse_error_call() -> tuple:
    """参数不是合法 JSON：仍进批次（记账 + 让模型看到），但标记为未执行。"""
    return (
        ScriptedAdapter([
            [*call("lookup", "{这不是 JSON"), end("tool_use")],
            [txt("我换个写法。"), end()],
        ]),
        ScriptedRuntime({"lookup": lambda a: "42"}),
        {},
    )


def scenario_custom_policy() -> tuple:
    """护栏参数可配：warn_after=1 / stop_after=2。"""
    return (ScriptedAdapter([tool_round()] * 5),
            ScriptedRuntime({"lookup": lambda a: "42"}),
            {"no_progress": NoProgressPolicy(warn_after=1, stop_after=2)})


SCENARIOS = {
    "happy": scenario_happy,
    "tool_error_recovery": scenario_tool_error_recovery,
    "no_progress": scenario_no_progress,
    "repeated_but_changing": scenario_repeated_but_changing,
    "budget_rounds": scenario_budget_rounds,
    "budget_tokens": scenario_budget_tokens,
    "budget_tool_calls": scenario_budget_tool_calls,
    "budget_duration": scenario_budget_duration,
    "contract_correction": scenario_contract_correction,
    "contract_budget_exhausted": scenario_contract_budget_exhausted,
    "stream_error": scenario_stream_error,
    "parse_error_call": scenario_parse_error_call,
    "custom_policy": scenario_custom_policy,
}


# ---------------------------------------------------------------- 执行

def play(name: str) -> None:
    adapter, runtime, kwargs = SCENARIOS[name]()
    renderer = TraceRenderer()
    audit = AuditLogger(AUDIT_DIR / f"scenario-{name}.jsonl")

    result = run("帮我查一下 a 的值", adapter=adapter, runtime=runtime,
                 render=renderer, audit=audit, **kwargs)

    print(f"\n{'=' * 72}\n【{name}】")
    print(f"  终止：stop_reason={result.stop_reason}  truncated={result.truncated}  "
          f"轮数={result.iterations}")
    if result.detail:
        print(f"  detail：{result.detail}")
    print(f"  消息角色：{[m['role'] for m in result.messages]}")
    print(f"  工具调用 {result.tool_calls_total} 次：{runtime.seen or '（无）'}")
    if result.usage:
        print(f"  累计 token：{result.usage.total_tokens}")
    if renderer.notices:
        print(f"  运行提示：{renderer.notices}")
    if renderer.texts and name in {"happy", "tool_error_recovery", "contract_correction"}:
        print(f"  最终回答：{''.join(renderer.texts)[:120]}")
    if name == "contract_correction":
        hint = [m for m in result.messages if m["role"] == "user"][1]["content"]
        print(f"  注入的纠错提示：{hint.splitlines()[0]}")

    kinds: dict[str, int] = {}
    for record in audit.trace_lines(result.trace_id):
        kinds[record.kind] = kinds.get(record.kind, 0) + 1
    print(f"  审计：{audit.path.as_posix()}（{kinds}）")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = sys.argv[1:]
    if args and args[0] == "--list":
        print("可用场景：")
        for name in SCENARIOS:
            print(f"  {name}")
        return 0

    targets = args or list(SCENARIOS)
    for name in targets:
        if name not in SCENARIOS:
            print(f"未知场景：{name}（用 --list 看全部）")
            return 2
        play(name)
    print(f"\n全部完成。审计明细在 {AUDIT_DIR.as_posix()}/ 下，按场景一个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
