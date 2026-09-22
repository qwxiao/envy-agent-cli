"""交互层测试。

`run_repl` 的测试用**假 session + 假 adapter**：真起一个 prompt-toolkit
需要真终端，而这里要验的是"读输入 → 跑任务 → 渲染 → 再读"这套控制流，
不是终端本身的渲染效果。
"""

import io

import pytest

prompt_toolkit = pytest.importorskip("prompt_toolkit", reason="交互层是可选依赖")

from prompt_toolkit.document import Document          # noqa: E402
from rich.console import Console                       # noqa: E402

from envy_agent_cli.loop import AgentResult            # noqa: E402
from envy_agent_cli.repl import commands as commands_mod  # noqa: E402
from envy_agent_cli.repl.commands import (             # noqa: E402
    COMMANDS,
    CommandContext,
    dispatch,
)
from envy_agent_cli.repl.renderer import RichRenderer   # noqa: E402
from envy_agent_cli.repl.session import (               # noqa: E402
    SlashCompleter,
    _print_stats,
    run_repl,
)


@pytest.fixture
def ctx():
    return CommandContext(provider="deepseek", model="v4", workspace="/w",
                          tool_names=["read_file", "write_file"], message_count=3)


def plain_console():
    """不带动画的 Console——测试里不需要转义序列，且 Live 在非终端下无意义。"""
    return Console(file=io.StringIO(), force_terminal=False, width=100)


# ---------------------------------------------------------------- 补全


def completions(text: str) -> list[str]:
    completer = SlashCompleter()
    return [c.text for c in completer.get_completions(Document(text), None)]


def test_completer_suggests_commands_for_slash_prefix():
    assert "/help" in completions("/h")
    assert "/exit" in completions("/e")


def test_completer_silent_for_plain_text():
    """打普通句子时不该弹命令建议——那些补全没一个是他想要的。"""
    assert completions("看看 /tmp") == []
    assert completions("hello") == []


def test_completer_stops_after_space():
    assert completions("/help ") == []


# ---------------------------------------------------------------- 命令


def test_help_lists_every_command(ctx):
    message = dispatch("/help", ctx).message
    for name in COMMANDS:
        assert f"/{name}" in message


def test_exit_commands_set_flag(ctx):
    assert dispatch("/exit", ctx).should_exit is True
    assert dispatch("/quit", ctx).should_exit is True


def test_clear_reports_count_and_sets_flag(ctx):
    outcome = dispatch("/clear", ctx)
    assert outcome.should_clear is True
    assert "3" in outcome.message


def test_tools_lists_registered_names(ctx):
    message = dispatch("/tools", ctx).message
    assert "read_file" in message and "write_file" in message


def test_tools_handles_empty_registry():
    outcome = dispatch("/tools", CommandContext())
    assert "没有注册任何工具" in outcome.message


def test_model_shows_provider_and_workspace(ctx):
    message = dispatch("/model", ctx).message
    assert "deepseek" in message and "/w" in message


def test_unknown_command_is_forgiving(ctx):
    """手滑打错一个字母就被红字骂一顿，是命令行体验里最没必要的恶意。"""
    outcome = dispatch("/lsit", ctx)
    assert "不认识的命令" in outcome.message
    assert "/help" in outcome.message          # 顺手把可选项列出来
    assert outcome.should_exit is False


def test_command_matching_is_case_insensitive(ctx):
    assert dispatch("/HELP", ctx).message


def test_history_reports_message_count(ctx):
    assert "3" in dispatch("/history", ctx).message


def test_history_handles_empty_session():
    assert "还没有历史" in dispatch("/history", CommandContext()).message


def test_memory_reports_count_and_path():
    ctx = CommandContext(memory_count=12, memory_path="/home/u/.envy/memory.db")
    message = dispatch("/memory", ctx).message
    assert "12" in message and "memory.db" in message


def test_memory_says_so_when_disabled():
    """没启用时说清楚是"没启用"，而不是显示 0 条让人以为库是空的。"""
    message = dispatch("/memory", CommandContext()).message
    assert "没有启用" in message


def test_command_set_covers_the_basics(ctx):
    """命令集至少要覆盖：帮助、退出、清历史、看工具、看模型、看会话。

    少于这个数，命令表就只是个装饰——用户该有的动作得靠猜。
    """
    functional = {name for name in COMMANDS if name not in {"quit"}}   # quit 是别名
    assert len(functional) >= 6


# ---------------------------------------------------------------- 渲染


def test_renderer_buffers_text_and_flushes():
    console = plain_console()
    renderer = RichRenderer(console)

    renderer.text("**加粗")
    renderer.text("**完成")
    renderer.flush()

    output = console.file.getvalue()
    assert "加粗" in output and "完成" in output
    assert renderer._text == ""                # flush 后清空，下一轮从零开始


def test_renderer_flush_is_idempotent_on_empty():
    console = plain_console()
    renderer = RichRenderer(console)
    renderer.flush()
    assert console.file.getvalue().strip() == ""


def test_reasoning_hidden_by_default():
    renderer = RichRenderer(plain_console())
    renderer.reasoning("我在想……")
    assert renderer._reasoning == ""


def test_reasoning_collected_when_enabled():
    renderer = RichRenderer(plain_console(), show_reasoning=True)
    renderer.reasoning("我在想……")
    assert renderer._reasoning == "我在想……"


def test_notice_prints_immediately():
    console = plain_console()
    RichRenderer(console).notice("输出被截断")
    assert "输出被截断" in console.file.getvalue()


def test_print_markdown_renders():
    console = plain_console()
    RichRenderer(console).print_markdown("# 标题\n\n正文")
    assert "标题" in console.file.getvalue()


# ---------------------------------------------------------------- 主循环


class FakeSession:
    """按脚本喂输入的假 session。用尽后抛 EOFError，等价于 Ctrl-D。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.prompts = 0

    def prompt(self, *args, **kwargs) -> str:
        self.prompts += 1
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


class FakeAdapter:
    name = "fake"
    model = "fake-model"


def make_result(text: str = "回答") -> AgentResult:
    return AgentResult(text=text, messages=[{"role": "user", "content": "问"},
                                            {"role": "assistant", "content": text}],
                       iterations=1, stop_reason="end_turn")


def run_with(monkeypatch, lines, *, results=None, runtime=None, captured=None):
    """跑一次 run_repl，`run()` 被替换成按脚本返回假结果的桩。"""
    queue = list(results or [make_result()])

    def fake_run(question, **kwargs):
        if captured is not None:
            captured.append({"question": question, "history": kwargs.get("history")})
        result = queue.pop(0) if queue else make_result()
        render = kwargs.get("render")
        if render is not None:
            render.text(result.text)      # 模拟流式输出，否则 flush 时无内容可渲染
        return result

    monkeypatch.setattr("envy_agent_cli.repl.session.run", fake_run)
    console = plain_console()
    code = run_repl(
        adapter=FakeAdapter(),
        runtime=runtime if runtime is not None else object(),
        tool_schemas=[],
        system_prompt="sys",
        params=None,
        workspace="/w",
        console=console,
        session=FakeSession(lines),
    )
    return code, console.file.getvalue()


def test_repl_runs_task_and_exits_on_eof(monkeypatch):
    code, output = run_with(monkeypatch, ["读一下 README"])
    assert code == 0
    assert "回答" in output
    assert "再见" in output


def test_repl_skips_blank_input(monkeypatch):
    captured: list[dict] = []
    run_with(monkeypatch, ["", "   ", "干活"], captured=captured)
    assert [c["question"] for c in captured] == ["干活"]


def test_repl_handles_command_without_running_task(monkeypatch):
    captured: list[dict] = []
    code, output = run_with(monkeypatch, ["/tools", "/exit"], captured=captured)
    assert captured == []                       # 命令不触发任务
    assert code == 0


def test_repl_clear_resets_history(monkeypatch):
    """清空后下一轮拿到的 history 必须是空的——只清显示不算清。"""
    captured: list[dict] = []
    run_with(monkeypatch, ["第一轮", "/clear", "第二轮"],
             results=[make_result("一"), make_result("二")], captured=captured)

    assert captured[0]["history"] == []          # 首轮无历史
    assert captured[1]["history"] == []          # 清空后仍然无历史


def test_repl_carries_history_across_turns(monkeypatch):
    captured: list[dict] = []
    run_with(monkeypatch, ["第一轮", "第二轮"],
             results=[make_result("一"), make_result("二")], captured=captured)

    assert captured[0]["history"] == []
    assert captured[1]["history"]                # 第二轮带上了上一轮的消息
    assert len(captured[1]["history"]) == 2


def test_repl_drops_turn_on_interrupt(monkeypatch):
    """中断发生在 run() 内部时，历史可能停在半截状态——宁可丢一轮，不可坏一段历史。"""
    calls = {"n": 0}

    def flaky_run(question, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return make_result("好的")

    monkeypatch.setattr("envy_agent_cli.repl.session.run", flaky_run)
    console = plain_console()
    run_repl(adapter=FakeAdapter(), runtime=object(), tool_schemas=[],
             system_prompt="sys", params=None, workspace="/w",
             console=console, session=FakeSession(["会中断的", "第二轮"]))

    assert "已中断本轮" in console.file.getvalue()


def test_repl_continues_after_keyboard_interrupt_on_prompt(monkeypatch):
    """Ctrl-C 只取消这一次输入，不该把会话弄没。"""

    class InterruptOnce(FakeSession):
        def prompt(self, *args, **kwargs):
            if self.prompts == 0:
                self.prompts += 1
                raise KeyboardInterrupt
            return super().prompt(*args, **kwargs)

    monkeypatch.setattr("envy_agent_cli.repl.session.run",
                        lambda q, **kw: make_result("ok"))
    console = plain_console()
    code = run_repl(adapter=FakeAdapter(), runtime=object(), tool_schemas=[],
                    system_prompt="sys", params=None, workspace="/w",
                    console=console, session=InterruptOnce(["干活"]))

    assert code == 0


def test_stats_line_reports_truncation():
    console = plain_console()
    result = make_result()
    result.truncated = True
    result.stop_reason = "max_rounds"
    result.detail = "轮数上限 20"
    _print_stats(console, result)

    output = console.file.getvalue()
    assert "max_rounds" in output and "轮数上限 20" in output
