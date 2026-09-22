"""CLI 参数层测试（Typer）。

只测**参数层**：解析、类型校验、退出码。
业务逻辑在 `_execute()`——它不该通过拼命令行字符串来测，那是另一层的成本，
也会让测试里的失败难以定位（分不清是参数没传对还是逻辑错）。
"""

from envy_agent_cli.cli import main


def test_help_exits_zero_and_lists_options(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for option in ("--provider", "--hitl", "--context-window", "--list-mcp"):
        assert option in out


def test_list_providers(capsys):
    assert main(["--list-providers"]) == 0
    out = capsys.readouterr().out
    assert "deepseek" in out and "glm" in out


def test_list_mcp_succeeds_even_without_config(capsys):
    assert main(["--list-mcp"]) == 0


def test_no_args_without_tty_prints_diagnostics(capsys):
    """管道里跑（stdin 不是终端）不能进 REPL——否则会静默挂住等永远不来的输入。"""
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "已配置凭证的厂商" in out


def test_invalid_integer_is_rejected(capsys):
    """类型校验由 Typer 生成——这正是换掉手写解析器的理由之一。"""
    assert main(["--context-window", "abc"]) == 2
    err = capsys.readouterr().err
    assert "参数错误" in err
    # Typer 原始信息不带选项名，所以我们补了一句 --help 指引
    assert "--help" in err


def test_unknown_option_is_rejected(capsys):
    assert main(["--nonexistent-option"]) == 2


def test_negative_round_limit_is_rejected(capsys):
    """`min=1` 由注解声明，不用手写分支。"""
    assert main(["--max-rounds", "0"]) == 2


def test_boolean_switch_pair_parses_both_directions(capsys):
    """`--compact/--no-compact` 是布尔开关对，两个方向都得能解析。"""
    assert main(["--list-providers", "--no-compact"]) == 0
    assert main(["--list-providers", "--compact"]) == 0


def test_negative_switches_parse(capsys):
    assert main(["--list-providers", "--no-mcp", "--no-memory", "--no-audit"]) == 0


def test_question_words_are_joined(capsys):
    """位置参数收集成列表——任务描述里的空格不该把它切成多个参数。"""
    # 用 --list-providers 短路，避免真的去连模型；这里只验参数能被接受
    assert main(["--list-providers", "读", "一下", "README"]) == 0
