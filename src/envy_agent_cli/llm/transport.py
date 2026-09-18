"""模块1 · 传输层：把 SSE 字节流切成"事件块"。

**这一层只认字节和协议，不认模型**——它不知道 TextDelta 是什么，也不需要知道。
厂商差异（哪个字段是正文、哪个是思维链）归 `adapter.py` 处理。

三条硬约束（封版，改动即破契约）：

1. **按协议的"记录粒度"处理，不按网络的"到达粒度"处理**
   SSE 的记录边界是**空行 `\\n\\n`**，不是换行符。网络可能把一个 `data: {...}`
   拆成两个 chunk 到达，按行切会切出半行 JSON，`json.loads` 直接崩。
   所以：攒缓冲 → `while "\\n\\n" in buffer` 切完整块 → 再解析。
2. **`[DONE]` 必须在 `json.loads` 之前判断**——`data: [DONE]` 不是合法 JSON，先解析必崩。
3. **流末残留缓冲要处理**——服务端最后一块可能没有结尾空行。

验证方式：mock 一个"把完整事件切成几片"的流来测重组，不联网也能验。
"""

import json
from typing import Iterator

SSE_DONE = "[DONE]"


def extract_data(block: str) -> str | None:
    """从一个事件块里抽出 data 行内容；多行 data 用换行拼。没有 data 行返回 None。"""
    lines = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            lines.append(line[len("data:"):].strip())
    return "\n".join(lines) if lines else None


def iter_sse_data(chunks: Iterator[str]) -> Iterator[str]:
    """字节流 → data 载荷字符串流（每 yield 一次 = 一个完整 SSE 事件）。

    Args:
        chunks: 文本分片迭代器（httpx 的 `response.iter_text()`，或测试里伪造的分片）。
    """
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            data = extract_data(block)
            if data is not None:
                yield data
    # 流末残留：最后一块可能没有结尾空行
    if buffer.strip():
        data = extract_data(buffer)
        if data is not None:
            yield data


def iter_sse_json(chunks: Iterator[str]) -> Iterator[dict]:
    """字节流 → 已解析的 JSON 对象流。

    `[DONE]` 哨兵在这里终结迭代，**保证它永远不会进 json.loads**——
    把这个顺序固化在一处，消费方就不需要记得自己判断。
    解析失败的块直接跳过（厂商偶发的心跳/注释行不该让整条流崩掉）。
    """
    for data in iter_sse_data(chunks):
        if data == SSE_DONE:
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue
