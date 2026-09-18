"""模块3 · 执行契约（Runtime → Loop）。

**三份 Schema 不可合并**：
- Input Schema 拦错误调用；
- Output Schema 固定成功事实（本模块 `ToolResult`）；
- Error Schema 驱动失败恢复（本模块 `ToolError`）。

缺一个，Loop 就得替它干活——缺 Error Schema，失败无法分类，Loop 只能猜下一步。
而 Loop 的天职是**确定性编排**，一旦要"猜"，它就退化成另一个 LLM，架构上直接失败。

**重试依据 `code`，不依据异常类型**：只有瞬时故障允许重试。
"""

from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    """结构化错误码。它是"失败恢复"的依据，不是给人看的文案。"""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"      # 参数不合法 —— 不该重试，让模型改参数
    PERMISSION_DENIED = "PERMISSION_DENIED"    # 越出权限声明 —— 不该重试
    REJECTED_BY_USER = "REJECTED_BY_USER"      # HITL 拒绝 —— 不该重试
    NOT_FOUND = "NOT_FOUND"                    # 工具或目标不存在 —— 不该重试
    TIMEOUT = "TIMEOUT"                        # 超时 —— 可重试（但注意幂等性！）
    UPSTREAM_ERROR = "UPSTREAM_ERROR"          # 上游/IO 瞬时故障 —— 可重试
    UNKNOWN = "UNKNOWN"                        # 兜底 —— 不重试


#: 允许重试的错误码。其余错误重试一万次也没用，只会白白放大成本。
RETRYABLE_CODES = frozenset({ErrorCode.TIMEOUT, ErrorCode.UPSTREAM_ERROR})


@dataclass(slots=True)
class ToolResult:
    """一次调用的归一化结果。

    Attributes:
        executed: **是否真的派发到了工具函数**。
            参数不是合法 JSON、工具未注册、权限判定不通过时都是 `False`——
            "执行了但失败"与"根本没执行"是**两个不同的指标**，审计与评测都要能分开统计。
    """

    content: str
    is_error: bool = False
    tool_call_id: str | None = None
    executed: bool = True


@dataclass(slots=True)
class ToolError:
    """失败路径的结构化错误（Error Schema 的落地形态）。

    Attributes:
        code: 错误分类，决定"能不能重试 / 要不要让模型改参数"。
        message: 给模型看的说明（**脱敏后**，不带堆栈）。
        retryable: 是否属于瞬时故障。
        tool_call_id: 对上它对应的那次调用。
    """

    code: ErrorCode
    message: str
    retryable: bool = False
    tool_call_id: str | None = None

    @classmethod
    def from_code(cls, code: ErrorCode, message: str, tool_call_id: str | None = None) -> "ToolError":
        """按错误码自动推导 retryable。"""
        return cls(code=code, message=message, retryable=code in RETRYABLE_CODES, tool_call_id=tool_call_id)
