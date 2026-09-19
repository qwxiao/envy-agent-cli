"""模块4 · 规则摘要引擎（抽取式，默认档）。

不调模型：零成本、确定、可离线断言。代价是**信息有损、没有语义提炼**——
它保的是"目标 / 决策 / 文件 / 未完成工作"这些骨架，细节和语义联系会丢。

**只产出条目，不管包装。** 标签与开场白由 `build_summary_message` 负责——
如果开场白写在这里，它就会跟着每份被带过来的旧摘要重复出现 N 次。

**工具结果与普通消息分路处理**（见 `_render_tool_result`）：
过长的工具结果**只给索引、不给半截原文**——摘要前言声明了"需要精确内容时请重新读取"，
要是同时又塞一段被截断的源码，模型会误判自己已经持有原文，**反而不去重读**，
前言就从索引退化成了误导。
"""

import json
import re

from envy_agent_cli.context.compactor import ENGINE_RULE, TOOL_ROLE

_WHITESPACE = re.compile(r"\s+")

#: 工具结果的包装标签，对应 `tools/runtime.py` 的 `wrap_result`。
#: 这里**故意不复用那个常量**——上下文层不该绑到工具层；两边是否一致由测试守着。
TOOL_RESULT_TAG = "tool_result"

#: 从工具结果里取出工具名（`<tool_result tool="read_file" ...>`）。
_TOOL_NAME = re.compile(rf'<{TOOL_RESULT_TAG}\s+tool="([^"]+)"')

#: 正文被丢弃时的显式标记。**宁可让丢失"可感知"，也别让丢失"不可感知"**。
DISCARDED = "[原文已丢弃，需重读]"


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
            return ""

        quota = max(self.per_message_min,
                    min(self.per_message_max, self.max_chars // max(1, len(turns))))
        lines: list[str] = []
        for message in turns:
            text = self._render(message, quota)
            if not text:
                continue
            if len(text) > quota:
                text = text[: quota - 3] + "..."
            lines.append(f"- {message.get('role', 'unknown')}: {text}")
        return "\n".join(lines)[: self.max_chars]

    def _render(self, message: dict, quota: int) -> str:
        """一条消息压成一行。工具结果另走一条路——它的"长"和正文的"长"不是一回事。"""
        if message.get("role") == TOOL_ROLE:
            return self._render_tool_result(message, quota)
        return self._render_plain(message)

    @staticmethod
    def _render_plain(message: dict) -> str:
        """普通消息：正文优先，正文为空但带调用声明时用调用声明。"""
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

    @staticmethod
    def _render_tool_result(message: dict, quota: int) -> str:
        """工具结果：**要么给全，要么只给索引**，绝不给"半截原文"。

        短结果原样留着——它本来就是内容，不存在"半截"的问题，丢掉反而白白损失信息。
        长结果压成"哪个工具、多大、正文没留"，让模型清楚知道自己手上没有原文，该重读就重读。
        """
        text = _WHITESPACE.sub(" ", str(message.get("content") or "")).strip()
        if not text:
            return ""
        if len(text) <= quota:
            return text
        found = _TOOL_NAME.search(text)
        label = found.group(1) if found else "工具"
        return f"{label} 返回 {len(text)} 字符，正文过长未保留。{DISCARDED}"


__all__ = ["DISCARDED", "RuleSummaryEngine", "TOOL_RESULT_TAG"]
