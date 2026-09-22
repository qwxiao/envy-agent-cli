"""命令行入口。

    envy                                 进交互式会话（REPL）
    envy "把当前目录里所有 .py 文件的文件名列出来"
    envy --provider glm "读一下 README 的前 20 行"
    envy --list-providers

**这是一个真正的 Agent**：模型会自己决定调哪个工具、看结果、再决定下一步，
受 ToolRuntime 治理（参数校验 / 路径白名单 / 人工确认 / 超时重试 / 审计）。

**参数层用 Typer 基于类型注解生成**——help 文本、类型校验、错误提示都不用手写。
本文件因此分成两半：`cli()` 只声明"命令行长什么样"，`_execute()` 才是有副作用的业务。
这个分界让参数层可以随框架演进（换 argparse / click 都只动上面一半），
而下面一半与终端无关——测试可以直接调 `_execute()`，不必构造命令行字符串。

渲染在这一层——传输层、适配层、编排层都不打印。
"""

import sys
from pathlib import Path

import typer

from envy_agent_cli.audit.logger import AuditLogger
from envy_agent_cli.config import DEFAULT_PROVIDER, describe_config, load_settings
from envy_agent_cli.context import (
    ContextBudget,
    ContextWindowManager,
    NoopContextPolicy,
    RuleSummaryEngine,
)
from envy_agent_cli.llm import ChatParams, available, build
from envy_agent_cli.llm.adapter import PROVIDER_DEFAULTS
from envy_agent_cli.loop import Budget, ConsoleRenderer, run
from envy_agent_cli.memory import register_memory_tools
from envy_agent_cli.mcp import (
    McpError,
    McpSession,
    connect_mcp_servers,
    describe_mcp_config,
    load_mcp_server_specs,
)
from envy_agent_cli.tools.builtin import register_builtin_tools
from envy_agent_cli.tools.registry import to_model_schemas
from envy_agent_cli.tools.runtime import UNTRUSTED_DATA_NOTICE, ToolRuntime
from envy_agent_cli.tools.workspace import register_workspace_tools

# 交互层是**可选依赖**（`pip install envy-agent-cli[repl]`）。
# 没装就退化成单次执行模式，而不是抛 ImportError 把人挡在门外——
# 引擎不该被一个终端 UI 的缺席拖住。
try:
    from envy_agent_cli.repl import repl_confirm, run_repl

    REPL_AVAILABLE = True
except ImportError:                       # pragma: no cover - 取决于安装方式
    REPL_AVAILABLE = False

APP_NAME = "envy"

DEFAULT_CONTEXT_WINDOW = 128_000

SYSTEM_PROMPT = (
    "你是一个终端编程助手。可以使用工具来查看和修改工作区里的文件。\n"
    "先观察、再动手；遇到工具报错时，读错误信息并调整策略。\n"
    + UNTRUSTED_DATA_NOTICE
)

EPILOG = """凭证来自环境变量或 .env：<厂商名>_API_KEY（如 DEEPSEEK_API_KEY / GLM_API_KEY）。

MCP 工具来自 .envy/mcp.json（项目级）与 ~/.envy/mcp.json（用户级），
格式与 Claude Desktop / Cursor 一致（顶层 mcpServers）。远端工具注册成
mcp__<server>__<tool>，走同一个 ToolRuntime——鉴权、审批、超时熔断、审计
对内外部工具一视同仁。

审计轨迹写在 audit/cli.jsonl，可用 trace_id 串起一次任务。
上下文压缩默认开：每轮发请求前检查历史是否超预算，超了就把旧轮次摘要掉。"""

app = typer.Typer(
    add_completion=False,          # 补全脚本与本工具的用法无关，不占 help 篇幅
    no_args_is_help=False,         # 无参数不是错误，是"进交互模式"
    rich_markup_mode=None,         # help 里不解析 rich 标记，避免 `[...]` 被吞
    help="终端 Coding Agent：自研 ReAct 主循环 + Tool Runtime，工具含本地文件、MCP 远端与长期记忆。",
)


# ---------------------------------------------------------------- 业务

def _execute(
    question: list[str] | None,
    *,
    provider: str | None,
    model: str | None,
    hitl: str,
    yes: bool,
    max_rounds: int | None,
    context_window: int | None,
    compact: bool,
    use_memory: bool,
    use_mcp: bool,
    use_audit: bool,
    list_providers: bool,
    list_mcp: bool,
) -> int:
    """把参数变成一次运行。**不解析命令行**——那是上面那一半的事。"""
    if list_providers:
        print(f"已注册厂商：{', '.join(available())}（默认 {DEFAULT_PROVIDER}）")
        for name, defaults in sorted(PROVIDER_DEFAULTS.items()):
            print(f"  {name:10s} {defaults['base_url']}  模型 {defaults['model']}")
        return 0

    if list_mcp:
        return _show_mcp_config()

    # 没有任务 = 想进交互模式。但管道里跑（stdin 不是终端）时不能进——
    # 那会让进程静默挂在那里等永远不会来的输入。没装交互依赖时同理。
    interactive = not question
    if interactive and (not sys.stdin.isatty() or not REPL_AVAILABLE):
        _print_diagnostics()
        return 0

    try:
        settings = load_settings(provider=provider)
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    adapter = build(settings.provider, settings.api_key,
                    model=model or settings.model,
                    base_url=settings.base_url)

    workspace = Path.cwd()
    register_builtin_tools(workspace)
    register_workspace_tools(workspace)
    memory_store = register_memory_tools(workspace) if use_memory else None

    hitl_mode = "never" if yes else hitl
    audit = AuditLogger(Path("audit") / "cli.jsonl") if use_audit else None
    # 交互模式下用 REPL 版的审批提示（Rich 样式、默认拒绝）
    runtime = ToolRuntime(workspace=workspace, hitl_mode=hitl_mode, audit=audit,
                          confirm=repl_confirm if interactive else None)

    params = ChatParams(max_tokens=2048)
    policy = _build_policy(compact, context_window, params)
    budget = Budget(max_rounds=max_rounds) if max_rounds else None

    if not interactive:
        print(f"[{adapter.name} · {adapter.model}] 工作区 {workspace}  HITL={hitl_mode}  "
              f"压缩={'开' if compact else '关'}", file=sys.stderr)

    # ⚠️ 必须在 to_model_schemas() 之前接上——那张表是快照，之后再注册就投影不进去了
    mcp_session = _connect_mcp(use_mcp)
    try:
        if interactive:
            return run_repl(
                adapter=adapter,
                runtime=runtime,
                tool_schemas=to_model_schemas(),
                system_prompt=SYSTEM_PROMPT,
                params=params,
                workspace=workspace,
                audit=audit,
                context_policy=policy,
                memory_store=memory_store,
            )
        result = run(" ".join(question or []),
                     adapter=adapter,
                     runtime=runtime,
                     tool_schemas=to_model_schemas(),
                     render=ConsoleRenderer(),
                     params=params,
                     system_prompt=SYSTEM_PROMPT,
                     budget=budget,
                     audit=audit,
                     context_policy=policy)
    finally:
        if mcp_session is not None:
            mcp_session.close()

    return _report(result)


def _build_policy(compact: bool, context_window: int | None, params: ChatParams):
    if not compact:
        return NoopContextPolicy()
    window = context_window if context_window else DEFAULT_CONTEXT_WINDOW
    return ContextWindowManager(
        ContextBudget(context_window=window, max_output_tokens=params.max_tokens or 4096),
        summary_engine=RuleSummaryEngine(),
    )


def _report(result) -> int:
    """单次执行结束后的收尾输出。**全走 stderr**——stdout 留给正文。"""
    print()
    tokens = result.usage.total_tokens if result.usage else 0
    print(f"[{result.stop_reason}] 轮数={result.iterations} "
          f"工具调用={result.tool_calls_total} tokens={tokens} "
          f"压缩={len(result.context_events)}次", file=sys.stderr)
    for event in result.context_events:
        print(f"[压缩] {event.span_id} {event.before_tokens} -> {event.after_tokens} tokens，"
              f"摘要 {event.summarized_messages} 条（{event.triggered_by}，{event.summary_engine}）",
              file=sys.stderr)
    if result.detail:
        print(f"[说明] {result.detail}", file=sys.stderr)
    if result.trace_id:
        print(f"[追踪] trace_id={result.trace_id}（审计：audit/cli.jsonl）", file=sys.stderr)
    return 1 if result.stop_reason == "error" else 0


def _show_mcp_config() -> int:
    info = describe_mcp_config()
    if "error" in info:
        print(f"配置读取失败：{info['error'][0]}", file=sys.stderr)
        return 2
    if not info["stdio"] and not info["http"]:
        print("没有配置任何 MCP server。")
        print(f"项目级：{Path.cwd() / '.envy' / 'mcp.json'}")
        print(f"用户级：{Path.home() / '.envy' / 'mcp.json'}")
        return 0
    print(f"stdio：{', '.join(info['stdio']) or '（无）'}")
    print(f"http ：{', '.join(info['http']) or '（无）'}")
    return 0


def _print_diagnostics() -> None:
    """无参数且不能进交互时打印的东西：用法 + 当前配置实况。"""
    print(f"用 {APP_NAME} --help 查看用法。\n")
    info = describe_config()
    print(f".env 中的键：{info['from_file'] or '（未找到 .env）'}")
    print(f"已配置凭证的厂商：{info['providers_with_key'] or '（无）'}")
    mcp = describe_mcp_config()
    if "error" not in mcp and (mcp["stdio"] or mcp["http"]):
        print(f"MCP server：stdio={mcp['stdio'] or '无'}  http={mcp['http'] or '无'}")


def _connect_mcp(enabled: bool) -> McpSession | None:
    """按配置连接 MCP server。

    **失败不阻断启动**：配置读不出来、某个 server 连不上，都只打一行提示就继续——
    内置工具照常可用。这比"少一个 server 整个 CLI 起不来"合理得多。
    """
    if not enabled:
        return None
    try:
        specs = load_mcp_server_specs(Path.cwd())
    except McpError as exc:
        print(f"[MCP] 配置读取失败：{exc}", file=sys.stderr)
        return None
    if not specs:
        return None

    session = connect_mcp_servers(specs)
    for error in session.errors:
        print(f"[MCP] 跳过 {error}", file=sys.stderr)
    if session.tool_count:
        print(f"[MCP] 接入 {session.tool_count} 个工具：{', '.join(session.tools)}", file=sys.stderr)
    return session


# ---------------------------------------------------------------- 参数层

@app.command(no_args_is_help=False, epilog=EPILOG)
def cli(
    question: list[str] | None = typer.Argument(
        None, help="要执行的任务。不给则进入交互式会话。"),
    provider: str | None = typer.Option(
        None, "--provider", "-p", help=f"厂商（默认 {DEFAULT_PROVIDER}）"),
    model: str | None = typer.Option(None, "--model", help="临时换模型"),
    hitl: str = typer.Option(
        "auto", "--hitl", help="人工确认策略：auto（只问有副作用的）/ always / never"),
    yes: bool = typer.Option(False, "--yes", "-y", help="本次全自动（等价于 --hitl never）"),
    max_rounds: int | None = typer.Option(
        None, "--max-rounds", min=1, help="轮数上限（默认 20）"),
    context_window: int | None = typer.Option(
        None, "--context-window", min=1000, help="模型窗口大小，用于压缩预算线"),
    compact: bool = typer.Option(True, "--compact/--no-compact",
                                 help="上下文压缩（默认开）"),
    memory: bool = typer.Option(True, "--memory/--no-memory",
                                help="长期记忆工具（默认开）"),
    mcp: bool = typer.Option(True, "--mcp/--no-mcp", help="MCP 工具（默认开）"),
    audit: bool = typer.Option(True, "--audit/--no-audit", help="审计日志（默认开）"),
    list_providers: bool = typer.Option(False, "--list-providers", help="列出已注册厂商"),
    list_mcp: bool = typer.Option(False, "--list-mcp", help="列出配置里的 MCP server"),
) -> None:
    """跑一次任务，或进入交互式会话。"""
    code = _execute(
        question,
        provider=provider, model=model, hitl=hitl, yes=yes,
        max_rounds=max_rounds, context_window=context_window,
        compact=compact, use_memory=memory, use_mcp=mcp, use_audit=audit,
        list_providers=list_providers, list_mcp=list_mcp,
    )
    raise typer.Exit(code)


def main(argv: list[str] | None = None) -> int:
    """console_scripts 入口。

    `standalone_mode=False` 让 Click 把控制权还给我们——不然它会在内部
    `sys.exit()`，我们就没法把退出码礼貌地交还给调用方（测试尤其需要）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        app(args=args, standalone_mode=False, prog_name=APP_NAME)
        return 0
    except typer.Exit as exc:
        # 正常结束路径：`cli()` 里的 `raise typer.Exit(code)`
        return int(exc.exit_code or 0)
    except typer.Abort:
        print("\n已中止。", file=sys.stderr)
        return 130
    except typer.TyperException as exc:
        # 参数错误（类型不符、缺必填、未知选项）。⚠️ standalone_mode=False 下
        # Typer 不会自己打印，得我们打——否则用户只看到一个退出码。
        #
        # 补一句 --help 指引：Typer 的原始信息只说"'abc' 不是合法的整数"，
        # **不带选项名**——有多个整数选项时，用户不知道该改哪个。
        print(f"参数错误：{exc}", file=sys.stderr)
        print(f"用 {APP_NAME} --help 查看各参数的说明。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
