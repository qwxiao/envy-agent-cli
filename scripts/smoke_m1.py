"""手动冒烟：连一次真实模型，验证模块1 的端到端链路。

单元测试全部离线（伪造 SSE 分片），这个脚本补的是"真网络 + 真协议"那一段：
请求体是否被服务端接受、真实 HTTP 事件流能否被正确分帧、用量与结束原因是否解析出来。
同时打印**各事件的到达次数**——这是观察厂商差异最直接的窗口
（比如某家把思维链放在 content 里，那 ReasoningDelta 就不会出现）。

用法：
    uv run python scripts/smoke_m1.py                # 默认厂商
    uv run python scripts/smoke_m1.py glm            # 指定厂商
    uv run python scripts/smoke_m1.py glm "自定义问题"

退出码：0 链路通；1 拿到 Error 事件；2 配置缺失。
"""

import sys

from envy_agent_cli.config import load_settings
from envy_agent_cli.llm import ChatParams, build
from envy_agent_cli.llm.events import (
    Error,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)

DEFAULT_QUESTION = "用一句话解释什么是 SSE（Server-Sent Events）。"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台默认 GBK

    argv = sys.argv[1:]
    provider = argv[0] if argv else None
    question = argv[1] if len(argv) > 1 else DEFAULT_QUESTION

    try:
        settings = load_settings(provider=provider)
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 2

    adapter = build(settings.provider, settings.api_key,
                    model=settings.model, base_url=settings.base_url)
    print(f"provider={adapter.name} model={adapter.model} base_url={adapter.base_url}")

    counts: dict[str, int] = {}
    text_chars = reasoning_chars = 0
    failed = False

    for event in adapter.stream_chat([{"role": "user", "content": question}],
                                     params=ChatParams(temperature=0.2, max_tokens=128)):
        name = type(event).__name__
        counts[name] = counts.get(name, 0) + 1

        if isinstance(event, TextDelta):
            text_chars += len(event.text)
        elif isinstance(event, ReasoningDelta):
            reasoning_chars += len(event.text)
        elif isinstance(event, ToolCallDelta):
            print(f"  [工具碎片] index={event.index} is_first={event.is_first} name={event.name}")
        elif isinstance(event, Usage):
            print(f"  [用量] prompt={event.prompt_tokens} completion={event.completion_tokens} "
                  f"total={event.total_tokens}")
        elif isinstance(event, MessageEnd):
            print(f"  [结束] stop_reason={event.stop_reason}")
        elif isinstance(event, Error):
            failed = True
            print(f"  [错误] code={event.code.value} retryable={event.retryable} "
                  f"status={event.status_code} message={event.message[:200]}")

    print(f"事件统计: {counts}  正文字符={text_chars} 思维链字符={reasoning_chars}")
    print("结论:", "链路不通" if failed else "链路通")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
