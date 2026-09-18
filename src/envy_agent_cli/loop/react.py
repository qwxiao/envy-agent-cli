"""模块2 · ReAct 主循环（编排层）。

**职责（SHOULD DO）**
- 消费模块1 的事件流，攒工具调用碎片（按 index 建槽位，`arguments += 碎片`）；
- 维护 messages：assistant 的调用意图 + tool 的执行结果，**两笔账都要记**；
- 用 `stop_reason` 驱动循环：`tool_use` → 执行工具继续，`end_turn` → 收尾返回；
- 每轮请求前调用压缩器，防 messages 越滚越肥；
- 生成 trace：任务级 → 轮次级 → 调用级。

**边界（SHOULD NOT）**
- ❌ **不执行工具**：一律交给 `ToolRuntime.execute_all()`（绕过 Runtime = 校验/权限/审计全部失效）；
- ❌ 不碰 HTTP / 厂商 SDK（只依赖 `ChatModel` 契约）；
- ❌ 不在这一层做 `print` 以外的渲染决策——渲染通过注入的 `render` 回调，将来可换成 UI。

**一句话**：编排层是"翻译官"——把模型的意图翻译成工具动作，把工具结果翻译回模型能懂的对话。
"""

from typing import Callable

from envy_agent_cli.llm.adapter import ChatMessage, ChatModel
from envy_agent_cli.llm.events import AnyEvent, MessageEnd, TextDelta, ToolCallDelta, Usage
from envy_agent_cli.tools.runtime import ToolRuntime

MAX_ROUNDS = 20  # 循环护栏：模型可能死循环调用同一个工具，上限是生产级 Agent 的基本护栏


def run(
    question: str,
    *,
    adapter: ChatModel,
    runtime: ToolRuntime,
    render: Callable[[str], None] = lambda text: print(text, end="", flush=True),
    max_rounds: int = MAX_ROUNDS,
) -> None:
    """跑完一次完整的用户任务。

    依赖通过参数注入（不在模块顶层 new 全局对象）——这样测试可以塞 FakeAdapter / FakeRuntime，
    也让"换模型不改 Loop"在代码层面成立。
    """
    raise NotImplementedError("待移植：模块2 已验证的循环实现，按上述职责边界归位")


def _consume_stream(
    events,
    render: Callable[[str], None],
) -> tuple[str, list[dict] | None]:
    """消费一轮事件流：边收边渲染正文，攒齐工具调用。

    Returns:
        (assistant_text, tool_calls)
        - `tool_calls is None`：本轮没有工具调用（end_turn / max_tokens / error）
        - `tool_calls` 为 list：模型想调用这些工具，统一用 `{"id","name","arguments"}` 槽位

    关键点：`arguments` 在这一层才拼成完整字符串，**流里绝不中途 json.loads**。
    """
    raise NotImplementedError("待移植")


def _archive_assistant(messages: list[ChatMessage], text: str, tool_calls: list[dict]) -> None:
    """把模型的调用意图按 API 协议格式存档。

    ⚠️ 不能只存工具结果：协议要求 tool 消息必须配对前面 assistant 的调用请求，
    不存这一条，模型下一轮会失忆、重复调用。
    """
    raise NotImplementedError("待移植")


def _archive_tool_results(messages: list[ChatMessage], pairs: list[tuple[dict, str]]) -> None:
    """把工具执行结果作为 role=tool 的消息回填（用 tool_call_id 对上调用）。

    错误也是结果：不抛异常，作为文本回灌给模型，让它自己调整策略。
    """
    raise NotImplementedError("待移植")
