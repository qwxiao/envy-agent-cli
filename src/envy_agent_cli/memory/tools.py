"""模块6 · 长期记忆的工具暴露：`save_memory` / `search_memory`。

**为什么召回做成工具，而不是每轮自动注入**：

自动注入等于在 `messages` 之外开第二个输入通道——与架构已定的纪律冲突
（一个输入通道 = 一个真相来源，`prepare()` 不收 `system_prompt` 就是这条）。
走工具则记忆内容作为 tool result 进 `messages`，**输入通道仍然只有一条**，
而且召回时机由模型判断（它比任何启发式更清楚"现在需不需要回忆"）。

代价是模型得记得调它——所以工具描述写得明确一点，把"什么时候该用"讲清楚。

**为什么只有两个工具**：项目的工具口径是 8 个（`builtin` 3 + 文件/命令 3 + 记忆 2）。
"删除记忆"确实是个缺口，但它换来的是第 9 个工具——而工具每多一个，
schema 就多占一份上下文、模型选错率就上一档。等真有需要再加。
"""

from pathlib import Path

from envy_agent_cli.memory.store import MemoryStore, store_for
from envy_agent_cli.memory.types import MemoryKind
from envy_agent_cli.tools.registry import register
from envy_agent_cli.tools.spec import RegisteredTool, Risk, ToolSpec

#: 合法的记忆类别。模型给了没见过的值就降级成 `note`——
#: 为一次拼写错误打断整个流程不值得，但要如实告诉它降级了。
VALID_KINDS = tuple(k.value for k in MemoryKind)

#: 单次召回的条数上限。给得再多也只是把上下文塞满。
MAX_RECALL_LIMIT = 20


def register_memory_tools(
    cwd: str | Path,
    *,
    home: Path | None = None,
    max_records: int | None = None,
) -> MemoryStore:
    """注册长期记忆工具，返回底层的 store（便于调用方直接查/测）。

    ⚠️ 工具注册进的是**全局注册表**（与内置工具同一个），所以调用方要保证
    同一进程里只调用一次，否则会重复注册（同名覆盖，无害但浪费）。
    """
    kwargs = {} if max_records is None else {"max_records": max_records}
    store = store_for(cwd, home=home, **kwargs)

    def save_memory(content: str, kind: str = MemoryKind.NOTE.value, importance: float = 0.5) -> str:
        cleaned_kind = kind if kind in VALID_KINDS else MemoryKind.NOTE.value
        record = store.save(content, kind=cleaned_kind, importance=importance)
        note = "" if cleaned_kind == kind else f"（类别 {kind!r} 不认识，按 {cleaned_kind} 存了）"
        return f"已记住 [{record.kind}] {record.content}{note}"

    def search_memory(query: str, limit: int = 5, include_global: bool = False) -> str:
        hits = store.recall(query, limit=max(1, min(limit, MAX_RECALL_LIMIT)),
                            include_global=include_global)
        if not hits:
            return "没有找到相关的长期记忆。"
        # 带上相关度：模型能看到"哪条更像"，也能判断要不要换个说法再查一次
        lines = [f"{i}. [{hit.record.kind}] {hit.record.content}（相关度 {hit.lexical:.2f}）"
                 for i, hit in enumerate(hits, 1)]
        return "\n".join(lines)

    register(RegisteredTool(
        spec=ToolSpec(
            name="save_memory",
            description=(
                "把一条值得跨会话记住的信息存进长期记忆。"
                "适合存：项目约定、用户偏好、做过的技术决策及其理由。"
                "不适合存：文件内容、临时代码片段、这次任务的中间结果。"
                "内容要写成一句能独立看懂的话（将来没有上下文也要能读懂）。"
            ),
            input_model={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容，一句话说清"},
                    "kind": {"type": "string", "enum": list(VALID_KINDS),
                             "description": "fact=事实 / preference=偏好 / decision=决策 / note=其它"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1,
                                   "description": "重要程度，0~1，默认 0.5"},
                },
                "required": ["content"],
            },
            risk=Risk.LOW,
            required_keys=("content",),
        ),
        handler=save_memory,
        # 写自己的库，不是工作区文件——有副作用但风险低，不值得每次都问
        read_only=False,
        concurrency_safe=False,
    ))

    register(RegisteredTool(
        spec=ToolSpec(
            name="search_memory",
            description=(
                "按关键词检索长期记忆，返回相关的历史记录。"
                "在需要回忆项目约定、用户偏好或之前的技术决策时使用。"
                "查不到就是查不到，不要为了凑答案反复换词查。"
            ),
            input_model={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，用具体的关键词而不是整句话"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECALL_LIMIT,
                              "description": "最多返回几条，默认 5"},
                    "include_global": {"type": "boolean",
                                       "description": "是否连带查跨项目的用户级记忆，默认否"},
                },
                "required": ["query"],
            },
            risk=Risk.LOW,
            required_keys=("query",),
        ),
        handler=search_memory,
        read_only=True,
        concurrency_safe=True,
    ))

    return store
