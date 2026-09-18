"""模块4 · LLM 摘要引擎（语义档，可选）。

比规则档会提炼，但多一轮往返、多花钱、**结果不可复现**——所以它不是默认档，
默认是规则档，只有在"需要摘要质量而非防爆"时才换上来。

两条纪律：

1. **必须不流式**：摘要是一次性产物，没有边生成边消费的必要，把流收完再返回。
2. **摘要请求绝不能写进 messages**：否则摘要的输入里会含有上一份摘要，**自我递归**。

它属于 `context/` 但**不属于 `compactor.py`**——`compactor.py` 保持纯计算、零 IO，
依赖注入是这条边界的技术手段。
"""

import json

from envy_agent_cli.context.compactor import ENGINE_LLM
from envy_agent_cli.llm.adapter import ChatMessage, ChatModel
from envy_agent_cli.llm.events import Error, TextDelta
from envy_agent_cli.llm.params import ChatParams

SUMMARIZE_PROMPT = """把下面这段对话压缩成一段事实性摘要。要求：

1. **只记事实**：用户要什么、做过哪些尝试、得到什么结果、还有哪些没解决。
2. **保留具体值**：文件名、路径、函数名、错误码、数字——这些丢了就无法继续。
3. **不要复述过程**：不要写"模型调用了读取工具"，要写"读了某个文件，结论是 X"。
4. **不要评价、不要抒情**。
5. 用第三人称，控制在 300 字以内。
"""


def render_turns(turns: list[dict]) -> str:
    """把旧轮次渲染成给摘要模型看的纯文本（不是消息数组，避免被误当成对话续写）。"""
    lines: list[str] = []
    for message in turns:
        role = message.get("role", "unknown")
        content = message.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False)
        if not text and message.get("tool_calls"):
            text = json.dumps(message["tool_calls"], ensure_ascii=False)
        lines.append(f"[{role}] {text}")
    return "\n".join(lines)


class LLMSummaryEngine:
    """用一次模型调用把旧轮次提炼成摘要。失败由调用方兜（压缩永不抛出）。"""

    name = ENGINE_LLM

    def __init__(self, adapter: ChatModel, model: str | None = None,
                 prompt: str = SUMMARIZE_PROMPT) -> None:
        self._adapter = adapter
        self._model = model
        self._prompt = prompt

    def summarize(self, turns: list[dict]) -> str:
        if not turns:
            return ""
        request: list[ChatMessage] = [
            {"role": "user", "content": f"{self._prompt}\n\n---\n\n{render_turns(turns)}"}
        ]
        params = ChatParams(extra={"model": self._model} if self._model else None)

        chunks: list[str] = []
        for event in self._adapter.stream_chat(request, params=params):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
            elif isinstance(event, Error):
                raise RuntimeError(f"摘要请求失败：{event.code.value} {event.message}")
        return "".join(chunks).strip()


__all__ = ["LLMSummaryEngine", "SUMMARIZE_PROMPT", "render_turns"]
