"""模块5 · MCP 客户端：把工具来源从进程内扩展到进程外。

对外的四个概念：

- `McpServerSpec` —— 连什么（配置文件反序列化）
- `McpClient` —— 怎么谈（握手、发现、调用）
- `connect_mcp_servers` —— 怎么变成内部工具（注册进注册表）
- `McpSession` —— 谁持有连接（不持有就泄漏）

**与模块3 的边界**：MCP 工具注册进同一个 `registry`、走同一个 `ToolRuntime.execute`——
这是架构承诺（见 ARCHITECTURE.md「明确不做」表）。本模块只负责"发现与调用"，
**不碰鉴权、不碰审批、不碰审计**，那些全部复用模块3 已有的七职责。
"""

from envy_agent_cli.mcp.bridge import McpSession, connect_mcp_servers
from envy_agent_cli.mcp.client import McpClient
from envy_agent_cli.mcp.config import describe_mcp_config, load_mcp_server_specs
from envy_agent_cli.mcp.types import (
    McpError,
    McpErrorCode,
    McpServerSpec,
    McpTool,
    McpToolResult,
)

__all__ = [
    "McpClient",
    "McpError",
    "McpErrorCode",
    "McpServerSpec",
    "McpSession",
    "McpTool",
    "McpToolResult",
    "connect_mcp_servers",
    "describe_mcp_config",
    "load_mcp_server_specs",
]
