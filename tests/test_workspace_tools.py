"""工作区增强工具测试：grep / edit_file / run_command。

三个工具各有一条"必须拒绝"的路径，那些是本文件的重点——
工具的价值一半在"能做什么"，另一半在"拒绝做什么"。
"""

import sys

import pytest

from envy_agent_cli.tools import registry
from envy_agent_cli.tools.workspace import (
    FORBIDDEN_COMMANDS,
    MAX_OUTPUT_CHARS,
    register_workspace_tools,
)


@pytest.fixture
def ws(tmp_path):
    """注册好工具的工作区，返回 (root, 取工具的函数)。"""
    saved = dict(registry._REGISTRY)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf8")
    (tmp_path / "src" / "b.py").write_text("def beta():\n    return alpha()\n", encoding="utf8")
    (tmp_path / "notes.md").write_text("alpha 也出现在这里\n", encoding="utf8")

    register_workspace_tools(tmp_path)
    yield tmp_path, registry.get
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


# ---------------------------------------------------------------- grep


def test_grep_finds_matches_with_location(ws):
    root, get = ws
    out = get("grep").handler(pattern="alpha")

    assert "src/a.py:1" in out
    assert "src/b.py:2" in out
    assert "notes.md:1" in out


def test_grep_scopes_to_subpath(ws):
    root, get = ws
    out = get("grep").handler(pattern="alpha", path="notes.md")
    assert "notes.md" in out and "a.py" not in out


def test_grep_reports_no_match_honestly(ws):
    root, get = ws
    assert "没有匹配" in get("grep").handler(pattern="绝不会出现的字符串xyz")


def test_grep_rejects_bad_regex(ws):
    """抛出带原因的异常，模型能自己改正则。"""
    root, get = ws
    with pytest.raises(ValueError) as exc:
        get("grep").handler(pattern="[未闭合")
    assert "正则" in str(exc.value)


def test_grep_truncates_large_result(ws):
    """大输出必须截断——原样回灌会把上下文一次打满。"""
    root, get = ws
    (root / "big.txt").write_text("\n".join(f"hit {i}" for i in range(500)), encoding="utf8")

    out = get("grep").handler(pattern="hit", max_matches=10)
    assert "截断" in out
    assert len(out.splitlines()) < 20


def test_grep_skips_ignored_dirs(ws):
    root, get = ws
    junk = root / "__pycache__"
    junk.mkdir()
    (junk / "cached.py").write_text("alpha 在缓存里\n", encoding="utf8")

    assert "__pycache__" not in get("grep").handler(pattern="alpha")


def test_grep_survives_binary_files(ws):
    """二进制文件该被跳过，而不是让整次搜索崩掉。"""
    root, get = ws
    (root / "blob.bin").write_bytes(bytes(range(256)) * 10)
    assert "alpha" in get("grep").handler(pattern="alpha")


def test_grep_missing_path_raises(ws):
    root, get = ws
    with pytest.raises(FileNotFoundError):
        get("grep").handler(pattern="x", path="不存在的目录")


# ---------------------------------------------------------------- edit_file


def test_edit_file_replaces_unique_match(ws):
    root, get = ws
    out = get("edit_file").handler(path="src/a.py", old_string="return 1", new_string="return 2")

    assert "已修改" in out
    assert "return 2" in (root / "src" / "a.py").read_text(encoding="utf8")


def test_edit_file_refuses_when_not_found(ws):
    """找不到就报错，**绝不静默新建**——那是 write_file 的语义。"""
    root, get = ws
    with pytest.raises(ValueError) as exc:
        get("edit_file").handler(path="src/a.py", old_string="def 不存在", new_string="x")
    assert "逐字符一致" in str(exc.value)
    assert "return 1" in (root / "src" / "a.py").read_text(encoding="utf8")   # 文件没动


def test_edit_file_refuses_ambiguous_match(ws):
    """⚠️ 多处匹配时拒绝执行，而不是"替换第一处"——猜错一处就是一处静默的错误修改。"""
    root, get = ws
    (root / "dup.py").write_text("value = 1\nvalue = 1\n", encoding="utf8")

    with pytest.raises(ValueError) as exc:
        get("edit_file").handler(path="dup.py", old_string="value = 1", new_string="value = 2")
    assert "2 处" in str(exc.value)


def test_edit_file_allows_ambiguous_when_disambiguated(ws):
    root, get = ws
    (root / "dup.py").write_text("value = 1\nvalue = 1\n", encoding="utf8")

    get("edit_file").handler(path="dup.py", old_string="value = 1\nvalue = 1",
                             new_string="value = 2\nvalue = 3")
    assert (root / "dup.py").read_text(encoding="utf8") == "value = 2\nvalue = 3\n"


@pytest.mark.parametrize("old, new, keyword", [
    ("", "x", "不能为空"),          # 空串会匹配每一处
    ("return 1", "return 1", "没有任何效果"),
])
def test_edit_file_rejects_degenerate_edits(ws, old, new, keyword):
    root, get = ws
    with pytest.raises(ValueError) as exc:
        get("edit_file").handler(path="src/a.py", old_string=old, new_string=new)
    assert keyword in str(exc.value)


def test_edit_file_missing_file(ws):
    root, get = ws
    with pytest.raises(FileNotFoundError):
        get("edit_file").handler(path="无此文件.py", old_string="a", new_string="b")


# ---------------------------------------------------------------- run_command


def test_run_command_executes(ws):
    root, get = ws
    out = get("run_command").handler(args=[sys.executable, "-c", "print('hello')"])
    assert "hello" in out


def test_run_command_reports_nonzero_exit(ws):
    root, get = ws
    out = get("run_command").handler(args=[sys.executable, "-c", "import sys; sys.exit(3)"])
    assert "退出码 3" in out


def test_run_command_reports_stderr(ws):
    root, get = ws
    out = get("run_command").handler(
        args=[sys.executable, "-c", "import sys; print('出错了', file=sys.stderr)"])
    assert "stderr" in out and "出错了" in out


@pytest.mark.parametrize("program", sorted(FORBIDDEN_COMMANDS)[:4])
def test_run_command_blocks_forbidden_programs(ws, program):
    """硬拒只针对"执行了就不可逆、且不属于任何正常编码任务"的命令。"""
    root, get = ws
    with pytest.raises(PermissionError) as exc:
        get("run_command").handler(args=[program, "-x"])
    assert "禁止名单" in str(exc.value)


def test_run_command_allows_everyday_commands(ws):
    """`rm` 这类日常命令走审批而不是硬拒——全硬拒只会逼用户关掉整个审批模式。"""
    root, get = ws
    assert "rm" not in FORBIDDEN_COMMANDS


def test_run_command_missing_executable(ws):
    root, get = ws
    with pytest.raises(FileNotFoundError):
        get("run_command").handler(args=["definitely-not-a-real-binary-xyz"])


def test_run_command_empty_args(ws):
    root, get = ws
    with pytest.raises(ValueError):
        get("run_command").handler(args=[])


def test_run_command_truncates_output(ws):
    root, get = ws
    out = get("run_command").handler(
        args=[sys.executable, "-c", f"print('x' * {MAX_OUTPUT_CHARS * 2})"])
    assert "截断" in out
    assert len(out) < MAX_OUTPUT_CHARS + 200


def test_run_command_is_high_risk_and_needs_approval(ws):
    """治理属性本身就是设计的一部分——它决定了这个工具会被怎么对待。"""
    root, get = ws
    tool = get("run_command")
    assert tool.spec.risk.value == "high"
    assert tool.spec.requires_approval is True
    assert tool.read_only is False


def test_registered_tool_governance_attributes(ws):
    root, get = ws
    assert get("grep").read_only is True and get("grep").concurrency_safe is True
    assert get("edit_file").read_only is False and get("edit_file").concurrency_safe is False
    assert get("edit_file").spec.requires_approval is True
