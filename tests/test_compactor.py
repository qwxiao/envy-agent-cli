"""模块4 测试：token 估算口径 + 切分红线（工具组完整性）。

切分红线是本模块唯一的"错了就 400"，所以它的测试不写"我喂几个例子看输出对不对"，
而是**用一份独立实现的判定做对照，穷举每一个切分点**——避免测试跟着实现一起错。
"""

import json
import random
from pathlib import Path

import pytest

from envy_agent_cli.context.compactor import (
    MESSAGE_OVERHEAD_TOKENS,
    _is_safe_split,
    _safe_boundary,
    _split_violations,
    estimate_message_tokens,
    estimate_text_tokens,
    estimate_tokens,
)

SRC = Path(__file__).resolve().parents[1] / "src"


# ---------------------------------------------------------------- 构造工具

def call(*call_ids: str) -> dict:
    """一条带工具调用请求的 assistant 消息（模型一次可以要多个工具）。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
            for cid in call_ids
        ],
    }


def reply(call_id: str, content: str = "结果") -> dict:
    """一条工具执行结果。"""
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def conversation() -> list[dict]:
    """一段典型历史：一次双调用工具组 + 一次单调用工具组，中间夹着正文轮次。"""
    return [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "看看这个项目"},
        call("c1", "c2"),        # 2
        reply("c1"),             # 3
        reply("c2"),             # 4
        {"role": "assistant", "content": "两个文件都读完了"},   # 5
        call("c3"),              # 6
        reply("c3"),             # 7
        {"role": "assistant", "content": "结论是……"},         # 8
    ]


def orphan_tool_naive(messages: list[dict], split: int) -> bool:
    """独立实现的对照片断（不调用被测代码）：保留区里有没有"声明被切走"的工具结果。"""
    older_declared = set()
    kept_declared = set()
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        target = older_declared if index < split else kept_declared
        for item in message.get("tool_calls") or ():
            target.add(item["id"])
    for message in messages[split:]:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id in older_declared and call_id not in kept_declared:
                return True
    return False


# ---------------------------------------------------------------- token 估算

def test_empty_text_estimates_zero():
    assert estimate_text_tokens("") == 0


def test_chinese_counts_one_token_per_character():
    """中文按 1 字 1 token——这是"宁高不宁低"的关键：
    若按"3 字符 1 token"算，中文会被低估约 4 倍，保护就没了。"""
    assert estimate_text_tokens("你好世界") == 4
    assert estimate_text_tokens("上下文压缩器") == 6


def test_non_chinese_counts_three_chars_per_token_rounded_up():
    assert estimate_text_tokens("abcdef") == 2      # 6 / 3
    assert estimate_text_tokens("abcd") == 2        # 4 / 3 向上取整
    assert estimate_text_tokens("a") == 1


def test_mixed_text_adds_both_halves():
    # 2 个中文 + 6 个非中文 → 2 + 2
    assert estimate_text_tokens("你好abcdef") == 4


def test_message_has_fixed_overhead_even_when_empty():
    assert estimate_message_tokens({"role": "user", "content": None}) == MESSAGE_OVERHEAD_TOKENS
    assert estimate_message_tokens({"role": "user"}) == MESSAGE_OVERHEAD_TOKENS


def test_zero_message_history_costs_nothing():
    assert estimate_tokens([]) == 0


def test_tool_call_declaration_is_counted():
    """调用声明单独序列化计算——它和正文一样要发给模型。"""
    bare = {"role": "assistant", "content": None}
    with_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
    ]}
    assert estimate_tokens([with_call]) > estimate_tokens([bare])


def test_non_string_content_is_serialized():
    """多段 content（list）不能当空处理。"""
    blocks = [{"type": "text", "text": "一段很长的正文内容"}]
    assert estimate_tokens([{"role": "user", "content": blocks}]) > MESSAGE_OVERHEAD_TOKENS


def test_tool_definitions_are_counted():
    """工具定义每轮重发，几十个 Schema 是几千 token，不算它就是算错账。"""
    messages = [{"role": "user", "content": "你好"}]
    schemas = [
        {"type": "function",
         "function": {"name": f"tool_{i}", "description": "一个用来演示的工具定义",
                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
        for i in range(20)
    ]
    assert estimate_tokens(messages, schemas) > estimate_tokens(messages) + 200


def test_tool_definitions_default_to_empty():
    messages = [{"role": "user", "content": "你好"}]
    assert estimate_tokens(messages) == estimate_tokens(messages, None) == estimate_tokens(messages, [])


def test_estimate_does_not_depend_on_previous_calls():
    """纯函数：同样的输入永远同样的输出（不做跨调用校准，估算不许有状态）。"""
    messages = conversation()
    assert estimate_tokens(messages) == estimate_tokens(messages)
    assert estimate_tokens(list(messages)) == estimate_tokens(messages)


# ---------------------------------------------------------------- 切分红线

@pytest.mark.parametrize("split,expected", [
    (0, True),    # 全进保留区
    (1, True),    # 切在 user 之后
    (2, True),    # 切在工具组之前
    (3, False),   # 切在声明与应答之间 → 孤儿 c1
    (4, False),   # 切在双调用组中间 → 孤儿 c2
    (5, True),    # 切在工具组之后
    (6, True),
    (7, False),   # 第二组同样
    (8, True),
    (9, True),    # 全进摘要（保留区为空）
])
def test_safe_split_on_known_conversation(split, expected):
    assert _is_safe_split(conversation(), split) is expected


def test_safe_split_matches_independent_oracle_at_every_position():
    """穷举每个切分点，与独立实现的判据逐一对齐。"""
    messages = conversation()
    for split in range(len(messages) + 1):
        assert _is_safe_split(messages, split) is not orphan_tool_naive(messages, split), \
            f"切分点 {split} 的判定与对照实现不一致"


def test_safe_split_rejects_out_of_range():
    messages = conversation()
    assert _is_safe_split(messages, -1) is False
    assert _is_safe_split(messages, len(messages) + 1) is False


def test_multi_call_group_is_all_or_nothing():
    """模型一次要三个工具：这一组要么整组留下，要么整组进摘要。"""
    messages = [
        {"role": "user", "content": "读三个文件"},
        call("c1", "c2", "c3"),
        reply("c1"), reply("c2"), reply("c3"),
        {"role": "assistant", "content": "都读完了"},
    ]
    assert _is_safe_split(messages, 1) is True     # 整组留下
    assert _is_safe_split(messages, 5) is True     # 整组进摘要
    for split in (2, 3, 4):                        # 组中间：任何一刀都会造孤儿
        assert _is_safe_split(messages, split) is False


def test_safe_boundary_pushes_forward_to_group_edge():
    messages = conversation()
    assert _safe_boundary(messages, 3) == 5    # 推到双调用组之后
    assert _safe_boundary(messages, 7) == 8    # 推到单调用组之后
    assert _safe_boundary(messages, 5) == 5    # 本来就安全的位置不动
    assert _safe_boundary(messages, 2) == 2


def test_safe_boundary_clamps_candidates():
    messages = conversation()
    assert _safe_boundary(messages, -5) == 0
    assert _safe_boundary(messages, 999) == len(messages)


def test_safe_boundary_reports_no_safe_position_on_dangling_declaration():
    """历史末尾留着一个没有应答的调用声明：任何切法都不安全，返回 len 表示"别压"。"""
    messages = [
        {"role": "user", "content": "开始"},
        call("c1"),
        reply("c1"),
        call("c2"),          # 中途终止留下的悬空声明
    ]
    assert _split_violations(messages, 3)[1] == ["c2"]
    assert _safe_boundary(messages, 3) == len(messages)
    assert _safe_boundary(messages, 0) == len(messages)


def test_no_orphan_across_many_random_conversations():
    """随机造 1000 段历史、随机取切分点：凡判定为安全的，保留区里一定没有孤儿工具结果。"""
    rng = random.Random(20260918)
    for _ in range(1000):
        messages: list[dict] = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
        serial = 0
        for _ in range(rng.randint(1, 6)):
            serial += 1
            ids = [f"c{serial}_{k}" for k in range(rng.randint(1, 3))]
            messages.append(call(*ids))
            messages.extend(reply(cid) for cid in ids)
            if rng.random() < 0.5:
                messages.append({"role": "assistant", "content": "中间结论"})
        split = rng.randint(0, len(messages))
        if _is_safe_split(messages, split):
            assert not orphan_tool_naive(messages, split)


def test_safe_boundary_never_returns_an_unsafe_position():
    """`_safe_boundary` 的返回值要么是安全切分点，要么是 len（放弃压缩）。"""
    rng = random.Random(7)
    for _ in range(500):
        messages: list[dict] = [{"role": "user", "content": "Q"}]
        for serial in range(rng.randint(1, 5)):
            ids = [f"c{serial}", f"c{serial}b"]
            messages.append(call(*ids))
            messages.extend(reply(cid) for cid in ids)
        candidate = rng.randint(0, len(messages))
        boundary = _safe_boundary(messages, candidate)
        assert boundary == len(messages) or _is_safe_split(messages, boundary)
        assert boundary >= candidate


# ---------------------------------------------------------------- 分层与零 IO

def test_compactor_has_no_io_dependencies():
    """`compactor.py` 纯计算：摘要能力由外部注入，这里不许 import 任何 llm/ 的东西。"""
    source = (SRC / "envy_agent_cli" / "context" / "compactor.py").read_text(encoding="utf8")
    for forbidden in ("envy_agent_cli.llm", "import httpx", "import requests", "open("):
        assert forbidden not in source, f"compactor.py 出现了 {forbidden}"


def test_estimator_is_dependency_free():
    """估算不依赖任何 tokenizer 包。"""
    source = (SRC / "envy_agent_cli" / "context" / "compactor.py").read_text(encoding="utf8")
    assert "tiktoken" not in source


def test_estimate_matches_serialized_size_direction():
    """同一段正文，序列化越长估算越大（单调性，防实现写反）。"""
    short = [{"role": "assistant", "content": "短"}]
    long = [{"role": "assistant", "content": "长" * 100}]
    assert estimate_tokens(long) > estimate_tokens(short)
    assert json.dumps({"role": "assistant"})  # 保证 json 仍被使用（多段 content 路径）
