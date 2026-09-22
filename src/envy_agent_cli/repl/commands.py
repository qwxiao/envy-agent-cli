"""REPL 斜杠命令。

**命令是客户端的事**——Loop 完全不知道它们存在。每条命令只做两件事之一：
给用户回一段话，或者改变客户端状态（清空历史、退出）。
**不碰 Agent 的执行逻辑**，那是 `run()` 的地盘。

这是"REPL 不许改 Loop"那条底线的另一半：不是 Loop 不能有输入参数，
而是**客户端的交互概念不许渗进编排层**。
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass(slots=True)
class CommandContext:
    """命令执行时能看到的东西。**只读**——命令靠返回值改变状态，不直接改动它。"""

    provider: str = ""
    model: str = ""
    workspace: str = ""
    tool_names: list[str] = field(default_factory=list)
    message_count: int = 0
    memory_count: int = 0
    memory_path: str = ""


@dataclass(slots=True)
class CommandOutcome:
    """命令的结果。三种都是显式的，没有"返回 None 表示退出"这种隐式约定。"""

    message: str = ""
    should_exit: bool = False
    should_clear: bool = False


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    handler: Callable[[CommandContext], CommandOutcome]


def _help(ctx: CommandContext) -> CommandOutcome:
    lines = ["可用命令："]
    lines.extend(f"  /{name:<8} {cmd.summary}" for name, cmd in sorted(COMMANDS.items()))
    lines.append("")
    lines.append("其余输入会被当作任务交给模型。Ctrl-C 取消当前输入，Ctrl-D 退出。")
    return CommandOutcome(message="\n".join(lines))


def _exit(ctx: CommandContext) -> CommandOutcome:
    return CommandOutcome(should_exit=True)


def _clear(ctx: CommandContext) -> CommandOutcome:
    """清空的是**会话历史**，不是屏幕。"""
    return CommandOutcome(
        message=f"已清空会话历史（原有 {ctx.message_count} 条消息）。",
        should_clear=True,
    )


def _tools(ctx: CommandContext) -> CommandOutcome:
    if not ctx.tool_names:
        return CommandOutcome(message="当前没有注册任何工具。")
    lines = [f"当前可用工具（{len(ctx.tool_names)} 个）："]
    lines.extend(f"  {name}" for name in ctx.tool_names)
    return CommandOutcome(message="\n".join(lines))


def _model(ctx: CommandContext) -> CommandOutcome:
    lines = [
        f"厂商：{ctx.provider or '（未设置）'}",
        f"模型：{ctx.model or '（用厂商默认）'}",
        f"工作区：{ctx.workspace or '（未设置）'}",
    ]
    return CommandOutcome(message="\n".join(lines))


def _history(ctx: CommandContext) -> CommandOutcome:
    """会话有多长——判断"该不该 /clear 重开"的依据。"""
    if not ctx.message_count:
        return CommandOutcome(message="当前会话还没有历史（这是一句新对话）。")
    return CommandOutcome(
        message=f"当前会话历史：{ctx.message_count} 条消息"
                f"（含系统提示与工具结果）。清空请用 /clear。"
    )


def _memory(ctx: CommandContext) -> CommandOutcome:
    """长期记忆的规模与位置——排查"为什么没召回"时先看这两个数。"""
    if not ctx.memory_path:
        return CommandOutcome(
            message="本次启动没有启用长期记忆（--no-memory），"
                    "或未安装记忆模块。"
        )
    lines = [
        f"长期记忆：{ctx.memory_count} 条（当前项目 scope）",
        f"库文件：{ctx.memory_path}",
        "",
        "记忆按 scope 隔离，跨项目共享的那部分要显式 include_global 才查得到。",
    ]
    return CommandOutcome(message="\n".join(lines))


#: 命令表。键是不带斜杠的名字——补全与派发都以它为准。
COMMANDS: dict[str, Command] = {
    "help": Command("help", "显示这条帮助", _help),
    "exit": Command("exit", "退出（同 /quit）", _exit),
    "quit": Command("quit", "退出（同 /exit）", _exit),
    "clear": Command("clear", "清空会话历史，开始新对话", _clear),
    "history": Command("history", "查看当前会话有多长", _history),
    "tools": Command("tools", "列出当前可用工具", _tools),
    "memory": Command("memory", "查看长期记忆的条数与位置", _memory),
    "model": Command("model", "显示当前厂商 / 模型 / 工作区", _model),
}

#: 参与补全的命令名（去掉别名，免得补全列表里出现两个一样的东西）
COMPLETION_WORDS: tuple[str, ...] = tuple(f"/{name}" for name in COMMANDS)


def dispatch(line: str, ctx: CommandContext) -> CommandOutcome:
    """执行一条斜杠命令。

    命令名不认识时**不报错**，而是提示并列出可选项——
    手滑打错一个字母就被红字骂一顿，是命令行体验里最没必要的恶意。
    """
    name = line[1:].split(maxsplit=1)[0].strip().lower() if line.startswith("/") else ""
    unknown = f"不认识的命令：/{name}\n" if name else ""
    command = COMMANDS.get(name)
    if command is None:
        return CommandOutcome(message=unknown + _help(ctx).message)
    return command.handler(ctx)
