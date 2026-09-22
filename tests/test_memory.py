"""模块6 · 长期记忆测试。

重点验三件事：

1. **去重可靠**——同一句话用不同写法打进来，不该存成多条
2. **不相关就不返回**——往上下文塞无关内容比什么都不给更糟
3. **scope 隔离**——跨项目污染是这个模块最大的风险
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from envy_agent_cli.memory import (
    GLOBAL_SCOPE,
    MemoryKind,
    MemoryStore,
    content_hash,
    lexical_features,
    normalize_content,
    project_scope,
    register_memory_tools,
)
from envy_agent_cli.tools import registry


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "mem.db", scope=project_scope("/proj/a"))


@pytest.fixture
def clean_registry():
    saved = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


# ---------------------------------------------------------------- 归一化与指纹


@pytest.mark.parametrize("raw, expected", [
    ("  多个   空格  ", "多个 空格"),
    ("全角ＡＢＣ", "全角ABC"),
    ("换行\n与\t制表", "换行 与 制表"),
])
def test_normalize_content(raw, expected):
    assert normalize_content(raw) == expected


def test_hash_ignores_formatting_but_not_case():
    """大小写不折叠——`Config` 与 `config` 可能是两个东西，宁可漏去重。"""
    assert content_hash("用  SQLite\n存记忆") == content_hash("用 SQLite 存记忆")
    assert content_hash("Config") != content_hash("config")


def test_lexical_features_chinese_bigrams_and_english_words():
    features = lexical_features("用 hatchling 构建项目")
    assert "hatchling" in features
    assert "构建" in features and "建项" in features      # 中文 2-gram
    assert "用" in features                              # 单字也留一份


# ---------------------------------------------------------------- 写入与去重


def test_save_and_count(store):
    store.save("第一条记忆")
    store.save("第二条记忆")
    assert store.count() == 2


def test_same_content_updates_instead_of_duplicating(store):
    store.save("这个项目用 hatchling", importance=0.4)
    record = store.save("这个项目用  hatchling", importance=0.9)   # 多一个空格

    assert store.count() == 1
    assert record.importance == 0.9          # 重复写入取 max


def test_confidence_takes_max_on_rewrite(store):
    store.save("用户偏好简洁回答", confidence=0.6)
    record = store.save("用户偏好简洁回答", confidence=0.95)
    assert record.confidence == 0.95


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_empty_content_rejected(store, bad):
    with pytest.raises(ValueError):
        store.save(bad)


def test_overlong_content_rejected(store):
    """超长的是文件不是记忆——挡在这里，别让它把库撑坏。"""
    with pytest.raises(ValueError):
        store.save("啊" * 3000)


# ---------------------------------------------------------------- 召回


def test_recall_returns_empty_when_nothing_matches(store):
    """⚠️ 这条是本模块最容易写错的地方：不相关就该返回空，
    而不是把 importance 最高的几条硬塞回来。"""
    store.save("这个项目用 hatchling 构建", kind="fact", importance=1.0)
    store.save("用户偏好先给结论", kind="preference", importance=1.0)

    assert store.recall("量子纠缠 光合作用") == []


def test_recall_finds_relevant_and_ranks_by_lexical(store):
    store.save("这个项目用 hatchling 构建，不用 setuptools", kind="fact")
    store.save("用户偏好：回答先给结论", kind="preference")
    store.save("决策：选 SQLite 而不是向量库", kind="decision")

    hits = store.recall("为什么用 SQLite 不用向量库", limit=3)

    assert hits, "应当命中决策那条"
    assert hits[0].record.kind == MemoryKind.DECISION.value
    assert hits[0].lexical > 0


def test_recall_respects_limit(store):
    for i in range(5):
        store.save(f"关于部署的第 {i} 条说明")
    assert len(store.recall("部署 说明", limit=2)) == 2


def test_recall_filters_by_kind(store):
    store.save("项目用 hatchling 构建", kind="fact")
    store.save("偏好：构建输出要简洁", kind="preference")

    hits = store.recall("构建", kinds=(MemoryKind.FACT.value,))
    assert [h.record.kind for h in hits] == [MemoryKind.FACT.value]


def test_recall_marks_access(store):
    store.save("项目用 hatchling 构建")
    store.recall("hatchling")
    assert store.recall("hatchling")[0].record.access_count >= 1


def test_recall_can_skip_access_marking(store):
    store.save("项目用 hatchling 构建")
    store.recall("hatchling", mark_access=False)
    assert store.recall("hatchling", mark_access=False)[0].record.access_count == 0


# ---------------------------------------------------------------- scope 隔离


def test_scopes_are_isolated(tmp_path):
    """跨项目污染是这个模块最大的风险——不同 scope 必须互不可见。"""
    a = MemoryStore(tmp_path / "mem.db", scope=project_scope("/proj/a"))
    b = MemoryStore(tmp_path / "mem.db", scope=project_scope("/proj/b"))

    a.save("A 项目的构建方式是 X")
    b.save("B 项目的构建方式是 Y")

    assert [h.record.content for h in a.recall("构建方式")] == ["A 项目的构建方式是 X"]
    assert [h.record.content for h in b.recall("构建方式")] == ["B 项目的构建方式是 Y"]


def test_global_scope_requires_explicit_flag(tmp_path):
    project = MemoryStore(tmp_path / "mem.db", scope=project_scope("/proj/a"))
    global_store = MemoryStore(tmp_path / "mem.db", scope=GLOBAL_SCOPE)

    global_store.save("用户习惯用中文提问")

    assert project.recall("中文提问") == []                                  # 默认不查全局
    hits = project.recall("中文提问", include_global=True)
    assert [h.record.content for h in hits] == ["用户习惯用中文提问"]


def test_scopes_listing(store, tmp_path):
    MemoryStore(tmp_path / "mem.db", scope=GLOBAL_SCOPE).save("全局的一条")
    store.save("项目的一条")

    assert set(store.scopes()) == {project_scope("/proj/a"), GLOBAL_SCOPE}


# ---------------------------------------------------------------- 过期与淘汰


def test_expired_records_are_not_returned(store):
    store.save("这条马上就过期", ttl_days=0.00001)
    # 直接改库时间，比 sleep 稳
    past = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    with store._connect() as conn:
        conn.execute("UPDATE memories SET expires_at = ?", (past,))

    assert store.recall("过期") == []


def test_quota_evicts_least_important(tmp_path):
    store = MemoryStore(tmp_path / "mem.db", scope=project_scope("/p"), max_records=3)
    for i in range(5):
        store.save(f"第 {i} 条记录内容", importance=i / 10.0)   # 越后越重要

    assert store.count() == 3
    remaining = {h.record.content for h in store.recall("记录内容", limit=10)}
    assert "第 0 条记录内容" not in remaining      # 最不重要的先走
    assert "第 4 条记录内容" in remaining


def test_forget(store):
    record = store.save("要删掉的记忆")
    assert store.forget(record.id) is True
    assert store.count() == 0
    assert store.forget(record.id) is False       # 再删一次如实回报"没删到"


def test_forget_cannot_cross_scope(tmp_path):
    """删除也必须守 scope——否则 A 项目能删掉 B 项目的记忆。"""
    a = MemoryStore(tmp_path / "mem.db", scope=project_scope("/a"))
    b = MemoryStore(tmp_path / "mem.db", scope=project_scope("/b"))
    record = b.save("B 的记忆")

    assert a.forget(record.id) is False
    assert b.count() == 1


# ---------------------------------------------------------------- 工具暴露


def test_register_memory_tools(clean_registry, tmp_path):
    store = register_memory_tools(tmp_path, home=tmp_path)

    assert "save_memory" in registry.all_names()
    assert "search_memory" in registry.all_names()

    saved = registry.get("save_memory")
    assert saved.read_only is False and saved.concurrency_safe is False
    assert saved.spec.required_keys == ("content",)

    # 召回时会更新 access_count（淘汰排序要用），所以它**不是纯只读**。
    # 标成只读会让标记与行为对不上——被问"只读工具真的只读吗"就答不上来。
    searched = registry.get("search_memory")
    assert searched.read_only is False and searched.concurrency_safe is False

    saved.handler(content="项目用 hatchling 构建", kind="fact")
    assert "hatchling" in searched.handler(query="构建工具")


def test_search_reports_empty_honestly(clean_registry, tmp_path):
    register_memory_tools(tmp_path, home=tmp_path)
    assert "没有找到" in registry.get("search_memory").handler(query="完全无关的查询词")


def test_unknown_kind_degrades_to_note(clean_registry, tmp_path):
    """为一次拼写错误打断流程不值得，但要如实告诉模型降级了。"""
    register_memory_tools(tmp_path, home=tmp_path)
    result = registry.get("save_memory").handler(content="某条记忆", kind="不认识的类别")
    assert "note" in result and "不认识" in result


def test_store_lands_in_home_not_project(tmp_path):
    """库文件放用户目录：项目目录会被换分支、被 git clean，记忆不该跟着消失。"""
    store = register_memory_tools(tmp_path / "proj", home=tmp_path / "home")
    assert store.path == tmp_path / "home" / ".envy" / "memory.db"
    assert store.path.exists()
