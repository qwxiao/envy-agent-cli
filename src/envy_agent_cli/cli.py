"""命令行入口。

    envy "把当前目录里所有 .py 文件的文件名列出来"
    envy --provider glm "读一下 README 的前 20 行"
    envy --hitl never "统计一下当前目录有几个文件"
    envy --list-providers

**这是一个真正的 Agent**：模型会自己决定调哪个工具、看结果、再决定下一步，
受 ToolRuntime 治理（参数校验 / 路径白名单 / 人工确认 / 超时重试 / 审计）。

渲染在这一层——传输层、适配层、编排层都不打印。
"""

import sys
from pathlib import Path

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
from envy_agent_cli.llm.events import LLMErrorCode
from envy_agent_cli.loop import ConsoleRenderer, run
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
from envy_agent_cli.tools.workspace import register_workspace_tools
from envy_agent_cli.tools.runtime import UNTRUSTED_DATA_NOTICE, ToolRuntime

# 交互层是**可选依赖**（`pip install envy-agent-cli[repl]`）。
# 没装就退化成单次执行模式，而不是抛 ImportError 把人挡在门外——
# 核心引擎只有 httpx，不该被一个终端 UI 的缺席拖住。
try:
    from envy_agent_cli.repl import repl_confirm, run_repl

    REPL_AVAILABLE = True
except ImportError:                       # pragma: no cover - 取决于安装方式
    REPL_AVAILABLE = False

USAGE = """用法：
  envy <问题>                        跑一次任务（模型自己决定调什么工具）
  envy --provider glm <问题>         指定厂商
  envy --model glm-5.3-flash <问题>  临时换模型
  envy --hitl never|auto|always      人工确认策略（默认 auto：只问有副作用的）
  envy --max-rounds 5 <问题>         临时改轮数上限
  envy --context-window 128000 <问题> 模型窗口（用于上下文压缩的预算线）
  envy --no-compact <问题>           关掉上下文压缩（历史原样累积，长任务会撑爆窗口）
  envy --yes                         本次全自动（等价于 --hitl never）
  envy --no-mcp <问题>               本次不加载 MCP 工具
  envy --no-memory <问题>            本次不注册长期记忆工具
  envy --list-providers              列出已注册厂商与默认入口
  envy --list-mcp                    列出配置里的 MCP server

凭证来自环境变量或 .env：<厂商名>_API_KEY（如 DEEPSEEK_API_KEY / GLM_API_KEY）。
审计轨迹写在 audit/cli.jsonl，可用 trace_id 串起一次任务。

MCP 工具来自 `.envy/mcp.json`（项目级）与 `~/.envy/mcp.json`（用户级），
配置格式与 Claude Desktop / Cursor 一致（顶层 `mcpServers`）。
远端工具注册成 `mcp__<server>__<tool>`，走**同一个 ToolRuntime**——
鉴权、审批、超时熔断、审计对内外部工具一视同仁。

上下文压缩默认开：每轮发请求前检查历史是否超预算，超了就把旧轮次摘要掉。
压缩事件落在审计里（kind=compact），与结果里的 context_events 同源。"""

DEFAULT_CONTEXT_WINDOW = 128_000

SYSTEM_PROMPT = (
    "你是一个终端编程助手。可以使用工具来查看和修改工作区里的文件。\n"
    "先观察、再动手；遇到工具报错时，读错误信息并调整策略。\n"
    + UNTRUSTED_DATA_NOTICE
)


def _parse_args(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """极简参数解析：`--key value` 形式的选项 + 其余位置参数。"""
    options: dict[str, str] = {}
    rest: list[str] = []
    flags = {"list-providers", "list-mcp", "yes", "no-mcp", "no-memory"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name = arg[2:]
            if name in flags:
                options[name] = "1"
            elif i + 1 < len(argv):
                options[name] = argv[i + 1]
                i += 1
            else:
                options[name] = ""
        else:
            rest.append(arg)
        i += 1
    return options, rest


def _print_help() -> None:
    print(USAGE)
    info = describe_config()
    print(f"\n.env 中的键：{info['from_file'] or '（未找到 .env）'}")
    print(f"已配置凭证的厂商：{info['providers_with_key'] or '（无）'}")
    mcp = describe_mcp_config()
    if "error" not in mcp and (mcp["stdio"] or mcp["http"]):
        print(f"MCP server：stdio={mcp['stdio'] or '无'}  http={mcp['http'] or '无'}")


def _connect_mcp(options: dict[str, str]) -> McpSession | None:
    """按配置连接 MCP server。

    **失败不阻断启动**：配置读不出来、某个 server 连不上，都只打一行提示就继续——
    内置工具照常可用。这比"少一个 server 整个 CLI 起不来"合理得多。

    返回 `None` 表示"没有要接的"（没配置，或显式 `--no-mcp`）。
    """
    if "no-mcp" in options:
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


def main(argv: list[str] | None = None) -> int:
    options, rest = _parse_args(list(sys.argv[1:] if argv is None else argv))

    if "help" in options or "h" in options:
        _print_help()
        return 0

    # 不带参数 = 想进交互模式。但管道里跑（stdin 不是终端）时不能进——
    # 那会让进程静默挂在那里等永远不会来的输入。没装交互依赖时同理。
    interactive = not rest and not options
    if interactive and (not sys.stdin.isatty() or not REPL_AVAILABLE):
        _print_help()
        return 0

    if "list-providers" in options:
        print(f"已注册厂商：{', '.join(available())}（默认 {DEFAULT_PROVIDER}）")
        for name, defaults in sorted(PROVIDER_DEFAULTS.items()):
            print(f"  {name:10s} {defaults['base_url']}  模型 {defaults['model']}")
        return 0

    if "list-mcp" in options:
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

    try:
        settings = load_settings(provider=options.get("provider") or None)
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    adapter = build(settings.provider, settings.api_key,
                    model=options.get("model") or settings.model,
                    base_url=settings.base_url)

    workspace = Path.cwd()
    register_builtin_tools(workspace)
    register_workspace_tools(workspace)
    if "no-memory" not in options:
        register_memory_tools(workspace)
    hitl_mode = "never" if "yes" in options else options.get("hitl", "auto")
    audit = None if "no-audit" in options else AuditLogger(Path("audit") / "cli.jsonl")
    # 交互模式下用 REPL 版的审批提示（Rich 样式、默认拒绝），单次执行模式用默认的
    runtime = ToolRuntime(workspace=workspace, hitl_mode=hitl_mode, audit=audit,
                          confirm=repl_confirm if interactive else None)

    params = ChatParams(max_tokens=2048)
    if "no-compact" in options:
        policy = NoopContextPolicy()
    else:
        try:
            window = int(options.get("context-window") or DEFAULT_CONTEXT_WINDOW)
        except ValueError:
            print("参数错误：--context-window 需要是整数", file=sys.stderr)
            return 2
        policy = ContextWindowManager(
            ContextBudget(context_window=window, max_output_tokens=params.max_tokens or 4096),
            summary_engine=RuleSummaryEngine(),
        )

    if not interactive:
        print(f"[{adapter.name} · {adapter.model}] 工作区 {workspace}  HITL={hitl_mode}  "
              f"压缩={'关' if 'no-compact' in options else '开'}", file=sys.stderr)

    # ⚠️ 必须在 to_model_schemas() 之前接上——那张表是快照，之后再注册就投影不进去了
    mcp_session = _connect_mcp(options)
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
            )
        result = run(" ".join(rest),
                     adapter=adapter,
                     runtime=runtime,
                     tool_schemas=to_model_schemas(),
                     render=ConsoleRenderer(),
                     params=params,
                     system_prompt=SYSTEM_PROMPT,
                     audit=audit,
                     context_policy=policy)
    finally:
        if mcp_session is not None:
            mcp_session.close()

    print()
    summary = (f"[{result.stop_reason}] 轮数={result.iterations} "
               f"工具调用={result.tool_calls_total} tokens={result.usage.total_tokens if result.usage else 0} "
               f"压缩={len(result.context_events)}次")
    print(summary, file=sys.stderr)
    for event in result.context_events:
        print(f"[压缩] {event.span_id} {event.before_tokens} -> {event.after_tokens} tokens，"
              f"摘要 {event.summarized_messages} 条（{event.triggered_by}，{event.summary_engine}）",
              file=sys.stderr)
    if result.detail:
        print(f"[说明] {result.detail}", file=sys.stderr)
    if result.trace_id:
        print(f"[追踪] trace_id={result.trace_id}（审计：audit/cli.jsonl）", file=sys.stderr)

    if result.stop_reason == "error":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
