"""模块3 · 工具层（声明 + 注册 + 受控执行）。

对外的三个概念：ToolSpec（声明什么）、registry（有哪些）、ToolRuntime（怎么安全地执行）。
"""

from envy_agent_cli.tools.registry import all_names, all_tools, get, register, to_model_schemas
from envy_agent_cli.tools.result import RETRYABLE_CODES, ErrorCode, ToolError, ToolResult
from envy_agent_cli.tools.runtime import ToolRuntime
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec

__all__ = [
    "RETRYABLE_CODES",
    "ErrorCode",
    "Permission",
    "RegisteredTool",
    "Risk",
    "ToolError",
    "ToolResult",
    "ToolRuntime",
    "ToolSpec",
    "all_names",
    "all_tools",
    "get",
    "register",
    "to_model_schemas",
]
