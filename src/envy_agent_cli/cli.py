"""命令行入口。

    envy "用一句话解释什么是 SSE"
    envy --provider glm "介绍一下 ReAct"
    envy --list-providers

当前阶段只跑通模块1（单轮问答）：装配适配器 → 消费事件流 → 渲染。
编排（多轮 + 工具调用）接上模块2 之后，这里会改成走 Agent Loop。

渲染放在这一层——传输层和适配层都不打印（分层约束之一），
所以同一份客户端既能喂终端，也能喂别的消费方。
"""

import sys

from envy_agent_cli.config import DEFAULT_PROVIDER, describe_config, load_settings
from envy_agent_cli.llm import ChatParams, available, build
from envy_agent_cli.llm.adapter import PROVIDER_DEFAULTS
from envy_agent_cli.llm.events import (
    Error,
    LLMErrorCode,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)

USAGE = """用法：
  envy <问题>                     用默认厂商问一句
  envy --provider glm <问题>      指定厂商
  envy --model glm-5.3-flash <问题>   临时换模型
  envy --list-providers           列出已注册厂商与默认入口

凭证来自环境变量或 .env：<厂商名>_API_KEY（如 DEEPSEEK_API_KEY / GLM_API_KEY）。"""


def _parse_args(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """极简参数解析：`--key value` 形式的选项 + 其余位置参数。"""
    options: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name = arg[2:]
            if name == "list-providers":
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
    print(f"[{adapter.name} · {adapter.model}]", file=sys.stderr)

    question = " ".join(rest)
    status = 0
    for event in adapter.stream_chat([{"role": "user", "content": question}],
                                     params=ChatParams()):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ReasoningDelta):
            # 思维链走 stderr：终端里和正文同屏可见，但重定向时答案干净
            # （`envy "问题" > answer.txt` 里不会混进推理过程）
            print(event.text, end="", file=sys.stderr, flush=True)
        elif isinstance(event, ToolCallDelta):
            print(f"\n[工具调用] index={event.index} name={event.name}", file=sys.stderr)
        elif isinstance(event, Usage):
            print(f"\n[用量] {event.total_tokens} tokens", file=sys.stderr)
        elif isinstance(event, MessageEnd):
            if event.stop_reason == "max_tokens":
                print("\n[警告] 输出被 max_tokens 截断，结果可能不完整", file=sys.stderr)
                status = 1
        elif isinstance(event, Error):
            print(f"\n[错误] {event.code.value} (retryable={event.retryable}) "
                  f"{event.message}", file=sys.stderr)
            if event.code is not LLMErrorCode.UNKNOWN:
                status = 1
    print()
    return status


if __name__ == "__main__":
    sys.exit(main())
