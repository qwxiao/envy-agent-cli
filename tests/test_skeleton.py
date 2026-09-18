"""骨架自检：保证"封版的接口"没被悄悄改掉，以及"边界"没被越过去。

骨架阶段的测试不做行为验证（实现还没填），只做两件事：
1. **契约守卫**：七字段、三契约、事件类型、错误码，数量与名字对得上；
2. **边界守卫**：静态检查编排层没有绕过 Runtime 直接调工具——这是架构红线。
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "envy_agent_cli"


# ---------------------------------------------------------------- 契约守卫


def test_tool_spec_has_seven_contract_fields():
    """注册契约 = 七字段。少一个，Runtime 就少一样判断依据。"""
    from envy_agent_cli.tools.spec import ToolSpec

    contract = {"name", "description", "input_model", "output_model", "error_model",
                "permission", "risk"}
    assert contract <= set(ToolSpec.__dataclass_fields__), "ToolSpec 七字段不完整"

    # handler 是**实现层**，绝不能混进声明层
    assert "handler" not in ToolSpec.__dataclass_fields__, "handler 不属于 schema 层"


def test_registered_tool_keeps_implementation_and_exec_hints_separate():
    """执行参数（读写/并发/超时）与声明层分开存放。"""
    from envy_agent_cli.tools.spec import RegisteredTool

    fields = set(RegisteredTool.__dataclass_fields__)
    assert {"spec", "handler", "read_only", "concurrency_safe", "timeout", "max_retries"} <= fields


def test_error_codes_and_retry_policy():
    """只有瞬时故障可重试——这是"失败恢复"的依据，不能靠异常类型猜。"""
    from envy_agent_cli.tools.result import RETRYABLE_CODES, ErrorCode

    assert ErrorCode.TIMEOUT in RETRYABLE_CODES
    assert ErrorCode.UPSTREAM_ERROR in RETRYABLE_CODES
    for code in (ErrorCode.INVALID_ARGUMENT, ErrorCode.PERMISSION_DENIED, ErrorCode.REJECTED_BY_USER):
        assert code not in RETRYABLE_CODES, f"{code} 不该重试"


def test_tool_error_derives_retryable_from_code():
    from envy_agent_cli.tools.result import ErrorCode, ToolError

    assert ToolError.from_code(ErrorCode.TIMEOUT, "超时").retryable is True
    assert ToolError.from_code(ErrorCode.INVALID_ARGUMENT, "参数错").retryable is False


def test_event_protocol_is_complete():
    """模块1 的事件类型：正文增量 / 工具碎片 / 消息结束 / 用量 / 错误，一个都不能少。"""
    from envy_agent_cli.llm import events

    for name in ("TextDelta", "ToolCallDelta", "MessageEnd", "Usage", "Error"):
        assert hasattr(events, name), f"事件类型缺 {name}"

    # 工具调用的参数是**碎片**，必须能增量追加而不是一次性完整 JSON
    delta = events.ToolCallDelta(index=0, call_id="call_1", name="read_file", arguments='{"pa')
    delta.arguments += 'th": "a.txt"}'
    assert delta.arguments == '{"path": "a.txt"}'


def test_model_adapter_is_a_runtime_checkable_protocol():
    """Loop 只依赖 Protocol，不依赖任何厂商 SDK——这是"换模型不改 Loop"的前提。"""
    from envy_agent_cli.llm.protocol import ModelAdapter

    class FakeAdapter:
        name = "fake"

        def stream_chat(self, messages, tools=None, **params):
            return iter(())

    assert isinstance(FakeAdapter(), ModelAdapter)


# ---------------------------------------------------------------- 边界守卫


def _called_names(node: ast.AST) -> set[str]:
    """收集文件里被调用的函数名 / 属性名，用于静态边界检查。"""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_loop_never_calls_tool_handlers_directly():
    """红线：编排层不许绕过 Runtime 直接调用工具。

    绕过去 = 参数校验、权限判定、审计全部失效。
    """
    tree = ast.parse((SRC / "loop" / "react.py").read_text(encoding="utf8"))
    called = _called_names(tree)

    forbidden = {"read_file", "write_file", "list_dir", "handler", "_call_handler"}
    assert not (called & forbidden), f"编排层出现直接工具调用：{called & forbidden}"

    source = (SRC / "loop" / "react.py").read_text(encoding="utf8")
    assert "ToolRuntime" in source, "编排层必须通过 ToolRuntime 执行工具"


def test_runtime_exposes_single_execution_entry():
    """Runtime 对外只应有 execute / execute_all 两个入口。"""
    from envy_agent_cli.tools.runtime import ToolRuntime

    public = {n for n in dir(ToolRuntime) if not n.startswith("_")}
    assert {"execute", "execute_all"} <= public


def test_no_reverse_layer_dependency():
    """分层方向：依赖只能自上而下，下层不许 import 上层。

    这条不只是"洁癖"——`tools` 反向 import `loop` 会直接造成循环 import
    （骨架第一次跑测试就是这么炸的），同时也是架构红线的静态表达。
    """
    rules = {
        "llm": ("loop", "tools", "context"),      # 传输层只知道协议
        "tools": ("loop",),                        # 执行层不该知道编排层存在
        "context": ("loop",),
    }
    for layer, forbidden in rules.items():
        for path in (SRC / layer).rglob("*.py"):
            source = path.read_text(encoding="utf8")
            for upper in forbidden:
                assert f"envy_agent_cli.{upper}" not in source, \
                    f"{path.relative_to(SRC)} 反向依赖了 {upper} 层"


def test_trace_context_is_a_leaf_module():
    """trace 是跨层共享的叶子模块，自己不能依赖任何内部模块（否则又会绕回循环 import）。"""
    source = (SRC / "trace.py").read_text(encoding="utf8")
    for upper in ("llm", "loop", "tools", "context", "audit"):
        assert f"envy_agent_cli.{upper}" not in source, f"trace 依赖了 {upper}"


@pytest.mark.parametrize("module", ["envy_agent_cli", "envy_agent_cli.llm", "envy_agent_cli.loop",
                                    "envy_agent_cli.tools", "envy_agent_cli.audit",
                                    "envy_agent_cli.context", "envy_agent_cli.trace"])
def test_packages_importable(module):
    """骨架最起码要能被导入。"""
    __import__(module)
