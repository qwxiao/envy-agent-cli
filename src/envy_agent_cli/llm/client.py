"""模块1 · 传输层：把 HTTP + SSE 流翻译成 typed 事件。

**职责（SHOULD DO）**
- 发请求、收流；
- 按 SSE 协议**空行 `\\n\\n`** 分帧（不是按行）；
- 进流先查 `status_code`（错误响应根本不是 SSE 格式，不查会把一坨报错当"没正文的正常流"静默结束）；
- 把 chunk 解析成 typed 事件 yield 出去；
- 超时 / HTTP 错 → yield `Error` 事件。

**边界（SHOULD NOT）**
- ❌ 不 print（渲染是上层的事，焊死在这里以后接 UI 就漏水）；
- ❌ 不攒 tool_calls（拼装属决策层）；
- ❌ 不判断任务是否结束（那是 `stop_reason` 的活，由编排层读）。

设计约束：本层是**同步 httpx + 生成器**，不用 async。
"""

import json
from typing import Iterator

from envy_agent_cli.llm.events import AnyEvent, Error, MessageEnd, TextDelta, ToolCallDelta, Usage
from envy_agent_cli.llm.protocol import ChatMessage

SSE_DONE = "[DONE]"


def stream_chat(
    messages: list[ChatMessage],
    tools: list[dict] | None = None,
    **params,
) -> Iterator[AnyEvent]:
    """一次性请求 + 流式接收，逐条 yield 事件。

    Yields:
        TextDelta / ToolCallDelta / MessageEnd / Usage / Error
    """
    raise NotImplementedError("待移植：模块1 已验证的 SSE 分帧 + 事件化实现")


def _iter_sse_events(response) -> Iterator[str]:
    """按 SSE 记录边界（空行）分帧，yield 每个完整事件块的 data 行内容。

    关键点：SSE 的记录边界是**空行 `\\n\\n`**，不是换行符。网络可能把一个 `data: {...}`
    拆成两个 chunk 到达，按行切会切出半行 JSON，`json.loads` 直接崩。
    **别按传输的"到达粒度"处理，要按协议的"记录粒度"处理。**
    """
    raise NotImplementedError("待移植：buffer 累积 + '\\n\\n' 切块")


def _extract_data(block: str) -> str | None:
    """从一个事件块里抽出 data 行内容；`[DONE]` 必须**在 json.loads 之前**判断并终止。"""
    raise NotImplementedError("待移植")


def _parse_chunk(chunk: dict) -> list[AnyEvent]:
    """把一个 SSE chunk 转成零到多条事件。

    需要防御性取值：chunk 里不只有正文，还有角色块、结束块（finish_reason）、
    以及 `choices` 为空的 usage 块（`chunk.get("choices") or []` 一行字防住）。
    """
    raise NotImplementedError("待移植")


def _headers() -> dict:
    """鉴权头。⚠️ 当前形态是全局 API_KEY，Adapter 层落地后由 Adapter 提供凭证。"""
    raise NotImplementedError
