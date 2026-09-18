"""模块4 · 规则摘要引擎（抽取式，默认档）。

不调模型：零成本、确定、可离线断言。代价是**信息有损、没有语义提炼**——
它保的是"目标 / 决策 / 文件 / 未完成工作"这些骨架，细节和语义联系会丢。

摘要开头显式写明"这是压缩稿、可能不完整"：
**宁可让丢失"可感知"，也别让丢失"不可感知"**——后者才是真坑。
"""

import json
import re

from envy_agent_cli.context.compactor import ENGINE_RULE

_WHITESPACE = re.compile(r"\s+")

#: 摘要正文的开场白。**标签由 `build_summary_message` 负责包**——这里再带一次就会出现双层标签，
#: 让 `grep <history_summary>` 这个审计锚点变成两个，锚点也就失去意义了。
HEADER = "更早的对话已被压缩成下面这份要点清单。它是有损的：保留目标、决定、涉及的文件与还没做完的事，但细节不完整——需要精确内容时请重新读取，不要以清单为准。"


class RuleSummaryEngine:
    """把旧轮次逐条压成一行要点。

    字数配额按条数摊：`per_message = clamp(max_chars // 条数, 下限, 上限)`，
    这样"消息特别多"时不会因为每条都留太长而整体超配额。
    """

    name = ENGINE_RULE

    def __init__(self, max_chars: int = 6000, per_message_max: int = 500,
                 per_message_min: int = 80) -> None:
        self.max_chars = max(256, max_chars)
        self.per_message_max = max(1, per_message_max)
        self.per_message_min = max(1, per_message_min)

    def summarize(self, turns: list[dict]) -> str:
        if not turns:
            return HEADER

        quota = max(self.per_message_min,
                    min(self.per_message_max, self.max_chars // max(1, len(turns))))
        lines = [HEADER]
        for message in turns:
            text = self._render(message)
            if not text:
                continue
            if len(text) > quota:
                text = text[: quota - 3] + "..."
            lines.append(f"- {message.get('role', 'unknown')}: {text}")
        return "\n".join(lines)[: self.max_chars]

    @staticmethod
    def _render(message: dict) -> str:
        """一条消息压成一行：正文优先，正文为空但带调用声明时用调用声明。"""
        content = message.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False)
        text = _WHITESPACE.sub(" ", text).strip()
        if not text and message.get("tool_calls"):
            text = json.dumps(message["tool_calls"], ensure_ascii=False)
        return text


__all__ = ["RuleSummaryEngine", "HEADER"]
