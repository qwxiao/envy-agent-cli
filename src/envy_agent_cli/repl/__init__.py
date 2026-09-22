"""交互式终端（REPL）。

同步架构下的 REPL：**读输入 → 跑任务 → 渲染 → 再读**，交替而不是并发。
换来的是简单——prompt-toolkit 与流式输出天生不打架；
代价是不能边输出边打字。

这一层是**可选依赖**（`pip install envy-agent-cli[repl]`）：
引擎不需要它，所以它不在核心依赖里。
"""

from envy_agent_cli.repl.commands import COMMANDS, CommandContext, CommandOutcome, dispatch
from envy_agent_cli.repl.renderer import RichRenderer
from envy_agent_cli.repl.session import (
    PROMPT_SIGN,
    build_prompt_session,
    repl_confirm,
    run_repl,
)

__all__ = [
    "COMMANDS",
    "PROMPT_SIGN",
    "CommandContext",
    "CommandOutcome",
    "RichRenderer",
    "build_prompt_session",
    "dispatch",
    "repl_confirm",
    "run_repl",
]
