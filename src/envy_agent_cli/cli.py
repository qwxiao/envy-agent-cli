"""命令行入口。

    envy "用一句话解释什么是 SSE"

当前阶段只跑通模块1（单轮问答）：装配适配器 → 消费事件流 → 渲染。
编排（多轮 + 工具调用）接上模块2 之后，这里会改成走 Agent Loop。

渲染放在这一层——传输层和适配层都不打印（分层约束之一），
所以同一份客户端既能喂终端，也能喂别的消费方。
"""

import sys

from envy_agent_cli.config import describe_keys, load_settings
from envy_agent_cli.llm import ChatParams, build
from envy_agent_cli.llm.events import (
    Error,
    LLMErrorCode,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__.strip())
        keys = describe_keys()
        print(f"\n.env 中可识别的配置项: {keys or '（未找到 .env）'}")
        return 0

    question = " ".join(args)

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    adapter = build(settings.provider, settings.api_key,
                    model=settings.model, base_url=settings.base_url)

    status = 0
    for event in adapter.stream_chat([{"role": "user", "content": question}],
                                     params=ChatParams()):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ReasoningDelta):
            print(event.text, end="", flush=True)  # 思维链与正文分色是终端层的事
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
