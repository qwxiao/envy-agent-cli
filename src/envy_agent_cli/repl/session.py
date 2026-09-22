"""REPL 主循环：把单次执行的 `run()` 串成一段会话。

**同步架构下，输入与输出是交替的，不是并发的**：

```
prompt 读输入  →  run() 流式输出  →  回到 prompt
```

这意味着 prompt-toolkit 与流式输出**天生不会打架**——读输入时没有输出，
输出时也没有输入框在等着。代价是不能"边输出边打字"。

这是刻意的取舍：异步方案能换来并发，但要引入事件循环，
与整个项目的同步生成器架构冲突；而"REPL 不许改 Loop"是硬约束。
用交替换来的简单，比用并发换来的花哨更适合这个项目的性格。

**会话历史怎么活下来**：`run()` 的返回值里带着这一轮结束时的完整 `messages`，
原样存下来，下一轮通过 `history=` 传回去。客户端只管搬运，不解析、不裁剪——
裁剪是压缩器的事（模块4），在这里动手会让两套逻辑打架。
"""

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.prompt import Prompt

from envy_agent_cli.audit.logger import AuditLogger
from envy_agent_cli.context import ContextPolicy
from envy_agent_cli.llm import ChatParams, ChatModel
from envy_agent_cli.llm.adapter import ChatMessage
from envy_agent_cli.loop import AgentResult, run
from envy_agent_cli.repl.commands import (
    COMPLETION_WORDS,
    CommandContext,
    dispatch,
)
from envy_agent_cli.repl.renderer import RichRenderer
from envy_agent_cli.tools.registry import all_names
from envy_agent_cli.tools.runtime import ConfirmFn, ToolRuntime
from envy_agent_cli.tools.spec import RegisteredTool

#: 历史文件。放在用户目录——换个项目还想翻到上次问过什么。
HISTORY_PATH = Path.home() / ".envy" / "repl_history"

PROMPT_SIGN = "❯ "


class SlashCompleter(Completer):
    """只在光标前以 `/` 开头时补全命令名。

    ⚠️ 输入里出现空格后就不再补全——否则用户打"看看 /tmp 目录"时
    会被弹出一堆命令建议，那些补全没有一个是他想要的。
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for word in COMPLETION_WORDS:
            if word.startswith(text):
                yield Completion(word, start_position=-len(text))


def repl_confirm(tool: RegisteredTool, args: dict) -> bool:
    """HITL 审批提示。**默认拒绝**——用户在提示符下敲回车不该等于放行。"""
    console = Console()
    shown = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4]) or "无参数"
    console.print(f"[yellow]需要确认[/yellow]  {tool.spec.name}({shown})")
    return Prompt.ask("  允许执行吗", choices=["y", "n"], default="n") == "y"


def build_prompt_session(history_path: Path | None = None) -> PromptSession:
    path = history_path if history_path is not None else HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(path)),
        completer=SlashCompleter(),
        complete_while_typing=True,
    )


def run_repl(
    *,
    adapter: ChatModel,
    runtime: ToolRuntime,
    tool_schemas: list[dict],
    system_prompt: str,
    params: ChatParams,
    workspace: Path,
    audit: AuditLogger | None = None,
    context_policy: ContextPolicy | None = None,
    show_reasoning: bool = False,
    console: Console | None = None,
    session: PromptSession | None = None,
) -> int:
    """交互式会话。返回进程退出码。

    `runtime` 由调用方构造（它需要带着 REPL 版的 `confirm` 回调），
    这里只负责"读输入 → 跑任务 → 渲染 → 再读"。
    """
    out = console if console is not None else Console()
    renderer = RichRenderer(out, show_reasoning=show_reasoning)
    prompt_session = session if session is not None else build_prompt_session()
    messages: list[ChatMessage] = []

    out.print(f"[bold]envy[/bold] · [cyan]{adapter.name}[/cyan]/{adapter.model or '默认'} · {workspace}")
    out.print("[dim]输入任务开始；/help 看命令，Ctrl-D 退出。[/dim]\n")

    while True:
        try:
            line = prompt_session.prompt(PROMPT_SIGN)
        except KeyboardInterrupt:
            continue            # Ctrl-C 只取消这一次输入，不该把会话弄没
        except EOFError:
            break               # Ctrl-D 才是退出

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            outcome = dispatch(line, _context(adapter, workspace, messages))
            if outcome.message:
                renderer.print_plain(outcome.message)
            if outcome.should_clear:
                messages = []
            if outcome.should_exit:
                break
            continue

        try:
            result = run(line,
                         history=messages,
                         adapter=adapter,
                         runtime=runtime,
                         tool_schemas=tool_schemas,
                         render=renderer,
                         params=params,
                         system_prompt=system_prompt,
                         audit=audit,
                         context_policy=context_policy)
        except KeyboardInterrupt:
            # ⚠️ 中断发生在 run() 内部时，历史可能停在"assistant 声明了工具调用
            # 但没有对应 tool 结果"的半截状态——那种历史发出去协议会报错。
            # 所以这一轮整个丢弃，不写回 messages（宁可丢一轮，不可坏一段历史）。
            renderer.print_plain("\n（已中断本轮，历史未改变）", style="yellow")
            continue

        messages = list(result.messages)
        renderer.flush()
        _print_stats(out, result)

    out.print("[dim]再见。[/dim]")
    return 0


def _context(adapter: ChatModel, workspace: Path, messages: list[ChatMessage]) -> CommandContext:
    return CommandContext(
        provider=adapter.name,
        model=getattr(adapter, "model", "") or "",
        workspace=str(workspace),
        tool_names=all_names(),
        message_count=len(messages),
    )


def _print_stats(console: Console, result: AgentResult) -> None:
    """一轮结束后的统计行。**挂在正文下面、灰一点**——它是元信息不是产出。"""
    tokens = result.usage.total_tokens if result.usage else 0
    parts = [f"轮数 {result.iterations}", f"工具 {result.tool_calls_total}", f"tokens {tokens}"]
    if result.context_events:
        parts.append(f"压缩 {len(result.context_events)} 次")
    if result.truncated:
        parts.append(f"[yellow]{result.stop_reason}[/yellow]")
    if result.detail:
        parts.append(result.detail)
    console.print(f"[dim]── {' · '.join(parts)}[/dim]\n")


__all__ = ["PROMPT_SIGN", "SlashCompleter", "build_prompt_session", "repl_confirm", "run_repl"]
