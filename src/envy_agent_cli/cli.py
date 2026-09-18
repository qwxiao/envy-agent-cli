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
from envy_agent_cli.llm import ChatParams, available, build
from envy_agent_cli.llm.adapter import PROVIDER_DEFAULTS
from envy_agent_cli.llm.events import LLMErrorCode
from envy_agent_cli.loop import ConsoleRenderer, run
from envy_agent_cli.tools.builtin import register_builtin_tools
from envy_agent_cli.tools.registry import to_model_schemas
from envy_agent_cli.tools.runtime import UNTRUSTED_DATA_NOTICE, ToolRuntime

USAGE = """用法：
  envy <问题>                        跑一次任务（模型自己决定调什么工具）
  envy --provider glm <问题>         指定厂商
  envy --model glm-5.3-flash <问题>  临时换模型
  envy --hitl never|auto|always      人工确认策略（默认 auto：只问有副作用的）
  envy --max-rounds 5 <问题>         临时改轮数上限
  envy --yes                         本次全自动（等价于 --hitl never）
  envy --list-providers              列出已注册厂商与默认入口

凭证来自环境变量或 .env：<厂商名>_API_KEY（如 DEEPSEEK_API_KEY / GLM_API_KEY）。
审计轨迹写在 audit/cli.jsonl，可用 trace_id 串起一次任务。"""

SYSTEM_PROMPT = (
    "你是一个终端编程助手。可以使用工具来查看和修改工作区里的文件。\n"
    "先观察、再动手；遇到工具报错时，读错误信息并调整策略。\n"
    + UNTRUSTED_DATA_NOTICE
)


def _parse_args(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """极简参数解析：`--key value` 形式的选项 + 其余位置参数。"""
    options: dict[str, str] = {}
    rest: list[str] = []
    flags = {"list-providers", "yes"}
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


def main(argv: list[str] | None = None) -> int:
    options, rest = _parse_args(list(sys.argv[1:] if argv is None else argv))

    if "help" in options or "h" in options or (not rest and not options):
        print(USAGE)
        info = describe_config()
        print(f"\n.env 中的键：{info['from_file'] or '（未找到 .env）'}")
        print(f"已配置凭证的厂商：{info['providers_with_key'] or '（无）'}")
        return 0

    if "list-providers" in options:
        print(f"已注册厂商：{', '.join(available())}（默认 {DEFAULT_PROVIDER}）")
        for name, defaults in sorted(PROVIDER_DEFAULTS.items()):
            print(f"  {name:10s} {defaults['base_url']}  模型 {defaults['model']}")
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
    hitl_mode = "never" if "yes" in options else options.get("hitl", "auto")
    audit = None if "no-audit" in options else AuditLogger(Path("audit") / "cli.jsonl")
    runtime = ToolRuntime(workspace=workspace, hitl_mode=hitl_mode, audit=audit)

    print(f"[{adapter.name} · {adapter.model}] 工作区 {workspace}  HITL={hitl_mode}",
          file=sys.stderr)

    result = run(" ".join(rest),
                 adapter=adapter,
                 runtime=runtime,
                 tool_schemas=to_model_schemas(),
                 render=ConsoleRenderer(),
                 params=ChatParams(max_tokens=2048),
                 system_prompt=SYSTEM_PROMPT,
                 audit=audit)

    print()
    summary = (f"[{result.stop_reason}] 轮数={result.iterations} "
               f"工具调用={result.tool_calls_total} tokens={result.usage.total_tokens if result.usage else 0}")
    print(summary, file=sys.stderr)
    if result.detail:
        print(f"[说明] {result.detail}", file=sys.stderr)
    if result.trace_id:
        print(f"[追踪] trace_id={result.trace_id}（审计：audit/cli.jsonl）", file=sys.stderr)

    if result.stop_reason == "error":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
