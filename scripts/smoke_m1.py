"""手动冒烟：连一次真实模型，验证模块1 的端到端链路。

单元测试全部离线（伪造 SSE 分片），这个脚本补的是"真网络 + 真协议"那一段：
请求体是否被服务端接受、真实 HTTP 事件流能否被正确分帧、用量与结束原因是否解析出来。

用法（凭证只走环境变量，不要写进文件）：

    set -a; source ../.env; set +a          # 或自行 export API_KEY/BASE_URL/MODEL
    uv run python scripts/smoke_m1.py

退出码：0 表示链路通；1 表示拿到的是 Error 事件。
"""

import os
import sys

from envy_agent_cli.llm import ChatParams, build
from envy_agent_cli.llm.events import (
    Error,
    MessageEnd,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    Usage,
)


def main() -> int:
    # Windows 控制台默认 GBK，中文输出会乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = os.environ.get("API_KEY")
    if not api_key:
        print("缺少环境变量 API_KEY")
        return 2

    provider = os.environ.get("SMOKE_PROVIDER", "deepseek")
    adapter = build(
        provider,
        api_key,
        model=os.environ.get("MODEL") or None,
        base_url=os.environ.get("BASE_URL") or None,
    )
    print(f"provider={adapter.name} model={adapter.model} base_url={adapter.base_url}")

    messages = [{"role": "user", "content": "用一句话解释什么是 SSE（Server-Sent Events）。"}]
    counts: dict[str, int] = {}
    text_chars = reasoning_chars = 0
    failed = False

    for event in adapter.stream_chat(messages, params=ChatParams(temperature=0.2, max_tokens=128)):
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
