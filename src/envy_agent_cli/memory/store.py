"""模块6 · 长期记忆存储：SQLite + 词法召回。

**为什么是 SQLite 而不是向量库**：

长期记忆的量级是"几十到几百条"，查询是"找和这句话相关的几条"。
这个规模下，向量检索的收益（语义泛化）**抵不过它的代价**——
要一个 embedding 服务、要处理索引更新、要解释"为什么召回了这条"（向量说不清）。
而词法召回在这个量级上够用，且**可解释**：模型能看到是哪几个词命中的。

> 真需要语义泛化时再上向量——那时数据量和查询复杂度会自己证明这个需求，
> 而不是现在先猜。

**为什么每次操作新建连接**：`ToolRuntime` 用线程池跑 handler（为了超时可控），
所以 handler 里的存储调用可能发生在非主线程。SQLite 的连接默认不允许跨线程，
与其引入锁和 `check_same_thread=False`，不如每次开一个新连接——
CLI 场景下调用频率极低，"开连接"的开销远小于"想清楚线程安全"的成本。
"""

import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from envy_agent_cli.memory.types import (
    GLOBAL_SCOPE,
    MAX_CONTENT_CHARS,
    MemoryKind,
    MemoryRecord,
    RecallHit,
    normalize_content,
    project_scope,
)

#: 单个 scope 的容量上限。超了按"重要性低、用得少、最旧"的顺序淘汰。
DEFAULT_MAX_RECORDS = 500

#: 召回评分的权重。**词法占大头**——记忆检索要的是"相关"，
#: 重要性只是同等相关时的排序依据，不能反过来盖过相关性，
#: 否则每次都会召回那几条"很重要但和当前问题无关"的记忆。
W_LEXICAL = 0.72
W_IMPORTANCE = 0.18
W_RECENCY = 0.10

#: 时间衰减的半衰期（天）。30 天前的记忆权重减半——
#: 项目约定会过期，"三个月前我们决定用 X"未必还有效。
RECENCY_HALFLIFE_DAYS = 30.0

_EN_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    importance   REAL    NOT NULL DEFAULT 0.5,
    confidence   REAL    NOT NULL DEFAULT 1.0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    expires_at   TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_scope_hash ON memories(scope, content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
"""


def lexical_features(text: str) -> set[str]:
    """提取词法特征：英文/数字词（小写）+ 中文 2-gram。

    中文没有空格分词，2-gram 是**不引入分词器依赖**的实用做法——
    对"找相关记忆"这个精度要求足够，省掉一个 jieba/HanLP 依赖。
    """
    lowered = text.lower()
    features = set(_EN_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            features.add(run)
        else:
            features.update(run[i:i + 2] for i in range(len(run) - 1))
    return features


def lexical_overlap(query: set[str], content: str) -> float:
    """查询特征被内容覆盖的比例（0~1）。

    ⚠️ 分母是**查询**的特征数，不是两者的并集——问的是"我问的这些词，
    有多少能在记忆里找到"，不是"两条文本有多像"。后者会让短查询永远拿低分。
    """
    if not query:
        return 0.0
    return len(query & lexical_features(content)) / len(query)


def _recency(updated_at: str, now: datetime) -> float:
    """时间衰减：半衰期 30 天。解析不出来就当 1.0（不因为脏数据惩罚一条记忆）。"""
    try:
        moment = datetime.fromisoformat(updated_at)
    except (TypeError, ValueError):
        return 1.0
    days = max(0.0, (now - moment).total_seconds() / 86400.0)
    return math.pow(0.5, days / RECENCY_HALFLIFE_DAYS)


class MemoryStore:
    """一个 scope 的长期记忆库。

    ⚠️ **实例绑定一个 scope**，而不是"一个库装所有 scope、查询时过滤"——
    后者只要有一处忘了加 `WHERE scope = ?`，就是一次跨项目污染。
    把 scope 焊在实例上，忘不掉。
    """

    def __init__(self, path: Path | str, *, scope: str, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self.path = Path(path)
        self.scope = scope
        self.max_records = max_records
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---------- 写 ----------

    def save(
        self,
        content: str,
        *,
        kind: str = MemoryKind.NOTE.value,
        importance: float = 0.5,
        confidence: float = 1.0,
        ttl_days: float | None = None,
    ) -> MemoryRecord:
        """写入一条记忆。同 scope 内**内容相同则更新而不是新增**。

        Raises:
            ValueError: 内容为空或超长。**空记忆不是记忆**，超长的是文件不是记忆。
        """
        cleaned = normalize_content(content)
        if not cleaned:
            raise ValueError("记忆内容不能为空")
        if len(cleaned) > MAX_CONTENT_CHARS:
            raise ValueError(
                f"记忆内容过长（{len(cleaned)} 字符 > {MAX_CONTENT_CHARS}）——"
                f"记忆应当是一句可复用的话，不是一段材料"
            )

        now = datetime.now()
        stamp = now.isoformat(timespec="seconds")
        expires = (now + timedelta(days=ttl_days)).isoformat(timespec="seconds") if ttl_days else None
        record = MemoryRecord(
            scope=self.scope, content=cleaned, kind=str(kind),
            importance=_clamp(importance), confidence=_clamp(confidence),
            created_at=stamp, updated_at=stamp, expires_at=expires,
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories (scope, content, kind, importance, confidence,
                                      created_at, updated_at, expires_at, access_count, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(scope, content_hash) DO UPDATE SET
                    importance = MAX(memories.importance, excluded.importance),
                    confidence = MAX(memories.confidence, excluded.confidence),
                    updated_at = excluded.updated_at,
                    kind       = excluded.kind
                """,
                (record.scope, record.content, record.kind, record.importance, record.confidence,
                 record.created_at, record.updated_at, record.expires_at, record.content_hash),
            )
            row = conn.execute(
                "SELECT * FROM memories WHERE scope = ? AND content_hash = ?",
                (self.scope, record.content_hash),
            ).fetchone()
            self._enforce_quota(conn)
        return _to_record(row) if row is not None else record

    def forget(self, memory_id: int) -> bool:
        """删一条。返回是否真的删掉了（便于工具如实回报）。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ? AND scope = ?",
                                  (memory_id, self.scope))
            return cursor.rowcount > 0

    # ---------- 读 ----------

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: tuple[str, ...] | None = None,
        min_score: float = 0.0,
        include_global: bool = False,
        mark_access: bool = True,
    ) -> list[RecallHit]:
        """按相关性召回。

        Args:
            min_score: 分数门槛。**低于它的不返回**——"没找到相关的"是合法结果，
                硬塞几条不相关的进上下文，比什么都不给更糟。
            include_global: 是否连带查用户级记忆。默认 `False`：
                跨项目可见必须显式要求，不能默认发生。
        """
        scopes = [self.scope] + ([GLOBAL_SCOPE] if include_global and self.scope != GLOBAL_SCOPE else [])
        placeholders = ",".join("?" * len(scopes))
        params: list[object] = list(scopes)

        sql = f"SELECT * FROM memories WHERE scope IN ({placeholders})"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        now = datetime.now()
        sql += " AND (expires_at IS NULL OR expires_at > ?)"
        params.append(now.isoformat(timespec="seconds"))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        query_features = lexical_features(normalize_content(query))
        hits: list[RecallHit] = []
        for row in rows:
            record = _to_record(row)
            overlap = lexical_overlap(query_features, record.content)
            # ⚠️ 一个查询词都没命中 = 不相关。**重要性高不构成"相关"**——
            # 少了这一条，任何查询都会把库里最"重要"的几条捞回来，
            # 而往上下文里塞不相关的内容，比什么都不给更糟（模型会拿它当事实用）。
            # importance 与 recency 只在**有词汇重叠的候选集内**做排序微调。
            if overlap <= 0.0:
                continue
            score = (W_LEXICAL * overlap
                     + W_IMPORTANCE * record.importance
                     + W_RECENCY * _recency(record.updated_at, now))
            if score < min_score:
                continue
            hits.append(RecallHit(record=record, score=score, lexical=overlap))

        # 分数高的在前；同分时新写的在前。
        # 两次排序而不是一个复合 key：Python 的 sort 是稳定的，
        # 先按时间降序铺好，再按分数排一次，同分项自然保持"新的在前"。
        hits.sort(key=lambda h: h.record.updated_at, reverse=True)
        hits.sort(key=lambda h: h.score, reverse=True)
        hits = hits[:max(0, limit)]

        if mark_access and hits:
            self._bump_access([h.record.id for h in hits if h.record.id is not None])
        return hits

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM memories WHERE scope = ?", (self.scope,)).fetchone()
        return int(row["n"]) if row else 0

    def scopes(self) -> list[str]:
        """库里出现过的所有 scope。排查"记忆去哪了"时先用它。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT scope FROM memories ORDER BY scope").fetchall()
        return [r["scope"] for r in rows]

    # ---------- 内部 ----------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _bump_access(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            conn.execute(
                f"UPDATE memories SET access_count = access_count + 1 "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )

    def _enforce_quota(self, conn: sqlite3.Connection) -> None:
        """超容量时淘汰：**重要性低 → 用得少 → 最旧**。

        顺序不能反：先按"重不重要"筛，重要性相同的才看使用频次。
        反过来会让一条极重要但没被召回过的新记忆被一条常被召回的水货挤掉。
        """
        row = conn.execute("SELECT COUNT(*) AS n FROM memories WHERE scope = ?", (self.scope,)).fetchone()
        if row is None or int(row["n"]) <= self.max_records:
            return
        overflow = int(row["n"]) - self.max_records
        conn.execute(
            """
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories WHERE scope = ?
                ORDER BY importance ASC, access_count ASC, updated_at ASC
                LIMIT ?
            )
            """,
            (self.scope, overflow),
        )


def _to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"], scope=row["scope"], content=row["content"], kind=row["kind"],
        importance=row["importance"], confidence=row["confidence"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        expires_at=row["expires_at"], access_count=row["access_count"],
        content_hash=row["content_hash"],
    )


def _clamp(value: float) -> float:
    """把 0~1 的权重夹到合法区间。越界不报错——工具的入参来自模型，宽容比严格好。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, number))


def default_store_path(home: Path | None = None) -> Path:
    """默认库文件位置：`~/.envy/memory.db`。

    **放在用户目录而不是项目目录**：项目目录会被 `git clean`、会被换分支、
    会被整个删掉重建——记忆不该跟着代码一起消失。
    隔离靠 scope 字段（`project:<路径>`），不靠文件位置。
    """
    root = home if home is not None else Path.home()
    return root / ".envy" / "memory.db"


def store_for(cwd: str | Path, *, home: Path | None = None, **kwargs) -> MemoryStore:
    """按工作目录建一个 **项目级** store。"""
    return MemoryStore(default_store_path(home), scope=project_scope(str(Path(cwd).resolve())), **kwargs)
