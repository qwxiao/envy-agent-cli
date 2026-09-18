"""模块3 · ToolRuntime —— Agent 的"受控执行系统"，全流程唯一执行入口。

**七职责（本类的全部工作）**

| # | 职责 | 对应方法 |
|---|---|---|
| 1 | 查找工具 | `_lookup` |
| 2 | 参数校验 | `_validate` |
| 3 | 鉴权 / 权限判定 | `_authorize` |
| 4 | 超时控制 | `_invoke_with_timeout` |
| 5 | 重试 | `_invoke_with_retry` |
| 6 | 审计 | `_audit` |
| 7 | 结果归一化 | `_normalize` |

**两条不能破的边界**
- Loop **不许绕过本类**直接调用工具 handler——绕过去，校验/权限/审计全部失效；
- 本类**不替业务方定义权限规则**，只执行工具声明的策略（`ToolSpec.permission` / `ToolSpec.risk`）。

**一个必须分清的区别**：HITL 是"**问人**"，鉴权是"**判定**"。
两者都叫"权限"，但前者把决定权交给人，后者按声明自动判定。本项目两者都要有。

**HITL 三模式**：`never`（全放行）/ `auto`（只问高风险或显式要求确认的）/ `always`（全问）。
"""

from typing import Callable

from envy_agent_cli.tools.result import ToolResult
from envy_agent_cli.tools.spec import RegisteredTool
from envy_agent_cli.trace import TraceContext

#: 人工审批函数签名：传入工具与参数，返回是否放行
ConfirmFn = Callable[[RegisteredTool, dict], bool]


class ToolRuntime:
    """工具执行的唯一入口。"""

    def __init__(
        self,
        *,
        hitl_mode: str = "auto",
        max_concurrent_read: int = 4,
        confirm: ConfirmFn | None = None,
        audit=None,
    ) -> None:
        """
        Args:
            hitl_mode: never / auto / always。
            max_concurrent_read: 只读工具的最大并发数（有界，防打爆）。
            confirm: 人工审批回调（默认走终端 input）。
            audit: 审计日志写入器（见 `envy_agent_cli.audit.logger`）。
        """
        self.hitl_mode = hitl_mode
        self.max_concurrent_read = max_concurrent_read
        self.confirm = confirm
        self.audit = audit

    # ---------- 对外：两个入口 ----------

    def execute(self, call: dict, trace: TraceContext) -> ToolResult:
        """执行**一次**工具调用。

        Args:
            call: 编排层拼装并解析后的调用，两种形态：
                - 正常：`{"seq": int, "id": ..., "name": ..., "arguments": {...}}`
                - 参数不是合法 JSON：`{"seq": int, "id": ..., "name": ..., "arguments": None,
                  "parse_error": "..."}` —— 此时**不派发 handler**，
                  直接产出 `INVALID_ARGUMENT` 的结构化结果（`executed=False`，仍记审计，
                  模型也要看到它）。
            trace: 调用级追踪上下文。

        注意输入契约：Runtime 只接受**结构化的调用**，`arguments` 必须是 dict。
        字符串不是它的语言——`json.loads` 由编排层做完（职责划分见 loop/react.py 模块说明）。
        """
        raise NotImplementedError("待实现：七职责串联")

    def execute_all(self, calls: list[dict], trace: TraceContext) -> list[ToolResult]:
        """执行**一批**调用，返回与入参一一对应的结果列表。

        调度策略：**只读且线程安全**的工具并发执行（有界线程池）；
        其余（写工具、未注册工具）串行——写操作有副作用，不能并行。
        """
        raise NotImplementedError("待实现：读写分流 + 只读并发")

    # ---------- 对内：七职责，一个方法一个 ----------

    def _lookup(self, name: str) -> RegisteredTool | None:
        """职责 1 · 查找。查不到不算异常，转成"可用工具列表"的错误结果让模型自我修正。"""
        raise NotImplementedError

    def _validate(self, tool: RegisteredTool, args: dict) -> dict:
        """职责 2 · 校验。参数不合法 → `INVALID_ARGUMENT`，**不该重试**，让模型改参数。"""
        raise NotImplementedError

    def _authorize(self, tool: RegisteredTool, args: dict) -> None:
        """职责 3 · 鉴权判定。校验参数是否越出 `ToolSpec.permission` 的路径白名单。

        注意：这里只做**判定**（自动、可复现）；**问人**（HITL）是下一步的事。
        """
        raise NotImplementedError

    def _needs_approval(self, tool: RegisteredTool) -> bool:
        """HITL：按模式 + 风险等级决定要不要问人。"""
        raise NotImplementedError

    def _invoke_with_timeout(self, tool: RegisteredTool, args: dict) -> str:
        """职责 4 · 超时。⚠️ 超时只是"没等到结果"，**不代表动作没执行**——
        非幂等的写操作超时后不能盲目重试。"""
        raise NotImplementedError

    def _invoke_with_retry(self, tool: RegisteredTool, args: dict) -> tuple[str, bool]:
        """职责 5 · 重试。有边界重试 = 最大次数 + 指数退避 + jitter（防惊群）+ 降级，四者缺一不可。

        只对 `RETRYABLE_CODES` 重试；确定性异常（如文件不存在）直接返回错误结果。
        """
        raise NotImplementedError

    def _audit(self, tool: RegisteredTool, args: dict, status: str, trace: TraceContext, **extra) -> None:
        """职责 6 · 审计。**每次执行落一条结构化 JSONL**，用 trace_id 串起来。

        这不是"日志好看"——它是回归评测的 Trajectory 原始数据源。
        """
        raise NotImplementedError

    def _normalize(self, raw: object, error: Exception | None = None) -> ToolResult:
        """职责 7 · 归一化。无论成功还是失败，都变成结构化结果回灌模型，**不抛异常**。

        `Tool Result 只是新的 observation`，不是终点。
        """
        raise NotImplementedError
