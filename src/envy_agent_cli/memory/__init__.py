"""模块6 · 长期记忆：跨会话不丢的那一层。

三层记忆里的最外层——短期在 `messages`、中期在压缩摘要、长期在这里。
它解决的是**唯一一个跨会话的丢失场景**：任务结束、窗口清空之后，
"这个项目当初为什么这么定"还能被找回来。

对外的概念：`MemoryStore`（存与查）、`register_memory_tools`（暴露给模型）。

**隔离靠 scope，不靠库文件位置**：一个库装所有项目，每条记忆带 `project:<路径>`
或 `user:global`，查询时只取当前 scope（`user:global` 要显式要求）。
"""

from envy_agent_cli.memory.store import (
    MemoryStore,
    default_store_path,
    lexical_features,
    store_for,
)
from envy_agent_cli.memory.tools import register_memory_tools
from envy_agent_cli.memory.types import (
    GLOBAL_SCOPE,
    MemoryKind,
    MemoryRecord,
    RecallHit,
    content_hash,
    normalize_content,
    project_scope,
)

__all__ = [
    "GLOBAL_SCOPE",
    "MemoryKind",
    "MemoryRecord",
    "MemoryStore",
    "RecallHit",
    "content_hash",
    "default_store_path",
    "lexical_features",
    "normalize_content",
    "project_scope",
    "register_memory_tools",
    "store_for",
]
