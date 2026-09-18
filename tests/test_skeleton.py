"""契约守卫与边界守卫。

骨架/实现阶段都保留这组测试，它盯的是**两件容易悄悄坏掉的事**：
1. **封版的接口**不能被人随手改掉（七字段、三契约、六种事件、错误码形状）；
2. **架构边界**不能被越过（分层方向、Loop 不执行工具、trace 是叶子）。
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

    # handler 是**实现层**，绝不能混进声明层（模型永远看不到它）
    assert "handler" not in ToolSpec.__dataclass_fields__, "handler 不属于 schema 层"


def test_registered_tool_keeps_implementation_and_exec_hints_separate():
    from envy_agent_cli.tools.spec import RegisteredTool

    fields = set(RegisteredTool.__dataclass_fields__)
    assert {"spec", "handler", "read_only", "concurrency_safe", "timeout", "max_retries"} <= fields


def test_risk_levels_replaced_the_single_bool():
    """`needs_confirmation` 单 bool 已被三级 risk 取代——粗粒度会逼出硬编码。"""
    from envy_agent_cli.tools.spec import Risk

    assert {r.value for r in Risk} == {"low", "medium", "high"}


def test_error_shapes_are_unified_across_llm_and_tools():
    """模型错误与工具错误必须是同一种形状：`(code, retryable)`。

    形状不统一，Loop 就得为两种错误写两套分支——那正是我们要避免的耦合。
    """
    from envy_agent_cli.llm.events import Error as LLMError
    from envy_agent_cli.llm.events import LLMErrorCode
    from envy_agent_cli.tools.result import ErrorCode, ToolError

    llm_fields = set(LLMError.__dataclass_fields__)
    assert {"code", "retryable", "message"} <= llm_fields

    tool_fields = set(ToolError.__dataclass_fields__)
    assert {"code", "retryable", "message"} <= tool_fields

    assert LLMErrorCode.AUTH_FAILED.value == "auth_failed"
    assert ErrorCode.PERMISSION_DENIED.value == "PERMISSION_DENIED"


def test_only_transient_errors_are_retryable():
    from envy_agent_cli.llm.events import RETRYABLE_LLM_CODES, LLMErrorCode
    from envy_agent_cli.tools.result import RETRYABLE_CODES, ErrorCode

    assert RETRYABLE_LLM_CODES == {LLMErrorCode.RATE_LIMITED, LLMErrorCode.SERVER_ERROR,
                                   LLMErrorCode.TIMEOUT}
    assert LLMErrorCode.INVALID_REQUEST not in RETRYABLE_LLM_CODES
    assert LLMErrorCode.AUTH_FAILED not in RETRYABLE_LLM_CODES
    assert ErrorCode.INVALID_ARGUMENT not in RETRYABLE_CODES


def test_tool_error_derives_retryable_from_code():
    from envy_agent_cli.tools.result import ErrorCode, ToolError

    assert ToolError.from_code(ErrorCode.TIMEOUT, "超时").retryable is True
    assert ToolError.from_code(ErrorCode.INVALID_ARGUMENT, "参数错").retryable is False


def test_event_protocol_is_six_events():
    """事件协议：正文 / 思维链 / 工具碎片 / 消息结束 / 用量 / 错误，一个都不能少。"""
    from envy_agent_cli.llm import events

    for name in ("TextDelta", "ReasoningDelta", "ToolCallDelta", "MessageEnd", "Usage", "Error"):
        assert hasattr(events, name), f"事件类型缺 {name}"

    assert set(events.StopReason.__args__) == {"tool_use", "end_turn", "max_tokens",
                                               "stop_sequence"}


def test_tool_call_delta_carries_is_first_instead_of_new_event_type():
    """第一片用字段标记，不新增事件类型——避免下游 isinstance 分支膨胀。"""
    from envy_agent_cli.llm.events import ToolCallDelta

    delta = ToolCallDelta(index=0, is_first=True, call_id="c1", name="read_file", arguments='{"pa')
    delta.arguments += 'th": "a.txt"}'
    assert delta.is_first is True
    assert delta.arguments == '{"path": "a.txt"}'


def test_chat_params_is_frozen_with_documented_default():
    from dataclasses import FrozenInstanceError

    from envy_agent_cli.llm.params import ChatParams

    params = ChatParams()
    assert params.temperature == 0.2
    assert params.to_body_fields()["temperature"] == 0.2
    with pytest.raises(FrozenInstanceError):
        params.temperature = 0.9  # type: ignore[misc]


def test_chat_model_is_a_runtime_checkable_protocol():
    """Loop 只依赖 Protocol，不依赖任何厂商 SDK——"换模型不改 Loop"的前提。"""
    from envy_agent_cli.llm.adapter import ChatModel

    class FakeAdapter:
        name = "fake"

        def stream_chat(self, messages, tools=None, params=None, trace=None):
            return iter(())

    assert isinstance(FakeAdapter(), ChatModel)


# ---------------------------------------------------------------- 边界守卫


def _called_names(node: ast.AST) -> set[str]:
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


def test_adapter_does_not_retry_or_render():
    """Adapter 是契约不是适配层：重试归 RetryPolicy、路由归工厂、降级归 Loop。"""
    source = (SRC / "llm" / "adapter.py").read_text(encoding="utf8")
    assert "import envy_agent_cli.llm.retry" not in source
    assert "print(" not in source


def test_runtime_exposes_single_execution_entry():
    from envy_agent_cli.tools.runtime import ToolRuntime

    public = {n for n in dir(ToolRuntime) if not n.startswith("_")}
    assert {"execute", "execute_all"} <= public


def test_no_reverse_layer_dependency():
    """分层方向：依赖只能自上而下，下层不许 import 上层。

    这条不只是"洁癖"——`tools` 反向 import `loop` 会直接造成循环 import
    （骨架第一次跑测试就是这么炸的），同时也是架构红线的静态表达。
    """
    rules = {
        "llm": ("loop", "tools", "context", "audit"),
        "tools": ("loop",),
        "context": ("loop",),
    }
    for layer, forbidden in rules.items():
        for path in (SRC / layer).rglob("*.py"):
            source = path.read_text(encoding="utf8")
            for upper in forbidden:
                assert f"envy_agent_cli.{upper}" not in source, \
                    f"{path.relative_to(SRC)} 反向依赖了 {upper} 层"


def test_trace_context_is_a_leaf_module():
    """trace 是跨层共享的叶子：只依赖标准库，且是纯数据。"""
    source = (SRC / "trace.py").read_text(encoding="utf8")
    for upper in ("llm", "loop", "tools", "context", "audit"):
        assert f"envy_agent_cli.{upper}" not in source, f"trace 依赖了 {upper}"

    from dataclasses import FrozenInstanceError

    from envy_agent_cli.trace import new_trace, round_span, tool_span

    trace = round_span(new_trace(), 2)
    assert trace.span_id.endswith(":r2")
    assert tool_span(trace).parent_span_id == trace.span_id
    with pytest.raises(FrozenInstanceError):
        trace.trace_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize("module", ["envy_agent_cli", "envy_agent_cli.llm", "envy_agent_cli.loop",
                                    "envy_agent_cli.tools", "envy_agent_cli.audit",
                                    "envy_agent_cli.context", "envy_agent_cli.trace"])
def test_packages_importable(module):
    __import__(module)
