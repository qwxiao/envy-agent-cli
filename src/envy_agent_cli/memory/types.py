"""模块6 · 长期记忆的数据契约。

**三层记忆的边界**（这是本模块存在的理由）：

| 层 | 载体 | 生命周期 | 归谁管 |
|---|---|---|---|
| 短期 | 会话 `messages` | 当次任务 | 模块2 主循环 |
| 中期 | 压缩摘要 | 会话内 | 模块4 压缩器 |
| **长期** | **SQLite（本模块）** | **跨会话** | 这里 |

**为什么正好是三层**：每层对应一个**不同的丢失场景**——
短期丢在"任务结束"，中期丢在"窗口满了"，长期丢在"换个会话就没了"。
两层不够（覆盖不了跨会话），四层没有新的丢失场景可补（再加只会多一个要维护的同步点）。
判据是"丢在哪"，不是"存多久"。

**与压缩器的关系**：压缩管**空间**（窗口内太满），长期记忆管**时间**（跨会话不丢）。
两者不重叠、不互相调用——压缩永不写入记忆，记忆永不触发压缩。

**召回为什么不做成"每轮自动注入"**：那等于在 `messages` 之外开第二个输入通道，
与架构已定的纪律冲突（一个输入通道 = 一个真相来源）。
本模块把召回做成**工具**——模型自己决定何时查，结果作为 tool result 进 `messages`，
输入通道仍然只有一条。
"""

import hashlib
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Final

#: 用户级 scope：跨项目可见。**必须显式指定**，默认不查它——
#: 否则在一个项目里写着写着，会突然冒出另一个项目的约定。
GLOBAL_SCOPE: Final[str] = "user:global"

#: 项目级 scope 的前缀
PROJECT_SCOPE_PREFIX: Final[str] = "project:"

#: 单条记忆的长度上限。超过的基本是"把文件贴进来了"，那不是记忆是缓存。
MAX_CONTENT_CHARS: Final[int] = 2000


class MemoryKind(str, Enum):
    """记忆的分类。

    分这四类不是为了好看，是为了**召回时能按类过滤**：
    查"项目怎么构建"时不需要看到"用户喜欢简短回答"。
    """

    FACT = "fact"              # 事实：这个项目用 hatchling 构建
    PREFERENCE = "preference"  # 偏好：用户希望回答先给结论
    DECISION = "decision"      # 决策：选 SQLite 而不是向量库，因为……
    NOTE = "note"              # 其它零散记录


def normalize_content(text: str) -> str:
    """归一化：NFKC + 折叠空白 + 去首尾。

    归一化是**去重的前提**——同一条记忆用全角/半角、多个空格打字进来，
    不归一化就会存成两条。
    """
    folded = unicodedata.normalize("NFKC", text)
    return " ".join(folded.split()).strip()


def content_hash(text: str) -> str:
    """内容指纹（归一化后再哈希）。

    ⚠️ 大小写**不折叠**——中文无大小写，英文里 `Config` 与 `config` 可能是两个东西。
    宁可漏去重（存两条），不可错去重（丢一条）。
    """
    return hashlib.sha256(normalize_content(text).encode("utf8")).hexdigest()[:32]


def project_scope(cwd: str) -> str:
    """由工作目录算项目 scope。

    ⚠️ 用**路径原文**而不是哈希：scope 会出现在错误信息与工具返回里，
    哈希值让人无法判断"这条记忆属于哪个项目"。路径本身就是最好的标识。
    """
    return f"{PROJECT_SCOPE_PREFIX}{cwd}"


@dataclass(slots=True)
class MemoryRecord:
    """一条长期记忆。

    Attributes:
        importance: 重要性（0~1）。影响召回排序与淘汰优先级，由写入方给。
        confidence: 置信度（0~1）。同样内容被重复写入时取 max——
            "又说了一遍"本身就是置信度上升的证据。
        access_count: 被召回次数。**淘汰时它救不了低价值的条目**（权重很低），
            但对于同等重要的两条，常被用到的应该留下。
    """

    scope: str
    content: str
    kind: str = MemoryKind.NOTE.value
    importance: float = 0.5
    confidence: float = 1.0
    created_at: str = ""
    updated_at: str = ""
    expires_at: str | None = None
    access_count: int = 0
    content_hash: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.content)

    def __repr__(self) -> str:
        # 内容可能很长，repr 里只给长度与指纹——日志里不该出现整段记忆
        return (f"MemoryRecord(id={self.id}, scope={self.scope!r}, kind={self.kind!r}, "
                f"len={len(self.content)}, hash={self.content_hash[:8]}, "
                f"importance={self.importance:g})")


@dataclass(slots=True)
class RecallHit:
    """一次召回命中：记录 + 分数 + 命中的特征（便于解释"为什么是它"）。"""

    record: MemoryRecord
    score: float
    lexical: float = 0.0
    """词法重叠度（0~1）。单独留一份，是为了让 `search_memory` 能解释排序依据——
    模型看到"这条为什么排第一"，下次查询会写得更准。"""
